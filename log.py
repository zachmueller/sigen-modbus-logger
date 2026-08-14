#!/usr/bin/env python3
"""
Tiered raw capture from a Sigenergy inverter. READ-ONLY, stdlib only.

Capture wide, decode narrow: read the largest block per request that the device
accepts, store the raw bytes, and decide what to decode later with decode.py.
Cost is per-request, not per-register, so a 124-register read is no dearer than a
2-register one -- the thing to economise on is requests per second, not fields.

Usage:
  log.py [host] [--seconds N] [--out DIR] [--rotate-minutes N] [--soak-report]
  log.py [host] --baseline            snapshot the full map as a topology baseline
  log.py [host] --check               diff the full map against that baseline

  --seconds 0     run until interrupted (default 60) -- use this for deployment
                  note: --seconds counts scheduler ticks (1 s each), not samples
  --keep-days N   delete .bin.gz older than N days on rotation (0 = keep all)
  --soak-report   bucket latency by 5 min, apply pass/fail gates, abort on breach

Host, cadence, unit ids and the persistent-run tuning come from config.json; see
config.py for every key and its default. CLI flags override it.

Built for indefinite operation: bounded memory, a heartbeat line every 5 min, a
schedule-rebase guard so a machine sleep never replays missed ticks, and a
degraded probe mode while the device is unreachable.

Nothing here writes to the device. Holding registers are read with FC3, which is
a read; write function codes are never issued.
"""
import collections
import gzip
import hashlib
import json
import os
import shutil
import signal
import statistics
import struct
import sys
import threading
import time

import config
import dump
import lib
from lib import Modbus, pct


def build_tiers(cfg):
    """The block plan: one row per block read, as
    (label, unit, addr, count, fc, period_s, offset_s).

    Every span below was validated as accepted; block reads must both start and
    end on a field boundary or the device answers exception 2.

    The three fast blocks deliberately share one tick rather than being spread
    across consecutive seconds: a sample is only coherent if grid, PV, battery and
    load were read at the same instant, and the energy balance depends on that.

    Cadence is cfg["fast_period_s"] (2 = 0.5 Hz, dropped from 1 s after median tick
    latency appeared to drift, with more load expected once the array is connected).
    Retuning it moves all three fast blocks together and changes plan_hash().
    """
    plant, inv, fast = cfg["plant_unit"], cfg["inverter_unit"], cfg["fast_period_s"]
    return [
        # Fast: 3 requests every fast_period_s, 317 registers.
        ("plant_live",    plant, 30000, 124, 4, fast, 0),
        ("plant_energy",  plant, 30200,  87, 4, fast, 0),
        ("inv_ac_dc",     inv,   31000, 106, 4, fast, 0),
        # Slow: odd offsets, so these land on ticks the fast tier skips. Peak
        # requests per tick is therefore 3, not the 4 it was when everything
        # shared even ticks.
        ("inv_battery",   inv,   30540,  84, 4,  60, 11),
        ("inv_pv_energy", inv,   31509,   4, 4,  60, 41),
        # Config: the export-policy surface. FC3 holding registers.
        ("plant_config",  plant, 40029,  40, 3, 300, 21),
    ]


def identity_block(cfg):
    return (cfg["inverter_unit"], 30500, 40, 4)  # model/serial/firmware, read once


HEADER = ">dHH"                # host epoch, present-block bitmask, latency ms
HEADER_LEN = struct.calcsize(HEADER)
FORMAT = "sigen-raw-1"

# Decoded during --soak-report only, to catch faults latency stats can't see.
SOAK_WATCH = ["plant_system_time", "plant_accumulated_grid_import_energy",
              "plant_accumulated_grid_export_energy"]

# Gates as a FRACTION OF THE TICK WINDOW, not absolute milliseconds. The window is
# fast_period_s seconds, so at 0.5 Hz a 900 ms tick uses 45% of its budget, whereas
# at 1 Hz it used 90%. Absolute thresholds silently became 2x stricter than intended
# when the cadence was halved, which made an ordinary tick look like a breach.
GATE_P95_FRAC = 0.50
GATE_MAX_FRAC = 0.90
BUCKET_HISTORY = 24  # completed buckets kept in memory when not soaking (2 h)


def gates(cfg):
    """(p95 gate, max gate) in ms, relative to the tick window."""
    window_ms = cfg["fast_period_s"] * 1000
    return int(GATE_P95_FRAC * window_ms), int(GATE_MAX_FRAC * window_ms)


def plan_hash(tiers):
    """Short fingerprint of the block plan.

    Embedded in both the manifest and every record filename. Retuning a cadence
    (fast_period_s, an offset, a span) changes it, so a file can never be silently
    decoded against a manifest describing a different plan -- which would misread
    every cadence-dependent check and, if a span changed, every field offset.

    The spec string format is frozen: changing it would invalidate every archive
    already on disk even though its block plan had not moved. tests/ pins it.
    """
    spec = ";".join(f"{u}:{a}+{c}:fc{f}:{p}@{o}"
                    for _, u, a, c, f, p, o in tiers)
    return hashlib.sha1(spec.encode()).hexdigest()[:8]


def bucket_label(i, bucket_s):
    """Label for the stats bucket tick `i` falls in.

    Must be UNIQUE per bucket: the heartbeat fires on label *transition*, so two
    consecutive buckets sharing a label means no heartbeat ever fires. Labelling
    purely in whole minutes did exactly that for any bucket_s that is not a
    multiple of 60 -- an easy thing to set now that it is configurable.
    """
    idx = i // bucket_s
    if bucket_s % 60 == 0:
        return f"{idx * bucket_s // 60:>3}-{(idx + 1) * bucket_s // 60} min"
    return f"{idx * bucket_s:>4}-{(idx + 1) * bucket_s} s"


def blocks_bytes(tiers, idx):
    return tiers[idx][3] * 2


def fires(tiers, idx, i):
    _, _, _, _, _, period, offset = tiers[idx]
    return i % period == offset % period


# ---------------------------------------------------------------- archive writer

class Archive:
    """Rotating raw-record writer. Gzips completed files off the capture thread.

    Built for indefinite runs: nothing here accumulates without bound, and the
    manifest is re-emitted when the date rolls over so every day's files sit
    beside a manifest that can decode them.
    """

    def __init__(self, outdir, rotate_minutes, manifest, keep_days=0):
        self.outdir = outdir
        self.rotate_s = rotate_minutes * 60
        self.manifest = manifest
        self.keep_days = keep_days
        self.fh, self.path, self.opened_at = None, None, None
        self.bytes_written, self.records, self.rotations = 0, 0, 0
        self.pruned = 0
        self.threads = []
        os.makedirs(outdir, exist_ok=True)
        self.manifest_path = self._write_manifest()

    def _write_manifest(self):
        """Idempotent: one manifest per calendar day, written only if absent."""
        path = os.path.join(
            self.outdir,
            f"sigen-{time.strftime('%Y%m%d')}-{self.manifest['plan_hash']}"
            ".manifest.json")
        if not os.path.exists(path):
            with open(path, "w") as fh:
                json.dump(self.manifest, fh, indent=1)
        return path

    def _open(self):
        name = (f"sigen-{time.strftime('%Y%m%dT%H%M%S')}"
                f"-{self.manifest['plan_hash']}.bin")
        self.path = os.path.join(self.outdir, name)
        self.fh = open(self.path, "wb")
        self.opened_at = time.monotonic()

    def _gzip_async(self, path):
        def work():
            tmp = path + ".gz.tmp"
            try:
                with open(path, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                os.replace(tmp, path + ".gz")
                os.remove(path)
            except Exception:
                # Leave the plain .bin in place; it is still fully decodable.
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        t = threading.Thread(target=work, daemon=True)
        t.start()
        self.threads = [x for x in self.threads if x.is_alive()]  # don't accumulate
        self.threads.append(t)

    def _prune(self):
        """Drop compressed archives older than keep_days. 0 disables."""
        if not self.keep_days:
            return
        cutoff = time.time() - self.keep_days * 86400
        for name in os.listdir(self.outdir):
            if not name.endswith(".bin.gz"):
                continue
            p = os.path.join(self.outdir, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    self.pruned += 1
            except OSError:
                pass

    def write(self, ts, mask, latency_ms, payload):
        if self.fh is None:
            self._open()
        elif self.rotate_s and time.monotonic() - self.opened_at >= self.rotate_s:
            self.fh.close()
            self._gzip_async(self.path)
            self.rotations += 1
            self._open()
            self.manifest_path = self._write_manifest()  # may be a new day
            self._prune()
        rec = struct.pack(HEADER, ts, mask, min(latency_ms, 0xFFFF)) + payload
        self.fh.write(rec)
        # Flush every record. Default block buffering holds ~12 records (8 KB / 646 B),
        # so a hard kill would lose ~12 s and live monitoring would lag by as much.
        # 646 B/s into the page cache is free; this makes the archive durable against
        # process death to within one second. Not fsync: a power cut can still lose
        # the page cache, which is a deliberate trade to avoid a disk sync per second.
        self.fh.flush()
        self.bytes_written += len(rec)
        self.records += 1

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None
        for t in self.threads:  # let outstanding gzips finish rather than truncate
            t.join(timeout=30)


# ---------------------------------------------------------------------- manifest

def build_manifest(mb, bundle, cfg, tiers):
    """The format of record. Without it the archive is undecodable.

    Identity (model/serial/firmware, plus the address polled) is included so an
    archive can be traced back to the unit that produced it. That also makes a
    manifest identifying, so manifest_identity=false omits all of it for archives
    you intend to share -- decoding needs none of it.
    """
    ident = {}
    if cfg["manifest_identity"]:
        try:
            unit, addr, count, fc = identity_block(cfg)
            buf = mb.read(unit, addr, count, fc)
            for key, off, n in [("model", 0, 15), ("serial", 15, 10),
                                ("firmware", 25, 15)]:
                ident[key] = buf[off * 2:(off + n) * 2].decode("ascii", "replace") \
                    .rstrip("\x00").strip()
        except Exception as e:
            ident["error"] = str(e)
    else:
        ident["omitted"] = "manifest_identity is false"

    offset, blocks = 0, []
    for i, (label, unit, addr, count, fc, period, off) in enumerate(tiers):
        blocks.append({"index": i, "label": label, "unit": unit, "addr": addr,
                       "count": count, "fc": fc, "period_s": period, "offset_s": off,
                       "bytes": count * 2, "payload_offset_if_present": offset})
        offset += count * 2
    return {
        "format": FORMAT,
        "plan_hash": plan_hash(tiers),
        "fast_period_s": cfg["fast_period_s"],
        "host": cfg["host"] if cfg["manifest_identity"] else None,
        "port": cfg["port"],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "device": ident,
        "record": {
            "header_struct": HEADER,
            "header_fields": ["host_epoch_f64", "present_block_bitmask_u16",
                              "latency_ms_u16"],
            "note": "Payload is the raw bytes of each present block in block-index "
                    "order. Record length = header + sum(bytes of set bits), so a "
                    "reader derives length from the bitmask.",
        },
        "blocks": blocks,
        "regmap": bundle.get("_meta", {}),
    }


# -------------------------------------------------------------------- soak report

def locate(bundle, tiers, key):
    """Find which block carries a field, and at what payload offset."""
    for group in bundle["groups"].values():
        for f in group:
            if f["key"] != key:
                continue
            for i, (_, unit, addr, count, _, _, _) in enumerate(tiers):
                if addr <= f["addr"] and f["addr"] + f["count"] <= addr + count:
                    return i, (f["addr"] - addr) * 2, f
    return None


def soak_report(buckets, watch_series, archive, mb, expected_ticks, cfg, tiers):
    gate_p95, gate_max = gates(cfg)
    print(f"\n=== soak report: {cfg['bucket_s'] // 60}-minute buckets ===")
    print(f"  {'window':>12}  {'ticks':>5}  {'empty':>5}  {'median':>7}  {'p95':>7}  "
          f"{'max':>7}  {'>1s':>4}  {'retry':>5}  {'fail':>4}")
    rows, dataless = [], []
    for b in buckets:
        # A bucket with no data at all is an outage, not a latency measurement.
        # Report it as its own line rather than dropping it silently.
        if not b["lat"]:
            if b["empty"]:
                dataless.append(b)
                print(f"  {b['label']:>12}  {0:>5}  {b['empty']:>5}  "
                      f"{'NO DATA — device not answering':>33}")
            continue
        row = {"label": b["label"], "n": len(b["lat"]), "empty": b["empty"],
               "median": statistics.median(b["lat"]), "p95": pct(b["lat"], 0.95),
               "max": max(b["lat"]), "over": b["over"], "retry": b["retry"],
               "fail": b["fail"]}
        rows.append(row)
        print(f"  {row['label']:>12}  {row['n']:>5}  {row['empty']:>5}  "
              f"{row['median']:>7.1f}  {row['p95']:>7.1f}  {row['max']:>7.1f}  "
              f"{row['over']:>4}  {row['retry']:>5}  {row['fail']:>4}")

    checks = []
    tot_fail = sum(r["fail"] for r in rows)
    tot_retry = sum(r["retry"] for r in rows)
    tot_over = sum(r["over"] for r in rows)
    tot_empty = sum(r["empty"] for r in rows) + sum(b["empty"] for b in dataless)
    checks.append(("every tick returned data", tot_empty == 0,
                   f"{tot_empty} empty ticks"
                   + (f", {len(dataless)} bucket(s) with none at all"
                      if dataless else "")))
    checks.append(("retry-exhausted failures == 0", tot_fail == 0, f"{tot_fail}"))
    checks.append(("missed ticks (cycle > 1 s) == 0", tot_over == 0, f"{tot_over}"))
    checks.append(("reconnects == 0", mb.connects <= 1,
                   f"{mb.connects - 1} after the initial connect"))
    if rows:
        first, last = rows[0]["median"], rows[-1]["median"]
        checks.append((f"last bucket median within 1.5x of first",
                       last <= first * 1.5,
                       f"{first:.1f} ms -> {last:.1f} ms"))
        worst_p95 = max(r["p95"] for r in rows)
        checks.append((f"every bucket p95 < {gate_p95} ms",
                       worst_p95 < gate_p95, f"worst {worst_p95:.1f} ms"))
        worst_max = max(r["max"] for r in rows)
        checks.append((f"every bucket max < {gate_max} ms",
                       worst_max < gate_max, f"worst {worst_max:.1f} ms"))
        med = [r["median"] for r in rows]
        checks.append(("median not monotonically rising",
                       not all(x < y for x, y in zip(med, med[1:])),
                       " -> ".join(f"{x:.0f}" for x in med)))
    checks.append((f"retries reported (warning only, {tot_retry})", True,
                   f"{tot_retry}"))

    st = watch_series.get("plant_system_time", [])
    if st:
        # Expected step is the cadence of the block carrying plant_system_time, not
        # a hardcoded 1 s -- the fast tier is configurable.
        expect = next((t[5] for t in tiers if t[2] <= 30000 < t[2] + t[3]),
                      cfg["fast_period_s"])
        steps = [y - x for x, y in zip(st, st[1:])]
        bad = [s for s in steps if s != expect]
        checks.append((f"device clock advances exactly {expect}s per sample",
                       not bad,
                       f"{len(st)} samples, {len(steps) - len(bad)}/{len(steps)} "
                       f"steps == {expect}"
                       + (f", offenders {sorted(set(bad))[:6]}" if bad else "")))
    for key in ("plant_accumulated_grid_import_energy",
                "plant_accumulated_grid_export_energy"):
        s = watch_series.get(key, [])
        if s:
            drops = [(a, b) for a, b in zip(s, s[1:]) if b < a]
            checks.append((f"{key} non-decreasing", not drops,
                           f"{s[0]} -> {s[-1]}" + (f", {len(drops)} drops" if drops else "")))

    # Exact prediction across every tier, rather than a fast-tier-only estimate:
    # payload bytes per block scaled by its cadence, plus one header per tick that
    # had anything scheduled at all.
    payload = sum((expected_ticks // tiers[idx][5]) * blocks_bytes(tiers, idx)
                  for idx in range(len(tiers)))
    active = sum(1 for t in range(expected_ticks)
                 if any(fires(tiers, idx, t) for idx in range(len(tiers))))
    predicted = payload + active * HEADER_LEN
    ratio = archive.bytes_written / predicted if predicted else 0
    checks.append(("archive size within 5% of prediction",
                   0.95 <= ratio <= 1.05,
                   f"{archive.bytes_written/1e6:.3f} MB actual vs "
                   f"{predicted/1e6:.3f} MB predicted ({ratio:.3f}x) over "
                   f"{active}/{expected_ticks} active ticks"))

    print("\n=== gates ===")
    failed = 0
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<46} {detail}")
        if not ok:
            failed += 1
    print(f"\n  {len(checks) - failed}/{len(checks)} passed")
    if failed:
        print("  Fallback per plan: drop plant_energy (30200+87) to the slow tier, "
              "back to 2 fast requests / ~4.7% duty.")
    return failed == 0


# ------------------------------------------------------------ topology baseline

def baseline_path(outdir):
    return os.path.join(outdir, "topology-baseline.json")


def probe_ac_charger_units(mb):
    """The AC charger has its own slave id; find one if it ever appears."""
    found = []
    for unit in list(range(1, 11)) + [246, 247, 248]:
        try:
            mb.read(unit, 32000, 2, 4, retries=0)
            found.append(unit)
        except Exception:
            pass
    return found


def run_topology(mode, outdir, bundle, cfg):
    with Modbus(cfg["host"], cfg["port"], cfg["timeout_s"]) as mb:
        rows = dump.sweep(mb, bundle, cfg)
        ac_units = probe_ac_charger_units(mb)
    snap = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ac_charger_units": ac_units,
            "status": {k: r["status"] for k, r in rows.items()},
            "signals": {k: rows[k]["value"] for k in
                        ("inverter_pack_count", "inverter_pv_string_count",
                         "inverter_mppt_count") if k in rows}}
    path = baseline_path(outdir)

    if mode == "baseline":
        os.makedirs(outdir, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(snap, fh, indent=1)
        tally = {}
        for s in snap["status"].values():
            tally[s] = tally.get(s, 0) + 1
        print(f"baseline written to {path}")
        print(f"  {sum(tally.values())} fields: " +
              " · ".join(f"{k} {v}" for k, v in sorted(tally.items())))
        print(f"  pack/string/mppt counts: {snap['signals']}")
        print(f"  AC charger unit ids answering: {ac_units or 'none'}")
        return True

    if not os.path.exists(path):
        sys.exit(f"no baseline at {path} — run --baseline first")
    with open(path) as fh:
        old = json.load(fh)
    changes = []
    for key, new_status in snap["status"].items():
        was = old["status"].get(key)
        if was != new_status:
            changes.append(f"  {key}: {was} -> {new_status}")
    for key, new_val in snap["signals"].items():
        if old.get("signals", {}).get(key) != new_val:
            changes.append(f"  {key}: {old.get('signals', {}).get(key)} -> {new_val}")
    if set(snap["ac_charger_units"]) != set(old.get("ac_charger_units", [])):
        changes.append(f"  ac_charger_units: {old.get('ac_charger_units')} -> "
                       f"{snap['ac_charger_units']}")
    print(f"topology check against baseline of {old['captured_at']}")
    if changes:
        print(f"  {len(changes)} CHANGE(S) — hardware or config moved:")
        print("\n".join(changes))
        print("\n  Widen or add a tier block if a new subsystem came online.")
    else:
        print("  no change")
    return not changes


# ------------------------------------------------------------------- capture loop

def capture(cfg, tiers, outdir, duration, rotate_minutes, soak, bundle,
            fault_at=None, keep_days=0):
    fast_period = cfg["fast_period_s"]
    bucket_s = cfg["bucket_s"]
    max_lag_s = cfg["max_lag_s"]
    degrade_after = cfg["degrade_after"]
    degrade_probe_s = cfg["degrade_probe_s"]
    gap_log_quiet_s = cfg["gap_log_quiet_s"]
    recycle_s = cfg["recycle_s"]
    gate_p95, gate_max = gates(cfg)

    stop = {"now": False}

    def on_sig(*_):
        stop["now"] = True
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    watch = {}
    if soak:
        for key in SOAK_WATCH:
            loc = locate(bundle, tiers, key)
            if loc:
                watch[key] = loc
    watch_series = {k: [] for k in watch}

    mb = Modbus(cfg["host"], cfg["port"], cfg["timeout_s"])
    manifest = build_manifest(mb, bundle, cfg, tiers)
    archive = Archive(outdir, rotate_minutes, manifest, keep_days)
    print(f"Sigenergy raw capture @ {cfg['host']}:{cfg['port']}")
    print(f"  device   {manifest['device'].get('model','?')} "
          f"SN {manifest['device'].get('serial','?')}")
    print(f"  archive  {outdir}")
    print(f"  manifest {os.path.basename(archive.manifest_path)}")
    fast = [t for t in tiers if t[5] == fast_period]
    hz = 1.0 / fast_period
    print(f"  fast tier {len(fast)} req every {fast_period}s ({hz:g} Hz), "
          f"{sum(t[3] for t in fast)} regs; "
          f"slow {[t[0] for t in tiers if 60 <= t[5] < 300]}; "
          f"config {[t[0] for t in tiers if t[5] >= 300]}")
    print(f"  rotate every {rotate_minutes} min"
          + (f", keep {keep_days}d of .gz" if keep_days else ", keeping all archives")
          + (f", running {duration}s" if duration else ", running until interrupted"))
    if soak:
        print(f"  soak gates: p95 < {gate_p95} ms, max < {gate_max} ms per "
              f"{bucket_s//60}-min bucket, abort on breach")
    print()

    buckets, cur = [], None
    # Bounded: a persistent run must not grow a stats list for every tick it has
    # ever taken. A bucket holds at most bucket_s latencies by construction; what
    # would leak is keeping every completed bucket, so cap the history.
    drift = collections.deque(maxlen=bucket_s)
    retries_total, fails_total, gaps, degraded_ticks, recycles = 0, 0, 0, 0, 0
    t0 = time.perf_counter()
    i = 0
    aborted = None
    consecutive_dead = 0
    last_gap_log, gaps_at_last_log = -1e9, 0

    while not stop["now"] and (duration == 0 or i < duration):
        target = t0 + i
        now = time.perf_counter()
        if now < target:
            time.sleep(target - now)
        elif now - target > max_lag_s:
            # The machine slept, or an outage stalled the loop. Without this the
            # scheduler would fire every missed tick back-to-back with no sleep,
            # hammering the device to "catch up" on data that no longer exists.
            lost = now - target
            t0 += lost
            target = t0 + i
            gaps += 1
            # Rate-limited: a sustained outage produces one of these per tick, which
            # buries everything else in the log. Report the first, then a periodic
            # roll-up.
            if now - last_gap_log >= gap_log_quiet_s:
                extra = gaps - gaps_at_last_log - 1
                print(f"  [gap] {lost:.0f}s behind schedule — rebasing rather than "
                      f"replaying {int(lost)} missed ticks"
                      + (f" ({extra} further gaps suppressed in the last "
                         f"{gap_log_quiet_s}s)" if extra > 0 else ""), flush=True)
                last_gap_log, gaps_at_last_log = now, gaps
        tick_start = time.perf_counter()
        drift_ms = (tick_start - target) * 1000

        # Emit on bucket *transition*, not at an exact tick index. Bucket boundaries
        # land on odd ticks (299, 599, ...), which are idle whenever fast_period_s is
        # even -- an index-based check would be skipped by the idle-tick shortcut
        # below and the heartbeat would never fire at all.
        label = bucket_label(i, bucket_s)
        if cur is None or cur["label"] != label:
            if cur is not None and (cur["lat"] or cur["empty"]):
                # Latency over ticks that RETURNED DATA. An outage tick's latency is
                # socket timeout, not device latency, and a handful of them drags
                # the median to ~6500 ms -- which reads as a catastrophically slow
                # inverter when the truth is that it is not answering at all.
                if cur["lat"]:
                    p95, mx = pct(cur["lat"], 0.95), max(cur["lat"])
                    stats = (f"median {statistics.median(cur['lat']):.1f} ms  "
                             f"p95 {p95:.1f} ms  max {mx:.1f} ms")
                else:
                    p95 = mx = None
                    stats = "NO DATA — device not answering"
                print(f"  [{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                      f"{archive.records} records  "
                      f"{archive.bytes_written/1e6:.1f} MB  "
                      f"{stats}  "
                      f"retries {cur['retry']}  fails {cur['fail']}"
                      + (f"  empty {cur['empty']}" if cur["empty"] else "")
                      + (f"  gaps {gaps}" if gaps else "")
                      + (f"  recycles {recycles}" if recycles else "")
                      + (f"  UNEXPECTED reconnects {mb.connects - 1 - recycles}"
                         if mb.connects - 1 - recycles > 0 else ""),
                      flush=True)
                if soak and p95 is not None and (p95 >= gate_p95 or mx >= gate_max):
                    aborted = (f"bucket {cur['label']} breached the gate "
                               f"(p95 {p95:.1f} ms, max {mx:.1f} ms) — aborting "
                               f"rather than continuing to load the device")
                    break
            cur = {"label": label, "lat": [], "empty": 0, "over": 0,
                   "retry": 0, "fail": 0}
            buckets.append(cur)
            if not soak and len(buckets) > BUCKET_HISTORY:
                buckets.pop(0)

        if recycle_s and i and i % recycle_s == 0:
            age = mb.connection_age()
            mb.close()          # next read reconnects, ~17 ms
            recycles += 1
            print(f"  [recycle] closing connection at age {age:.0f}s "
                  f"(scheduled every {recycle_s}s)", flush=True)

        if fault_at is not None and i == fault_at:
            # Simulate the device dropping a long-lived connection, which is the
            # untested failure mode. read() reconnects on the next call.
            print(f"  [fault injection] closing socket at t={i}s "
                  f"(connection age {mb.connection_age():.0f}s)")
            mb.close()

        # With a fast tier slower than 1 s, most ticks have nothing scheduled. Skip
        # them outright: writing an empty record would bloat the archive and, worse,
        # an idle tick would otherwise count as a "dead" one and trip degraded mode.
        due = [idx for idx in range(len(tiers)) if fires(tiers, idx, i)]
        if not due:
            i += 1
            continue

        # While the device is unreachable, probe with one block instead of the full
        # plan. An outage otherwise costs 3 blocks x 2 attempts x the socket
        # timeout per tick, which stalls the loop far longer than the tick itself.
        degraded = consecutive_dead >= degrade_after
        if degraded:
            # Also stop probing every tick. Each attempt costs ~6 s in timeouts
            # against a dead host, so tick-rate probing is a tight retry loop that
            # floods the log and keeps hammering the network for nothing. Skipping
            # without touching consecutive_dead keeps us degraded until one lands.
            if i % degrade_probe_s != 0:
                i += 1
                continue
            due = due[:1]
            degraded_ticks += 1

        mask, payload, retries, fails = 0, [], 0, 0
        for idx in due:
            label, unit, addr, count, fc, _, _ = tiers[idx]
            got = None
            for attempt in (0, 1):
                try:
                    got = mb.read(unit, addr, count, fc, retries=0)
                    break
                except Exception:
                    if attempt == 0:
                        retries += 1
                    else:
                        fails += 1
            if got is not None and len(got) == count * 2:
                mask |= 1 << idx
                payload.append(got)
                if soak:
                    for key, (bidx, off, f) in watch.items():
                        if bidx == idx:
                            watch_series[key].append(lib.decode(got[off:off + f["count"] * 2],
                                                                f)[0])

        if mask:
            if degraded:
                print(f"  [recovered] device answering again after "
                      f"{consecutive_dead} dead ticks", flush=True)
            consecutive_dead = 0
        else:
            consecutive_dead += 1
            if consecutive_dead == degrade_after:
                print(f"  [degraded] {consecutive_dead} ticks with no data — "
                      f"probing with 1 block/tick until the device returns",
                      flush=True)

        latency_ms = (time.perf_counter() - tick_start) * 1000
        archive.write(time.time(), mask, int(latency_ms), b"".join(payload))

        if mask:
            cur["lat"].append(latency_ms)
        else:
            cur["empty"] += 1      # an outage probe; its latency is socket timeout
        cur["retry"] += retries
        cur["fail"] += fails
        if latency_ms > 1000:
            cur["over"] += 1
        drift.append(drift_ms)
        retries_total += retries
        fails_total += fails

        if i < 3 or (duration and i == duration - 1):
            print(f"  t={i:>4}s  {bin(mask).count('1')} blocks  "
                  f"latency {latency_ms:6.1f} ms  drift {drift_ms:+6.1f} ms", flush=True)

        i += 1

    ticks = i
    archive.close()
    age = mb.connection_age()
    mb.close()

    print(f"\n=== capture finished: {ticks} ticks, {archive.records} records, "
          f"{archive.bytes_written/1e6:.2f} MB, {archive.rotations} rotations ===")
    if drift:
        print(f"  drift median {statistics.median(drift):+.1f} ms  "
              f"max {max(drift):+.1f} ms")
    print(f"  retries {retries_total} · retry-exhausted failures {fails_total} · "
          f"connects {mb.connects}"
          + (f" · final connection age {age:.0f}s" if age else "")
          + (f" · scheduled recycles {recycles}" if recycles else "")
          + (f" · schedule gaps {gaps}" if gaps else "")
          + (f" · degraded ticks {degraded_ticks}" if degraded_ticks else "")
          + (f" · pruned {archive.pruned} old archives" if archive.pruned else ""))
    if aborted:
        print(f"\n  ABORTED: {aborted}")

    ok = True
    if soak:
        ok = soak_report(buckets, watch_series, archive, mb, ticks, cfg,
                         tiers) and not aborted
    return ok


def main():
    args = sys.argv[1:]
    duration, soak, mode = 60, False, "capture"
    outdir, rotate_minutes, keep_days = None, None, None
    fault_at = None
    rest, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--seconds":
            duration = int(args[i + 1]); i += 2
        elif a == "--out":
            outdir = args[i + 1]; i += 2
        elif a == "--rotate-minutes":
            rotate_minutes = int(args[i + 1]); i += 2
        elif a == "--keep-days":
            keep_days = int(args[i + 1]); i += 2
        elif a == "--soak-report":
            soak = True; i += 1
        elif a == "--fault-inject":
            fault_at = int(args[i + 1]); i += 2
        elif a in ("--baseline", "--check"):
            mode = a[2:]; i += 1
        elif a in ("-h", "--help"):
            sys.exit(__doc__.strip())
        else:
            rest.append(a); i += 1

    # CLI beats config beats default, for the three knobs that also have flags.
    cfg = config.load(overrides={
        "host": rest[0] if rest else None,
        "rotate_minutes": rotate_minutes,
        "keep_days": keep_days,
        "data_dir": outdir,
    })
    tiers = build_tiers(cfg)

    bundle = dump.load_regmap()
    if mode in ("baseline", "check"):
        sys.exit(0 if run_topology(mode, cfg["data_dir"], bundle, cfg) else 1)
    sys.exit(0 if capture(cfg, tiers, cfg["data_dir"], duration,
                          cfg["rotate_minutes"], soak, bundle, fault_at,
                          cfg["keep_days"]) else 1)


if __name__ == "__main__":
    main()
