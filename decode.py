#!/usr/bin/env python3
"""
Offline decode of a raw archive written by log.py. Never touches the device.

This is the "decode narrow" half of capture-wide-decode-narrow: the archive holds
every register the tier blocks span, and this picks fields out of it after the
fact. Re-run it with a different --fields list whenever you want more columns --
the bytes are already on disk.

Usage:
  decode.py MANIFEST FILE.bin [FILE.bin.gz ...] [options]

  --fields a,b,c    field keys to emit (default: the live set, see DEFAULT_FIELDS)
  --all             every field the archive's blocks cover
  --last            vertical snapshot of the newest record ("is it working now?")
  --balance         derived export/self-consumption series instead of raw fields
  --format csv|jsonl                       (default csv)
  --downsample N    keep one row per N seconds
  --limit N         stop after N rows
  --check           integrity report only: clock steps, counter monotonicity, gaps
  --latency [MIN]   latency trend by wall-clock bucket (default 10 min), to
                    distinguish oscillation from real degradation
"""
import gzip
import json
import os
import re
import statistics
import struct
import sys
import time

import dump
import lib

HEADER = ">dHH"
HEADER_LEN = struct.calcsize(HEADER)
FORMAT = "sigen-raw-1"   # log.FORMAT, restated so a reader needs only this file

DEFAULT_FIELDS = [
    "plant_system_time",
    "plant_grid_sensor_active_power",
    "plant_active_power",
    "plant_sigen_photovoltaic_power",
    "plant_ess_power",
    "plant_general_load_power",
    "plant_ess_soc",
    "plant_running_state",
    "plant_on_off_grid_status",
    "plant_ess_available_max_charging_power",
    "plant_ess_available_max_discharging_power",
    "plant_ess_average_cell_temperature",
    "plant_accumulated_grid_import_energy",
    "plant_accumulated_grid_export_energy",
    "plant_pv_total_daily_generation",
    "inverter_grid_frequency",
    "inverter_phase_a_voltage",
    "inverter_phase_a_current",
    "inverter_power_factor",
    "inverter_pcs_internal_temperature",
    "inverter_pv_power",
    "inverter_pv1_voltage", "inverter_pv1_current",
    "inverter_pv2_voltage", "inverter_pv2_current",
    "inverter_pv3_voltage", "inverter_pv3_current",
    "inverter_pv4_voltage", "inverter_pv4_current",
]

# Keys the --balance mode needs; sign conventions come from the upstream
# descriptions: 30005 ">0 buy from grid; <0 sell to grid", 30037 ">0 charging".
BALANCE_KEYS = ["plant_system_time", "plant_grid_sensor_active_power",
                "plant_sigen_photovoltaic_power", "plant_ess_power",
                "plant_general_load_power", "plant_accumulated_grid_export_energy",
                "inverter_grid_frequency"]


def open_maybe_gz(path):
    return gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb")


STAMP_RE = re.compile(r"(\d{8}T\d{6})")
HASH_RE = re.compile(r"-([0-9a-f]{8})\.bin(\.gz)?$")


def sort_chronologically(paths):
    """Order files by the timestamp in their name, not by argv order.

    `*.bin *.bin.gz` is the natural thing to type and expands to plain files first,
    then compressed -- which is reverse-chronological for a rotating archive. Reading
    in that order produces backwards time steps and phantom counter drops that look
    like data corruption. Sort here so the caller can't get it wrong.
    """
    def key(p):
        m = STAMP_RE.search(os.path.basename(p))
        return (m.group(1) if m else "", os.path.basename(p))
    return sorted(paths, key=key)


def check_plan_hash(manifest, paths):
    """Refuse to decode files written under a different block plan.

    A cadence or span change alters how every record must be read, so a mismatch
    yields plausible-looking wrong numbers rather than an error. Absence of a hash
    on either side is itself a mismatch: manifests written before fingerprinting
    describe a plan we can no longer verify.
    """
    mh = manifest.get("plan_hash")
    bad = []
    for p in paths:
        m = HASH_RE.search(os.path.basename(p))
        fh = m.group(1) if m else None
        if fh != mh:
            bad.append((os.path.basename(p), fh or "none", mh or "none"))
    if bad:
        lines = "\n  ".join(f"{n}: file plan {f}, manifest plan {m}" for n, f, m in bad)
        sys.exit(f"plan-hash mismatch -- refusing to decode, since a differing block "
                 f"plan changes cadences and field offsets:\n  {lines}\n"
                 f"Decode each series with the manifest written alongside it.")


def records_from(manifest, path, offset=0):
    """Yield (ts, latency_ms, {block_index: bytes}) from one file.

    `offset` must be a record boundary -- the byte count after a previous complete
    record. It exists so a reader that has already seen the head of the still-open
    file can resume rather than re-read it; records are append-only, so anything
    before a boundary can never change. Seeking a .gz still decompresses from the
    start, so only pass an offset for a plain file.
    """
    size = {b["index"]: b["bytes"] for b in manifest["blocks"]}
    with open_maybe_gz(path) as fh:
        if offset:
            fh.seek(offset)
        while True:
            head = fh.read(HEADER_LEN)
            if len(head) < HEADER_LEN:
                break  # clean EOF, or a record torn by a kill: stop here
            ts, mask, latency = struct.unpack(HEADER, head)
            present = [i for i in sorted(size) if mask >> i & 1]
            need = sum(size[i] for i in present)
            payload = fh.read(need)
            if len(payload) < need:
                break  # truncated final record
            out, off = {}, 0
            for i in present:
                out[i] = payload[off:off + size[i]]
                off += size[i]
            yield ts, latency, out


def records(manifest, paths):
    """Yield (ts, latency_ms, {block_index: bytes}) per record, in file order."""
    for path in paths:
        for rec in records_from(manifest, path):
            yield rec


def resolve(manifest, bundle, keys):
    """Map field keys onto (block_index, byte_offset, field_def)."""
    by_key = {f["key"]: f for g in bundle["groups"].values() for f in g}
    located, missing = [], []
    for key in keys:
        f = by_key.get(key)
        if not f:
            missing.append((key, "not in regmap"))
            continue
        hit = None
        for b in manifest["blocks"]:
            if b["addr"] <= f["addr"] and f["addr"] + f["count"] <= b["addr"] + b["count"]:
                hit = (b["index"], (f["addr"] - b["addr"]) * 2, f)
                break
        if hit:
            located.append((key, hit))
        else:
            missing.append((key, "not covered by any captured block"))
    return located, missing


def covered_fields(manifest, bundle):
    keys = []
    for g in bundle["groups"].values():
        for f in g:
            if f["rtype"] == "wo":
                continue
            for b in manifest["blocks"]:
                if b["addr"] <= f["addr"] and f["addr"] + f["count"] <= b["addr"] + b["count"]:
                    keys.append(f["key"])
                    break
    return keys


def fmt_cell(key, v):
    """%g turns a 1.7e9 epoch into scientific notation, so ints stay ints."""
    if v is None:
        return ""
    if key in lib.TIME_KEYS:
        return lib.device_local(v) if v else ""
    if isinstance(v, str):
        return v
    if isinstance(v, int):
        return str(v)
    return f"{v:g}"


def value_of(blocks, loc):
    idx, off, f = loc
    buf = blocks.get(idx)
    if buf is None:
        return None
    raw, val = lib.decode(buf[off:off + f["count"] * 2], f)
    dt = lib.dtype_of(f)
    if dt != "STRING" and lib.is_sentinel(raw, dt):
        return None
    return val


def emit_rows(manifest, bundle, paths, keys, fmt, downsample, limit):
    located, missing = resolve(manifest, bundle, keys)
    for key, why in missing:
        print(f"# skipping {key}: {why}", file=sys.stderr)
    cols = [k for k, _ in located]
    if fmt == "csv":
        print("host_time,latency_ms," + ",".join(cols))
    n, last = 0, None
    for ts, latency, blocks in records(manifest, paths):
        if downsample and last is not None and ts - last < downsample:
            continue
        last = ts
        vals = {k: value_of(blocks, loc) for k, loc in located}
        if fmt == "csv":
            cells = [time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)), str(latency)]
            cells += [fmt_cell(k, vals[k]) for k in cols]
            print(",".join(cells))
        else:
            print(json.dumps({"host_time": ts, "latency_ms": latency, **vals}))
        n += 1
        if limit and n >= limit:
            break
    return n


def emit_balance(manifest, bundle, paths, downsample, limit):
    located, missing = resolve(manifest, bundle, BALANCE_KEYS)
    if missing:
        for key, why in missing:
            print(f"# missing {key}: {why}", file=sys.stderr)
    loc = dict(located)
    print("host_time,device_time,grid_kw,import_kw,export_kw,pv_kw,batt_kw,load_kw,"
          "self_consumption,exporting,grid_hz,lifetime_export_kwh")
    n, last, first_export = 0, None, None
    for ts, _, blocks in records(manifest, paths):
        if downsample and last is not None and ts - last < downsample:
            continue
        last = ts
        g = value_of(blocks, loc["plant_grid_sensor_active_power"]) \
            if "plant_grid_sensor_active_power" in loc else None
        if g is None:
            continue
        pv = value_of(blocks, loc.get("plant_sigen_photovoltaic_power")) or 0.0
        bt = value_of(blocks, loc.get("plant_ess_power")) or 0.0
        ld = value_of(blocks, loc.get("plant_general_load_power"))
        hz = value_of(blocks, loc.get("inverter_grid_frequency"))
        st = value_of(blocks, loc.get("plant_system_time"))
        ex_kwh = value_of(blocks, loc.get("plant_accumulated_grid_export_energy"))

        imp = max(g, 0.0)          # 30005 > 0 is buying from grid
        exp = max(-g, 0.0)         # 30005 < 0 is selling to grid
        # Self-consumed PV as a fraction of PV generated; undefined with no PV.
        sc = "" if pv <= 0 else f"{max(pv - exp, 0.0) / pv:.4f}"
        if exp > 0 and first_export is None:
            first_export = ts
        print(",".join([
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
            lib.device_local(st) if st else "",
            f"{g:g}", f"{imp:g}", f"{exp:g}", f"{pv:g}", f"{bt:g}",
            "" if ld is None else f"{ld:g}", sc,
            "1" if exp > 0 else "0",
            "" if hz is None else f"{hz:g}",
            "" if ex_kwh is None else f"{ex_kwh:g}",
        ]))
        n += 1
        if limit and n >= limit:
            break
    if first_export:
        print(f"# first export seen at "
              f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(first_export))}",
              file=sys.stderr)
    else:
        print("# no export in this window (grid power never went negative)",
              file=sys.stderr)
    return n


def emit_last(manifest, bundle, paths, keys):
    """Vertical view of the most recent record. The 'is it working right now?' view.

    Reports DATA freshness, not RECORD freshness. During an outage the logger keeps
    writing records on its probe schedule, but with mask 0 -- no blocks at all. An
    earlier version showed the age of that empty record and warned "capture may be
    stalled", which is precisely backwards: the capture is healthy and the device is
    not answering. So separate the two, and fall back to showing the last known good
    values rather than a column of blanks.
    """
    located, missing = resolve(manifest, bundle, keys)
    by_key = {f["key"]: f for g in bundle["groups"].values() for f in g}
    cadence = next((b["period_s"] for b in manifest["blocks"]
                    if b["addr"] <= 30000 < b["addr"] + b["count"]), 1)

    newest = last_data = None
    empties_since = 0
    for rec in records(manifest, paths):
        newest = rec
        if rec[2]:                     # any block present
            last_data, empties_since = rec, 0
        else:
            empties_since += 1
    if newest is None:
        print("no complete records yet")
        return 0

    now = time.time()
    rec_age = now - newest[0]
    if last_data is None:
        print(f"NO DATA in this archive at all. {empties_since} empty records, "
              f"newest {rec_age:.0f}s ago -- device unreachable for the whole span.")
        return 0

    data_age = now - last_data[0]
    if newest[2]:
        print(f"latest record  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(newest[0]))}"
              f"  ({data_age:.0f}s ago, tick latency {newest[1]} ms)")
        present = [b["label"] for b in manifest["blocks"] if b["index"] in newest[2]]
        print(f"blocks present {', '.join(present)}\n")
    else:
        print(f"!! DEVICE NOT ANSWERING -- no data for {data_age/60:.1f} min")
        print(f"   last good sample   "
              f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_data[0]))}")
        print(f"   logger is HEALTHY: still writing empty probe records, newest "
              f"{rec_age:.0f}s ago ({empties_since} consecutive empties)")
        print(f"   values below are the LAST KNOWN GOOD, not current\n")

    show = newest if newest[2] else last_data
    for key, loc in located:
        v = value_of(show[2], loc)
        unit = by_key.get(key, {}).get("unit") or ""
        flag = "" if v is not None else "   (not available)"
        print(f"  {key:<44}{fmt_cell(key, v):>22} {unit}{flag}")

    # Only a genuinely absent record means the capture itself has stopped. Allow
    # generous headroom, since degraded probing is much slower than the cadence.
    stall_after = max(4 * cadence, 90)
    if rec_age > stall_after:
        print(f"\n  WARNING: no record of ANY kind for {rec_age:.0f}s "
              f"(> {stall_after}s) -- the logger itself may have stalled. "
              f"Check status.sh and logs/sigen.err")
    return 1


def emit_check(manifest, bundle, paths):
    keys = ["plant_system_time", "plant_accumulated_grid_import_energy",
            "plant_accumulated_grid_export_energy"]
    located, _ = resolve(manifest, bundle, keys)
    loc = dict(located)
    series = {k: [] for k in loc}
    lat, host_ts, absent = [], [], {}
    n, empty = 0, 0
    for ts, latency, blocks in records(manifest, paths):
        n += 1
        host_ts.append(ts)
        # Latency on data records only. An empty record is an outage probe whose
        # "latency" is the socket timeout -- ~6.5 s, or 37 s before degraded mode
        # narrows the probe -- which would dominate max and skew p95.
        if blocks:
            lat.append(latency)
        else:
            empty += 1
        for b in manifest["blocks"]:
            if b["index"] not in blocks:
                absent[b["label"]] = absent.get(b["label"], 0) + 1
        for k, l in loc.items():
            v = value_of(blocks, l)
            if v is not None:
                series[k].append(v)

    print(f"records            {n}"
          + (f"  ({n - empty} with data, {empty} empty outage probes)" if empty
             else ""))
    if lat:
        print(f"latency ms         median {statistics.median(lat):.0f}  "
              f"p95 {lib.pct(lat, 0.95)}  max {max(lat)}"
              + ("   (data records only)" if empty else ""))
    back = sum(1 for a, b in zip(host_ts, host_ts[1:]) if b < a)
    if back:
        print(f"host clock         {back} BACKWARDS steps -- files decoded out of "
              f"order, or the host clock stepped")
    if host_ts:
        span = host_ts[-1] - host_ts[0]
        print(f"host span          {span:.0f}s over {n} records "
              f"({n/span if span else 0:.2f} rec/s)")
    st = series.get("plant_system_time", [])
    if st:
        # Expected step comes from the manifest's cadence for the block carrying
        # plant_system_time, so this stays correct if the fast tier is retuned.
        expect = next((b["period_s"] for b in manifest["blocks"]
                       if b["addr"] <= 30000 < b["addr"] + b["count"]), 1)
        steps = [round(y - x) for x, y in zip(st, st[1:])]
        exact = sum(1 for s in steps if s == expect)
        # A read lands wherever it lands relative to the device's internal second
        # tick, so at a cadence of 2 s or more, +-1 s steps are sampling jitter. They
        # come in compensating pairs and lose nothing. Anything larger is a real gap.
        jitter = [s for s in steps if s != expect and abs(s - expect) <= 1 and expect > 1]
        gaps = [s for s in steps if s != expect and s not in jitter]
        short, long_ = sum(1 for s in jitter if s < expect), sum(1 for s in jitter if s > expect)
        balanced = abs(short - long_) <= max(2, 0.2 * max(short, long_, 1))
        print(f"device clock       {len(st)} samples, {exact}/{len(steps)} steps "
              f"== {expect}s"
              + (f"  |  {len(jitter)} jitter ({short} short / {long_} long, "
                 f"{'balanced, no loss' if balanced else 'UNBALANCED - check for loss'})"
                 if jitter else "")
              + (f"  |  {len(gaps)} REAL GAPS {sorted(set(gaps))[:6]}" if gaps
                 else ("  OK" if not jitter else "  |  no gaps")))
    for k in ("plant_accumulated_grid_import_energy",
              "plant_accumulated_grid_export_energy"):
        s = series.get(k, [])
        if s:
            drops = sum(1 for a, b in zip(s, s[1:]) if b < a)
            print(f"{k[6:]:<34} {s[0]} -> {s[-1]}  "
                  f"{'OK (non-decreasing)' if not drops else f'{drops} DROPS'}")
    for label in sorted(absent):
        print(f"absent blocks      {label}: {absent[label]} records "
              f"(expected for slow/config tiers)")
    return n


def emit_latency(manifest, paths, bucket_min):
    """Latency by wall-clock bucket. Exists to tell oscillation from degradation.

    Reading a run of consecutive 5-minute heartbeats over a hand-picked window made
    an oscillating signal look like a monotonic climb, and a process restart look
    like a recovery. Both readings were wrong. So bucket the *whole* series, print
    the range and the spread, and state the trend explicitly instead of leaving it
    to the eye.

    Records with mask 0 are the logger's outage probes. Their tick latency is
    socket timeout, not device latency -- ~6.5 s against a dead host, which would
    swamp any bucket it landed in -- so they are counted separately rather than
    averaged in. Same data-versus-record distinction emit_last makes.
    """
    width = max(1, bucket_min) * 60
    buckets = {}
    for ts, latency, blocks in records(manifest, paths):
        slot = buckets.setdefault(int(ts // width) * width, {"lat": [], "empty": 0})
        if blocks:
            slot["lat"].append(latency)
        else:
            slot["empty"] += 1
    if not buckets:
        print("no records")
        return 0

    print(f"{'bucket start':<18}{'ticks':>7}{'empty':>7}{'median':>9}{'p95':>9}"
          f"{'max':>9}")
    medians = []
    for start in sorted(buckets):
        s = buckets[start]
        label = time.strftime("%Y-%m-%d %H:%M", time.localtime(start))
        if not s["lat"]:
            print(f"{label:<18}{0:>7}{s['empty']:>7}{'--':>9}{'--':>9}{'--':>9}"
                  f"   device not answering")
            continue
        med = statistics.median(s["lat"])
        medians.append(med)
        print(f"{label:<18}{len(s['lat']):>7}{s['empty']:>7}{med:>9.1f}"
              f"{lib.pct(s['lat'], 0.95):>9.1f}{max(s['lat']):>9.1f}")

    if not medians:
        print("\n  no bucket contained data — the device did not answer at any "
              "point in this span")
        return 0

    lo, hi = min(medians), max(medians)
    rising = len(medians) > 1 and all(x < y for x, y in zip(medians, medians[1:]))
    falling = len(medians) > 1 and all(x > y for x, y in zip(medians, medians[1:]))
    if len(medians) < 3:
        verdict = "too few buckets to judge a trend — widen the span"
    elif rising:
        verdict = "MONOTONICALLY RISING — the one shape that justifies acting"
    elif falling:
        verdict = "monotonically falling"
    else:
        verdict = "oscillating — no trend, do not act on a subrange of this"

    empty = sum(s["empty"] for s in buckets.values())
    print(f"\n  buckets        {len(medians)} with data of {len(buckets)} "
          f"({bucket_min} min each)")
    print(f"  median range   {lo:.1f} – {hi:.1f} ms"
          + (f"  ({hi / lo:.2f}x spread)" if lo else ""))
    print(f"  medians        " + " → ".join(f"{m:.0f}" for m in medians))
    print(f"  verdict        {verdict}")
    if empty:
        print(f"  excluded       {empty} empty probe records — device not "
              f"answering, so their latency is socket timeout, not device latency")
    return len(medians)


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        sys.exit(__doc__.strip())
    fmt, downsample, limit = "csv", 0, 0
    mode, fields, bucket_min = "rows", None, 10
    positional, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--fields":
            fields = args[i + 1].split(","); i += 2
        elif a == "--format":
            fmt = args[i + 1]; i += 2
        elif a == "--downsample":
            downsample = int(args[i + 1]); i += 2
        elif a == "--limit":
            limit = int(args[i + 1]); i += 2
        elif a == "--balance":
            mode = "balance"; i += 1
        elif a == "--check":
            mode = "check"; i += 1
        elif a == "--last":
            mode = "last"; i += 1
        elif a == "--latency":
            mode = "latency"
            if i + 1 < len(args) and args[i + 1].isdigit():
                bucket_min = int(args[i + 1]); i += 2
            else:
                i += 1
        elif a == "--all":
            mode = "all"; i += 1
        else:
            positional.append(a); i += 1

    with open(positional[0]) as fh:
        manifest = json.load(fh)
    paths = positional[1:]
    if not paths:
        sys.exit("no .bin/.bin.gz files given")
    if manifest.get("format") != FORMAT:
        sys.exit(f"unknown archive format {manifest.get('format')!r}")
    # Record filenames carry the block-plan fingerprint. Decoding a file written
    # under a different plan would misread cadences and possibly field offsets, so
    # refuse rather than emit plausible-looking wrong numbers.
    check_plan_hash(manifest, paths)
    paths = sort_chronologically(paths)
    bundle = dump.load_regmap()

    if mode == "check":
        emit_check(manifest, bundle, paths)
    elif mode == "latency":
        emit_latency(manifest, paths, bucket_min)
    elif mode == "last":
        emit_last(manifest, bundle, paths, fields or DEFAULT_FIELDS)
    elif mode == "balance":
        emit_balance(manifest, bundle, paths, downsample, limit)
    else:
        keys = covered_fields(manifest, bundle) if mode == "all" \
            else (fields or DEFAULT_FIELDS)
        emit_rows(manifest, bundle, paths, keys, fmt, downsample, limit)


if __name__ == "__main__":
    main()
