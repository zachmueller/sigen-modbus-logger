#!/usr/bin/env python3
"""
Find the inverter again after its address changes. Stdlib only.

Why this exists: on 2026-09-04 the CMU lost its 2-hour DHCP lease, came back on a
different address, and an unrelated device took over the old one -- which answered
ICMP and refused every TCP port. The logger polled that address for fourteen hours
while the process, and the heartbeat line, both reported health. See FINDINGS.

What this module is NOT: a Modbus client. It opens plain TCP sockets to see which
hosts have the port open, and then asks a **caller-supplied `probe`** to identify
each one. That keeps log.py the only module in the repository that constructs
lib.Modbus -- an invariant cloud/lambda/ingest/PACKAGE.txt and tests/test_web.py
both depend on, and one worth more than the convenience of reading a register here.

The identity check is the whole safety argument. "Something is listening on 502" is
not "this is my inverter": a neighbour's unit, a heat pump or a building's BMS all
answer the same way. Nothing is adopted unless it reports the serial we expect, and
when no expected serial is known the search refuses to return a host at all.
"""
import os
import socket
import time
# TimeoutError is imported by name rather than relied on as the builtin: they are only
# the same class from Python 3.11, and the capture host runs 3.9.
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from concurrent.futures import as_completed

# A LAN sweep is 254 connects. Cheap in parallel, and bounded so that a scan can
# never stall the capture loop longer than the degraded tick it runs inside already
# costs (~6 s of socket timeouts).
CONNECT_TIMEOUT_S = 1.0
MAX_WORKERS = 64
# Two budgets, not one shared total. Sharing meant a slow sweep could consume the whole
# allowance and leave nothing to identify the hosts it had just found -- a search that
# reliably located the inverter and then always declined to adopt it.
#
# The sweep covers up to two /24s, so ~508 connects at 64 workers and a 1 s timeout is
# about 8 s if nothing answers. Measured on the real LAN it was a few seconds.
SWEEP_BUDGET_S = 20.0
IDENTIFY_BUDGET_S = 15.0
BUDGET_S = SWEEP_BUDGET_S      # kept as the name listeners() defaults to

# Addresses that are never a host on a /24.
_SKIP_LAST_OCTETS = (0, 255)

# One line of text naming the address actually being polled, written by log.py and read
# by bin/watchdog.py and bin/status.sh. It lives here because "which address is the
# device at" is this module's subject, and because the alternative -- the watchdog
# importing log.py for a filename -- would pull the only Modbus-capable module in the
# repository into a process that must never open a connection.
#
# A line of text rather than JSON: the single fact anyone wants from it is the host, and
# a schema would be a second thing to keep in step for no gain.
HOST_STATE_NAME = "logger-host"


def write_host_state(log_dir, host):
    """Record which address is being polled. Best-effort: never raises.

    A failure here must not stop capture. The file exists so a watchdog and a human can
    see which interface is live, and neither is worth an outage.
    """
    try:
        if not log_dir:
            return
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, HOST_STATE_NAME), "w") as fh:
            fh.write(host + "\n")
    except Exception:
        pass


def read_host_state(log_dir):
    """The address log.py last recorded, or None if it never did."""
    try:
        with open(os.path.join(log_dir, HOST_STATE_NAME)) as fh:
            return fh.read().strip() or None
    except Exception:
        return None


def local_ipv4():
    """This host's address on the interface that carries its default route.

    A UDP `connect()` sends no packets -- it only asks the kernel to pick a source
    address -- so this is a routing-table lookup, not traffic. The target is
    TEST-NET-1 (RFC 5737), which is documentation space and never routed, so nothing
    here can be mistaken for reaching out to a real service.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def _slash24(addr):
    """'192.168.1.42' -> '192.168.1.', or None if it is not a dotted quad."""
    parts = (addr or "").split(".")
    if len(parts) != 4:
        return None
    try:
        if not all(0 <= int(p) <= 255 for p in parts):
            return None
    except ValueError:
        return None
    return ".".join(parts[:3]) + "."


def sweep_targets(hint_hosts=()):
    """Every address worth trying, hints first and in order.

    Two /24s are searched, not one: the inverter's last known subnet AND the subnet
    this host is actually on. They are normally the same, but if the router's LAN
    range is what changed then the old subnet is empty and only the local one has
    the device on it -- and if the capture host is the thing that moved, the reverse.
    Searching both costs one extra sweep and removes a whole class of "the scan found
    nothing and I cannot tell why".
    """
    seen, out = set(), []

    def add(host):
        if host and host not in seen:
            seen.add(host)
            out.append(host)

    for h in hint_hosts:
        add(h)

    me = local_ipv4()
    prefixes = []
    for prefix in [_slash24(h) for h in hint_hosts] + [_slash24(me)]:
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)

    for prefix in prefixes:
        for last in range(1, 255):
            if last in _SKIP_LAST_OCTETS:
                continue
            host = f"{prefix}{last}"
            if host != me:          # we are not the inverter
                add(host)
    return out


def port_open(host, port, timeout=CONNECT_TIMEOUT_S):
    """True if the TCP handshake completes. A refusal and a timeout are both False.

    The distinction mattered while diagnosing -- refused means a live host with
    nothing listening, timed out means no host or a filter -- but for *finding* the
    inverter only "can I open a connection" matters.
    """
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def listeners(targets, port, budget_s=BUDGET_S, max_workers=MAX_WORKERS):
    """Which of `targets` have `port` open, in the order they answered.

    Bounded by `budget_s`, and the bound is the point: this runs inside the capture
    loop. Results are taken as they complete so that spending the budget still
    returns whatever was found, rather than discarding a sweep that had already
    located the device.
    """
    found, deadline = [], time.monotonic() + budget_s
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(port_open, h, port): h for h in targets}
        try:
            for fut in as_completed(futures, timeout=budget_s):
                if fut.result():
                    found.append(futures[fut])
                if time.monotonic() > deadline:
                    break
        except FuturesTimeout:
            pass
        # Queued-but-unstarted work is abandoned rather than waited on; without this
        # the `with` block joins the pool and the budget means nothing.
        for fut in futures:
            fut.cancel()
    return found


def find(expect_serial, probe, hint_hosts=(), port=502, sweep_budget_s=SWEEP_BUDGET_S,
         identify_budget_s=IDENTIFY_BUDGET_S, log=None):
    """The address to poll, or None.

    `probe(host)` must return that host's serial number as a string, or None if it
    could not be read. It is the caller's Modbus read -- see the module docstring for
    why the register access is not in here.

    Returns {"host", "latency_ms", "matches": [(host, latency_ms), ...],
             "listeners": [...], "elapsed_s"} or None.

    The winner is the **lowest-latency** match, not the first found. The unit answers
    on both its wired port and its Wi-Fi module with the same serial, and the wired
    one is several times faster; picking by latency lands on it without anything
    having to know which address is which.
    """
    say = log or (lambda *_a, **_k: None)
    t0 = time.monotonic()

    # No serial, no adoption. Returning the fastest unidentified Modbus server on the
    # LAN would be a plausible-looking way to start recording someone else's inverter
    # into an archive that claims to be this one's.
    if not expect_serial:
        say("  [discover] no expected serial is known — refusing to adopt any host. "
            "The archive's newest manifest carries one once a capture has succeeded.")
        return None

    targets = sweep_targets(hint_hosts)
    open_hosts = listeners(targets, port, budget_s=sweep_budget_s)
    if not open_hosts:
        say(f"  [discover] nothing listening on :{port} across "
            f"{len(targets)} addresses in {time.monotonic() - t0:.1f}s")
        return None

    matches, others = [], []
    identify_deadline = time.monotonic() + identify_budget_s
    for host in open_hosts:
        if time.monotonic() > identify_deadline:
            say(f"  [discover] identification budget spent; {len(open_hosts)} hosts had "
                f":{port} open but not all were asked who they are")
            break
        t1 = time.monotonic()
        try:
            serial = probe(host)
        except Exception as e:
            serial = None
            others.append((host, f"{type(e).__name__}: {e}"))
        latency_ms = (time.monotonic() - t1) * 1000
        if serial and serial == expect_serial:
            matches.append((host, latency_ms))
        elif serial:
            # Worth logging loudly: a second, DIFFERENT inverter on the LAN is the
            # case where adopting the wrong one would look entirely healthy.
            others.append((host, f"serial {serial!r} — not ours"))

    for host, why in others:
        say(f"  [discover] {host}: {why}")

    if not matches:
        say(f"  [discover] {len(open_hosts)} host(s) had :{port} open, none reported "
            f"serial {expect_serial!r}")
        return None

    matches.sort(key=lambda m: m[1])
    host, latency_ms = matches[0]
    return {"host": host, "latency_ms": latency_ms, "matches": matches,
            "listeners": open_hosts, "elapsed_s": time.monotonic() - t0}
