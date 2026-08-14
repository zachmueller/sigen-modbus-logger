#!/usr/bin/env python3
"""
Shared Modbus transport and decode primitives for the Sigenergy toolchain.

Not a CLI. Imported by dump.py, log.py and decode.py, which are run directly from
this directory so a plain `import lib` resolves. Stdlib only.

Conventions this file owns, so that capture and offline decode cannot disagree:
  - Read-only ("ro") registers are INPUT registers -> FC4. Holding ("rw") -> FC3.
  - `gain` is a divisor: value = raw / gain.
  - DTYPE_OVERRIDE fixes upstream register-map type errors.
  - Epoch-style registers hold *local* time, so decode them with gmtime.

What is NOT here: the address, port and unit ids of a particular installation.
Those live in config.py / config.json. This file holds only facts about the
protocol and the device family.
"""
import socket
import struct
import time

MAX_SPAN = 124  # largest accepted block read
MAX_GAP = 16    # start a new block rather than span a bigger undocumented hole

EXC = {1: "ILLEGAL FUNCTION", 2: "ILLEGAL DATA ADDRESS", 3: "ILLEGAL DATA VALUE",
       4: "SERVER DEVICE FAILURE", 6: "SERVER DEVICE BUSY"}

WIDTH = {"U16": 1, "S16": 1, "U32": 2, "S32": 2, "U64": 4}
FMT = {"U16": ">H", "S16": ">h", "U32": ">I", "S32": ">i", "U64": ">Q"}

# Upstream map bugs, corrected here. A power factor is bounded to [-1, 1], so a
# U16 gain-1000 read of 64538 ("64.5") can only be S16 -998 -> -0.998.
DTYPE_OVERRIDE = {"inverter_power_factor": "S16"}

# Epoch-style registers. This unit stores *local* time in them, so the raw value is
# already offset by plant_system_timezone; decoding with localtime() double-counts
# the offset. Verified: reg 30000 -> gmtime matches wall clock, localtime is +12 h.
TIME_KEYS = ("plant_system_time", "inverter_startup_time", "inverter_shutdown_time")


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise EOFError("connection closed")
        buf += c
    return buf


class ModbusError(IOError):
    """A protocol-level refusal. Distinct from transport errors: retrying won't help."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"exception {code} ({EXC.get(code, '?')})")


class Modbus:
    """One persistent connection; unit id is per-request.

    Tracks `connects` and `connected_at` so callers can tell a silent
    reconnect from a genuinely long-lived connection.
    """

    def __init__(self, host, port=502, timeout=6.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.tid, self.sock = 0, None
        self.connects, self.connected_at = 0, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            self.connected_at = None

    def _connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.connects += 1
        self.connected_at = time.monotonic()

    def connection_age(self):
        return None if self.connected_at is None else time.monotonic() - self.connected_at

    def read(self, unit, addr, count, fc, retries=2):
        last = None
        for attempt in range(retries + 1):
            try:
                if self.sock is None:
                    self._connect()
                self.tid = (self.tid + 1) & 0xFFFF
                pdu = struct.pack(">BHH", fc, addr, count)
                self.sock.sendall(
                    struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, unit) + pdu)
                head = recv_exact(self.sock, 8)
                _, _, length, _, fc_r = struct.unpack(">HHHBB", head)
                rest = recv_exact(self.sock, length - 2)
                if fc_r & 0x80:
                    raise ModbusError(rest[0] if rest else 0)
                return rest[1:]
            except ModbusError:
                raise  # a protocol-level refusal; retrying changes nothing
            except Exception as e:
                last = e
                self.close()
                time.sleep(0.25)
        raise last


def plan_blocks(fields):
    """Group fields into block reads that start and end on field boundaries."""
    blocks, cur, end = [], [], None
    for f in sorted(fields, key=lambda x: (x["addr"], x["count"])):
        if not cur:
            cur, end = [f], f["addr"] + f["count"]
            continue
        f_end = f["addr"] + f["count"]
        if f["addr"] - end > MAX_GAP or f_end - cur[0]["addr"] > MAX_SPAN:
            blocks.append(cur)
            cur, end = [f], f_end
        else:
            cur.append(f)
            end = max(end, f_end)
    if cur:
        blocks.append(cur)
    return [(b[0]["addr"], max(f["addr"] + f["count"] for f in b) - b[0]["addr"], b)
            for b in blocks]


def dtype_of(field):
    return DTYPE_OVERRIDE.get(field["key"], field["dtype"])


def decode(buf, field):
    """Return (raw, scaled). STRING returns the same string twice."""
    dt = dtype_of(field)
    gain = field["gain"] or 1
    if dt == "STRING":
        s = buf.decode("ascii", "replace").rstrip("\x00").strip()
        return s, s
    n = WIDTH[dt]
    raw = struct.unpack(FMT[dt], buf[: n * 2])[0]
    return raw, (raw / gain if gain != 1 else raw)


def is_sentinel(raw, dtype):
    """All-ones unsigned reads are Sigenergy's 'no value' marker.

    Confirmed by the spec text on 40042: "With value 0xFFFFFFFF, register is not
    valid."
    """
    if dtype == "U16":
        return raw == 0xFFFF
    if dtype == "U32":
        return raw == 0xFFFFFFFF
    if dtype == "U64":
        return raw == 0xFFFFFFFFFFFFFFFF
    if dtype == "S16":
        return raw == 0x7FFF
    if dtype == "S32":
        return raw == 0x7FFFFFFF
    return False


def is_absent_marker(raw, dtype):
    """Signed -1 (all-ones bit pattern) on hardware channels the unit doesn't have.

    Kept separate from is_sentinel: -1 is a legal value for many fields, so the
    reading is preserved and only annotated.
    """
    return dtype in ("S16", "S32") and raw == -1


def device_local(raw):
    """Format an epoch-style register. See TIME_KEYS for why this is gmtime."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(raw))


def pct(vals, p):
    """Nearest-rank percentile, index-clamped so a short list can't raise.

    Shared by the logger's soak gates and the offline latency report so the two
    never quote differently-computed p95s for the same data.
    """
    s = sorted(vals)
    return s[min(int(len(s) * p), len(s) - 1)]
