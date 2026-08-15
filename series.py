#!/usr/bin/env python3
"""
Windowed aggregation over a raw archive, for the local viewer. Stdlib only.

Not a CLI. Imported by serve.py, which is run from this directory so a plain
`import series` resolves. Offline by construction: this module imports the decode
half of the toolchain and never constructs lib.Modbus -- the device already has
exactly one client (log.py), and tests/ asserts that this file cannot become a
second one.

What it adds on top of decode.py, which already knows how to turn bytes into
values:

  - an index of which file covers which span, so a 6-hour view reads 6 files
    rather than a year of them;
  - bucket aggregation to min/mean/max, so a plot of 10,800 records is ~700
    points that still show the spikes;
  - a per-file summary cache, since a rotated .bin.gz can never change;
  - a header-only health scan that keeps the three failure modes apart --
    device not answering, no records at all, and everything fine (FINDINGS 7).

Conventions worth knowing before changing anything here:

  - Buckets are keyed on ABSOLUTE epoch (ts // bucket_s), never on an offset from
    the window start. A bucket that straddles a file boundary therefore merges
    exactly from both files, and panning the window does not re-slice the data.
  - Bucket widths come from BUCKET_LADDER, not from span/700 directly, so panning
    and re-polling land on cache entries that already exist.
  - Files are grouped by the plan hash in their filename and each group is
    decoded with its own manifest. decode.check_plan_hash() is deliberately not
    used: it exits the process, which is not something a server may do. Grouping
    makes a mixed-plan decode impossible instead of fatal.
"""
import json
import os
import re
import statistics
import struct
import threading
import time
from collections import OrderedDict

import decode
import dump
import lib

MANIFEST_RE = re.compile(r"-([0-9a-f]{8})\.manifest\.json$")

# Bucket widths, coarsest-last. A discrete ladder is the point: pan or re-poll and
# the request lands on the same widths, so the per-file cache keeps hitting.
BUCKET_LADDER = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600,
                 7200, 21600, 86400)
# Aim for this many buckets, then snap DOWN to a ladder width. 900 is chosen so
# the common windows land on round widths: 6 h -> 30 s, 24 h -> 120 s, 7 d -> 900 s.
TARGET_BUCKETS = 900

# Decode at most this many records per bucket. Above it, records are strided.
# This is what keeps a month-long window as cheap as a six-hour one: work is
# bounded by bucket count, not by span. 64 samples is still a sound min/max for a
# bucket, and the stride is reported to the caller -- never applied silently.
SAMPLES_PER_BUCKET = 64

# How long a request may spend on files whose summaries are cold before it returns
# what it has and hands the rest to the background warmer. Files are read
# newest-first, so a partial answer is the recent end of the window rather than a
# random slice, and the page labels itself partial and re-asks. Deliberately short:
# a first 24-hour view over 25 files measured 8.5 s on the reference host, and eight
# seconds of niced CPU next to a logger with a 2 s tick is worse than three paints.
WARM_BUDGET_S = 3.0

# Accumulator slots. Chosen so that merging two of them is exact for every slot,
# which is what lets a straddling bucket be assembled from two files.
N, SUM, MIN, MAX, FIRST, FIRST_TS, LAST, LAST_TS, BITS = range(9)

# Same quantity under two names (FINDINGS 7, "duplicate registers"). Plotting both
# draws one line twice, so the catalogue marks the copy and the picker hides it.
# Value is the key to prefer.
DUPLICATE_OF = {
    "plant_total_load_power": "plant_general_load_power",
    "inverter_active_power": "plant_active_power",
    "inverter_ess_max_battery_charge_power": "plant_ess_available_max_charging_power",
    "inverter_ess_charge_discharge_power": "plant_ess_power",
    "inverter_ess_battery_soc": "plant_ess_soc",
    "inverter_ess_average_cell_temperature": "plant_ess_average_cell_temperature",
    "plant_total_charged_energy_of_the_ess_2": "plant_accumulated_battery_charge_energy",
    "plant_total_discharged_energy_of_the_ess_2":
        "plant_accumulated_battery_discharge_energy",
    "plant_total_imported_energy_2": "plant_accumulated_grid_import_energy",
    "plant_total_exported_energy_2": "plant_accumulated_grid_export_energy",
    "plant_total_generation_of_third_party_inverter_2":
        "plant_total_generation_of_third_party_inverter",
    "plant_total_charged_energy_of_the_evdc_2": "plant_total_charged_energy_of_the_evdc",
    "plant_total_discharged_energy_of_the_evdc_2":
        "plant_total_discharged_energy_of_the_evdc",
    "plant_total_energy_output_of_oil_fueled_generator_2":
        "plant_total_energy_output_of_oil_fueled_generator",
}


# --------------------------------------------------------------------- indexing

class FileSpan:
    """One archive file and the span it covers.

    first_ts comes from the first record's own header, not from the filename: the
    stamp in the name is host-local wall clock, and parsing it back would guess at
    a DST fold once a year. The name is used only for ordering, via
    decode.sort_chronologically().

    last_ts is the NEXT file's first_ts, so spans tile the archive with no
    invented holes -- a genuine hole shows up as buckets with no records, which is
    what it is.
    """

    __slots__ = ("path", "name", "first_ts", "last_ts", "size", "mtime", "gz")

    def __init__(self, path, first_ts, size, mtime):
        self.path = path
        self.name = os.path.basename(path)
        self.first_ts = first_ts
        self.last_ts = None
        self.size = size
        self.mtime = mtime
        self.gz = path.endswith(".gz")

    def overlaps(self, start, end):
        if self.first_ts is None:
            return False
        return self.first_ts < end and (self.last_ts is None or self.last_ts > start)


def first_record_ts(path):
    """Host epoch of a file's first record, or None if it holds no whole record.

    Anything unreadable -- pruned between listdir and open, or a truncated gzip --
    drops out of the index instead of taking it down. One bad file must not make the
    whole archive unviewable.
    """
    try:
        with decode.open_maybe_gz(path) as fh:
            head = fh.read(decode.HEADER_LEN)
    except Exception:
        return None
    if len(head) < decode.HEADER_LEN:
        return None                    # 0-byte file: just rotated, nothing in it yet
    return struct.unpack(decode.HEADER, head)[0]


class Series:
    """Every archive file written under one block plan, plus its manifest.

    One Series is the unit that can be decoded together. Two plans in the same
    directory are two Series and are never merged.
    """

    def __init__(self, data_dir, plan_hash, manifest_path, paths):
        self.data_dir = data_dir
        self.plan_hash = plan_hash
        self.manifest_path = manifest_path
        self.paths = paths
        self._manifest = None
        self._spans = None
        self._sig = None               # (name, size, mtime) of every file, for staleness
        self._ts_cache = {}
        self._lock = threading.Lock()

    # -- manifest ----------------------------------------------------------

    @property
    def manifest(self):
        if self._manifest is None:
            with open(self.manifest_path) as fh:
                self._manifest = json.load(fh)
            if self._manifest.get("format") != decode.FORMAT:
                raise ValueError(
                    f"{os.path.basename(self.manifest_path)}: unknown archive format "
                    f"{self._manifest.get('format')!r}")
        return self._manifest

    @property
    def fast_period_s(self):
        return self.manifest.get("fast_period_s") or 1

    def slow_block_indices(self):
        """Blocks that fire less often than the fast tier.

        Records carrying one of these are never strided away: they arrive once a
        minute or once every five, so index-based striding could alias them out
        entirely and a slow-tier field would silently read as absent.
        """
        fast = self.fast_period_s
        return frozenset(b["index"] for b in self.manifest["blocks"]
                         if b["period_s"] > fast)

    # -- index -------------------------------------------------------------

    def spans(self):
        """[FileSpan] oldest-first, re-read when the directory changes."""
        with self._lock:
            stats = []
            for p in decode.sort_chronologically(self.paths):
                try:
                    st = os.stat(p)
                except OSError:
                    continue           # pruned or rotated out from under us
                stats.append((p, st.st_size, st.st_mtime))
            sig = tuple((os.path.basename(p), s, m) for p, s, m in stats)
            if sig == self._sig and self._spans is not None:
                return self._spans

            spans = []
            for path, size, mtime in stats:
                key = (path, size, mtime)
                if key not in self._ts_cache:
                    self._ts_cache[key] = first_record_ts(path)
                ts = self._ts_cache[key]
                if ts is None:
                    continue           # nothing decodable in it yet
                spans.append(FileSpan(path, ts, size, mtime))
            # A file's span ends where the next one begins; the newest is open.
            for a, b in zip(spans, spans[1:]):
                a.last_ts = b.first_ts
            if len(self._ts_cache) > 4096:
                self._ts_cache = {(p, s, m): self._ts_cache[(p, s, m)]
                                  for p, s, m in stats if (p, s, m) in self._ts_cache}
            self._sig, self._spans = sig, spans
            return spans

    def files_for(self, start, end):
        return [s for s in self.spans() if s.overlaps(start, end)]

    def extent(self):
        """(first_ts, last_ts_known). The end is the newest file's first record --
        the true end needs a scan, which latest() does."""
        spans = self.spans()
        if not spans:
            return None, None
        return spans[0].first_ts, spans[-1].last_ts or spans[-1].first_ts

    def newest_first(self):
        return list(reversed(self.spans()))


def discover(data_dir):
    """{plan_hash: Series} for the archives directly in data_dir.

    Non-recursive on purpose: a retune's old series is moved into a subdirectory
    with its own manifest (see README, "Archive format"), and mixing the two is
    exactly what the plan hash exists to prevent. Point --data-dir at the
    subdirectory to view one of those.
    """
    try:
        names = os.listdir(data_dir)
    except OSError:
        return {}
    manifests, paths = {}, {}
    for name in sorted(names):
        full = os.path.join(data_dir, name)
        m = MANIFEST_RE.search(name)
        if m:
            # Any manifest for a hash describes the same plan -- that is what the
            # hash covers -- so the newest is as good as the first.
            manifests[m.group(1)] = full
            continue
        m = decode.HASH_RE.search(name)
        if m and (name.endswith(".bin") or name.endswith(".bin.gz")):
            paths.setdefault(m.group(1), []).append(full)
    out = {}
    for plan_hash, group in paths.items():
        if plan_hash not in manifests:
            continue                   # no manifest, no format of record, no decode
        out[plan_hash] = Series(data_dir, plan_hash, manifests[plan_hash], group)
    return out


def newest_series(data_dir):
    """The Series holding the most recent record, or None. What the viewer opens."""
    found = discover(data_dir)
    best, best_ts = None, None
    for s in found.values():
        _, last = s.extent()
        if last is not None and (best_ts is None or last > best_ts):
            best, best_ts = s, last
    return best


# ------------------------------------------------------------------ bucket sizing

def choose_bucket(span_s, floor_s=1):
    """Snap a window to a ladder width targeting ~TARGET_BUCKETS points."""
    want = max(float(floor_s), span_s / float(TARGET_BUCKETS))
    for b in BUCKET_LADDER:
        if b >= want:
            return b
    return BUCKET_LADDER[-1]


def stride_for(bucket_s, cadence_s):
    """Decode at most SAMPLES_PER_BUCKET records per bucket.

    Depends only on the bucket width and the manifest's cadence, never on the
    window -- so a file's summary is the same whichever window asked for it, and
    the cache stays valid.
    """
    per_bucket = max(1, int(bucket_s // max(1, cadence_s)))
    # Ceiling division, not floor: flooring leaves the bound broken for widths that
    # do not divide evenly (600 s buckets at 0.5 Hz gave stride 4, so 75 samples a
    # bucket, not 64) and the guarantee this function exists to make is an upper
    # bound on work per bucket.
    return max(1, -(-per_bucket // SAMPLES_PER_BUCKET))


# ----------------------------------------------------------------- summary cache

class SummaryCache:
    """LRU of per-file bucket summaries.

    Keyed by (path, size, mtime, bucket_s, stride, field). A rotated .bin.gz never
    changes, so its entry is valid until eviction; the open .bin changes size
    every record, which invalidates its own entries and nobody else's.

    Storing one entry PER FIELD rather than per field-set means adding a field to
    a custom chart costs one field's worth of work, not a re-summary of
    everything. The pass that fills them decodes all the missing fields at once.
    """

    def __init__(self, max_entries=8000):
        self.max_entries = max_entries
        self._d = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                self.hits += 1
                return self._d[key]
            self.misses += 1
            return None

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.max_entries:
                self._d.popitem(last=False)

    def stats(self):
        with self._lock:
            return {"entries": len(self._d), "hits": self.hits, "misses": self.misses}


HEALTH_KEY = "__health__"
RESUME_KEY = "__resume__"     # how far into the open file a summary has read


def _acc_add(acc, ts, v):
    if acc[N] == 0:
        acc[MIN] = acc[MAX] = acc[FIRST] = acc[LAST] = v
        acc[FIRST_TS] = acc[LAST_TS] = ts
    else:
        if v < acc[MIN]:
            acc[MIN] = v
        if v > acc[MAX]:
            acc[MAX] = v
        if ts < acc[FIRST_TS]:
            acc[FIRST], acc[FIRST_TS] = v, ts
        if ts >= acc[LAST_TS]:
            acc[LAST], acc[LAST_TS] = v, ts
    acc[N] += 1
    acc[SUM] += v
    if isinstance(v, int):
        acc[BITS] |= v          # bitmask OR, so an alarm bit set briefly survives
    return acc


def _acc_merge(a, b):
    """Exact for every slot -- which is why a straddling bucket can be assembled."""
    if a is None:
        return list(b)
    if b is None:
        return a
    if b[N] == 0:
        return a
    if a[N] == 0:
        return list(b)
    out = list(a)
    out[N] = a[N] + b[N]
    out[SUM] = a[SUM] + b[SUM]
    out[MIN] = min(a[MIN], b[MIN])
    out[MAX] = max(a[MAX], b[MAX])
    out[BITS] = a[BITS] | b[BITS]
    if b[FIRST_TS] < a[FIRST_TS]:
        out[FIRST], out[FIRST_TS] = b[FIRST], b[FIRST_TS]
    if b[LAST_TS] >= a[LAST_TS]:
        out[LAST], out[LAST_TS] = b[LAST], b[LAST_TS]
    return out


def _health_merge(a, b):
    out = {"n": a["n"] + b["n"], "empty": a["empty"] + b["empty"],
           "lat": (a["lat"] + b["lat"])[:SAMPLES_PER_BUCKET],
           "lat_max": max(a["lat_max"], b["lat_max"]),
           "first_ts": min(a["first_ts"], b["first_ts"]),
           "last_ts": max(a["last_ts"], b["last_ts"])}
    return out


def summarise_file(series, span, keys, bucket_s, cache=None):
    """{field -> {bucket -> acc}} plus {HEALTH_KEY -> {bucket -> health}} for one file.

    Cached per field, so a second call asking for one extra field decodes only
    that field. Health is always computed: it is header-only and free.

    The OPEN file is summarised incrementally. It grows every couple of seconds, so
    keying its cache entry on size would invalidate the whole thing on every poll
    and re-decode up to an hour of records to learn what the last five said --
    measured at 0.70 s per 10 s poll on the reference host, which is real load on a
    machine whose logger has a 2 s tick to hit. Records are append-only, so the
    summary carries the byte offset it reached and resumes from there.
    """
    stride = stride_for(bucket_s, series.fast_period_s)
    # A rotated .bin.gz can never change, so identity is (size, mtime) and the entry
    # is final. The open .bin is identified without them, and extended in place.
    growing = not span.gz and span.last_ts is None
    base = ((span.path, bucket_s, stride) if growing
            else (span.path, span.size, span.mtime, bucket_s, stride))
    out, missing = {}, []
    for key in keys:
        hit = cache.get(base + (key,)) if cache else None
        if hit is None:
            missing.append(key)
        else:
            out[key] = hit
    health = cache.get(base + (HEALTH_KEY,)) if cache else None
    resume = cache.get(base + (RESUME_KEY,)) if cache else None
    start_at, first_index = 0, 0

    if not growing:
        if not missing and health is not None:
            return dict(out, **{HEALTH_KEY: health}), False
    else:
        # Every field of the open file must share one read offset, or a field added
        # mid-poll would leave the others stranded at the old offset and they would
        # silently skip the records in between. So either they all resume, or they
        # are all rebuilt together.
        have_all = health is not None and resume is not None and not missing
        if have_all and resume["offset"] >= span.size:
            return dict(out, **{HEALTH_KEY: health}), False
        if have_all and resume["offset"] < span.size:
            start_at, first_index = resume["offset"], resume["count"]
        missing = list(keys)

    manifest = series.manifest
    located, _ = decode.resolve(manifest, bundle(), missing)
    # Copy on resume: cached accumulators are shared with whoever already read them.
    fresh = {k: {b: list(a) for b, a in (cache.get(base + (k,)) or {}).items()}
             if start_at else {} for k, _ in located}
    health = ({b: dict(h, lat=list(h["lat"])) for b, h in health.items()}
              if (start_at and health) else {})
    consumed, i = start_at, first_index - 1
    slow = series.slow_block_indices()
    for ts, latency, blocks in decode.records_from(manifest, span.path, start_at):
        i += 1
        consumed += decode.HEADER_LEN + sum(len(b) for b in blocks.values())
        bkey = int(ts // bucket_s) * bucket_s
        h = health.get(bkey)
        if h is None:
            h = health[bkey] = {"n": 0, "empty": 0, "lat": [], "lat_max": 0,
                                "first_ts": ts, "last_ts": ts}
        h["n"] += 1
        h["last_ts"] = ts
        if blocks:
            # Latency over records that RETURNED DATA. An outage probe's latency is
            # the socket timeout -- 6.5 s, or 37 s while degraded -- and averaging
            # those in reports a dead device as a catastrophically slow one
            # (FINDINGS 7). Max is kept exact because a real 1147 ms outlier is
            # the number worth seeing; median/p95 come from up to
            # SAMPLES_PER_BUCKET of them.
            if latency > h["lat_max"]:
                h["lat_max"] = latency
            if len(h["lat"]) < SAMPLES_PER_BUCKET:
                h["lat"].append(latency)
        else:
            h["empty"] += 1
        if not blocks or not located:
            continue
        if stride > 1 and i % stride and not (slow & blocks.keys()):
            continue
        for key, loc in located:
            v = decode.value_of(blocks, loc)
            if v is None or isinstance(v, str):
                continue               # sentinel, absent block, or a STRING field
            acc = fresh[key].get(bkey)
            fresh[key][bkey] = _acc_add(list(acc) if acc else [0, 0, None, None, None,
                                                               None, None, None, 0],
                                        ts, v)

    for key in missing:
        got = fresh.get(key, {})
        out[key] = got
        if cache:
            cache.put(base + (key,), got)
    out[HEALTH_KEY] = health
    if cache:
        cache.put(base + (HEALTH_KEY,), health)
        if growing:
            # Where to pick up next poll: the byte after the last COMPLETE record,
            # and the record index, since striding counts from the file's start.
            cache.put(base + (RESUME_KEY,), {"offset": consumed, "count": i + 1})
    return out, True


_BUNDLE = None
_BUNDLE_LOCK = threading.Lock()


def bundle():
    """The register map, loaded once. Big enough that per-request loading shows."""
    global _BUNDLE
    with _BUNDLE_LOCK:
        if _BUNDLE is None:
            _BUNDLE = dump.load_regmap()
        return _BUNDLE


# ------------------------------------------------------------------- the window

def _grid(start, end, bucket_s):
    lo = int(start // bucket_s) * bucket_s
    hi = int(end // bucket_s) * bucket_s
    return [lo + i * bucket_s for i in range(int((hi - lo) // bucket_s) + 1)]


def _runs(flags, grid, bucket_s):
    """Contiguous [t0, t1] spans where flags[i] is true."""
    out, run = [], None
    for i, f in enumerate(flags):
        if f and run is None:
            run = grid[i]
        elif not f and run is not None:
            out.append([run, grid[i]])
            run = None
    if run is not None:
        out.append([run, grid[-1] + bucket_s])
    return out


def tz_runs(grid):
    """[[ts, utc_offset_s, name]] change points across the window.

    The archive's filenames, heartbeats and everything else in this repo are in
    the capture host's local time, so the page labels axes that way too. Sending
    change points rather than one offset means a window straddling a DST switch
    labels both halves correctly instead of shifting one by an hour.
    """
    out = []
    for ts in grid:
        lt = time.localtime(ts)
        off = lt.tm_gmtoff if lt.tm_gmtoff is not None else -time.timezone
        name = time.strftime("%Z", lt)
        if not out or out[-1][1] != off:
            out.append([ts, off, name])
    return out


def window(series, start, end, keys, bucket_s=None, cache=None,
           warm_budget_s=WARM_BUDGET_S, warmer=None, end_exclusive=False):
    """Bucketed min/mean/max for `keys` over [start, end).

    Files are read NEWEST FIRST so that when the budget runs out, what came back
    is the recent end of the window rather than a random slice of it. Whatever is
    left is reported as pending_files and handed to `warmer`; the caller can say
    so and re-ask. Nothing is silently dropped.

    `end_exclusive` drops the bucket starting exactly at `end`. _grid() is
    END-INCLUSIVE, which is right for the viewer -- a window ending "now" should
    show the bucket now falls in -- but wrong for tiling: two adjacent spans would
    both contain the boundary bucket, so concatenating them duplicates a point, and
    a counter's last_value would be read from the span after this one. Dropping it
    HERE rather than truncating the arrays afterwards is what makes the counters
    exact: first_value, last_value and reset are derived below from the accumulators
    this index selects.

    Only meaningful when `end` is on a bucket boundary. When it is not, the final
    bucket is genuinely part of the span and is kept either way.
    """
    keys = [k for k in dict.fromkeys(keys)]
    span_s = max(1.0, end - start)
    if bucket_s is None:
        bucket_s = choose_bucket(span_s, series.fast_period_s)
    grid = _grid(start, end, bucket_s)
    if end_exclusive and len(grid) > 1 and end % bucket_s == 0:
        grid.pop()
    index = {t: i for i, t in enumerate(grid)}

    # Files are still selected over the FULL span: a file whose first record lands in
    # the dropped bucket still holds records belonging to the buckets we are keeping.
    order = series.files_for(start, end)[::-1]     # newest first
    merged = {k: {} for k in keys}
    health = {}
    pending, read, decoded = [], [], 0
    t0 = time.perf_counter()
    unreadable = []
    for i, span in enumerate(order):
        try:
            part, did_work = summarise_file(series, span, keys, bucket_s, cache)
        except Exception as e:
            # keep_days prunes on rotation, so a file can vanish between the index
            # and the read. Report it rather than failing the whole window.
            unreadable.append({"file": span.name, "error": str(e)})
            continue
        read.append(span)
        decoded += 1 if did_work else 0
        for k in keys:
            dst = merged[k]
            for bkey, acc in part.get(k, {}).items():
                if bkey in index:
                    dst[bkey] = _acc_merge(dst.get(bkey), acc)
        for bkey, h in (part.get(HEALTH_KEY) or {}).items():
            if bkey in index:
                health[bkey] = (_health_merge(health[bkey], h) if bkey in health
                                else dict(h))
        if (warm_budget_s and time.perf_counter() - t0 > warm_budget_s
                and i + 1 < len(order)):
            pending = order[i + 1:]
            break

    if pending and warmer is not None:
        warmer.submit(series, pending, keys, bucket_s)

    n = len(grid)
    series_out = {}
    by_key = _fields_by_key()
    for k in keys:
        acc_by_bucket = merged[k]
        col = {"unit": (by_key.get(k, {}).get("unit") or ""),
               "cadence_s": _cadence_of_field(series, k),
               "n": [0] * n, "mean": [None] * n, "min": [None] * n,
               "max": [None] * n, "last": [None] * n, "bits": [0] * n}
        for bkey, acc in acc_by_bucket.items():
            i = index[bkey]
            col["n"][i] = acc[N]
            col["mean"][i] = acc[SUM] / acc[N] if acc[N] else None
            col["min"][i] = acc[MIN]
            col["max"][i] = acc[MAX]
            col["last"][i] = acc[LAST]
            col["bits"][i] = acc[BITS]
        ordered = sorted(acc_by_bucket.items())
        col["first_value"] = ordered[0][1][FIRST] if ordered else None
        col["last_value"] = ordered[-1][1][LAST] if ordered else None
        col["first_ts"] = ordered[0][1][FIRST_TS] if ordered else None
        col["last_ts"] = ordered[-1][1][LAST_TS] if ordered else None
        # A lifetime counter that steps backwards is the device losing an unflushed
        # increment across a power cut, not corruption (FINDINGS 11). Flag it here
        # so a window total can say so rather than showing negative kWh.
        col["reset"] = any(b[1][FIRST] is not None and a[1][LAST] is not None
                           and b[1][FIRST] < a[1][LAST]
                           for a, b in zip(ordered, ordered[1:]))
        series_out[k] = col

    # Only files actually read count as covered. A pending file's buckets hold no
    # records yet, and calling that "the logger was down" would be a lie about the
    # archive rather than a statement about the request.
    covered = [[s.first_ts, s.last_ts if s.last_ts is not None else end]
               for s in read]
    lat_med, lat_p95, lat_max = [None] * n, [None] * n, [None] * n
    recs, empties = [0] * n, [0] * n
    for bkey, h in health.items():
        i = index[bkey]
        recs[i], empties[i] = h["n"], h["empty"]
        if h["lat"]:
            lat_med[i] = statistics.median(h["lat"])
            lat_p95[i] = lib.pct(h["lat"], 0.95)
            lat_max[i] = h["lat_max"]

    def inside(t):
        return any(a is not None and a <= t < b for a, b in covered)

    no_data = [recs[i] > 0 and recs[i] == empties[i] for i in range(n)]
    no_recs = [recs[i] == 0 and inside(grid[i]) for i in range(n)]

    return {
        "start": start, "end": end, "bucket_s": bucket_s,
        "stride": stride_for(bucket_s, series.fast_period_s),
        "samples_per_bucket": SAMPLES_PER_BUCKET,
        "plan_hash": series.plan_hash,
        "t": grid,
        "series": series_out,
        "health": {
            "records": recs, "empty": empties,
            "latency_median": lat_med, "latency_p95": lat_p95, "latency_max": lat_max,
            "no_data": _runs(no_data, grid, bucket_s),
            "no_records": _runs(no_recs, grid, bucket_s),
            "covered": covered,
            "note": ("latency is over records that returned data; an outage probe's "
                     "latency is the socket timeout, not the device's. Median and "
                     f"p95 use up to {SAMPLES_PER_BUCKET} records per bucket; max is "
                     "exact."),
        },
        "records": sum(recs),
        "files": len(order),
        "files_read": len(read),
        "files_decoded": decoded,
        "unreadable": unreadable,
        "pending_files": len(pending),
        "tz": tz_runs(grid),
        "took_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def energy(win, keys):
    """Window totals from the lifetime counters: last - first, per FINDINGS.

    The counters are the independent cross-check on any integrated power figure --
    they agree to about a watt-hour when the decode is right -- so the totals on
    screen come from them rather than from summing the power series.
    """
    out = {}
    for k in keys:
        col = win["series"].get(k)
        if not col or col["first_value"] is None or col["last_value"] is None:
            continue
        delta = col["last_value"] - col["first_value"]
        out[k] = {"kwh": max(0.0, round(delta, 3)),
                  "first": col["first_value"], "last": col["last_value"],
                  "reset": bool(col["reset"] or delta < 0),
                  "span_s": (col["last_ts"] or 0) - (col["first_ts"] or 0)}
    return out


# ------------------------------------------------------------------------ latest

def _tail_of_file(manifest, path):
    """(newest record, newest data record, trailing empties, record count).

    A whole-file scan, because records are variable length -- the bitmask decides
    how long each one is -- so there is nothing to seek to.
    """
    newest = last_data = None
    trailing = total = 0
    for rec in decode.records(manifest, [path]):
        total += 1
        newest = rec
        if rec[2]:
            last_data, trailing = rec, 0
        else:
            trailing += 1
    return newest, last_data, trailing, total


def latest(series, keys, max_files=6):
    """The newest record, and the newest one that carried data. They differ.

    Reports DATA freshness as well as RECORD freshness, because during an outage
    the logger keeps writing header-only records on its probe schedule: the
    capture is healthy and the device is not (FINDINGS 7). Same distinction
    decode.emit_last() draws, returned as values instead of printed.

    Scans newest-first and stops as soon as it has a data record, which is why
    max_files exists at all: the current file can hold nothing but probes, and a
    multi-hour outage can leave several such files. Six covers six hours of
    outage; past that the values are old enough that the age is the story.
    """
    manifest = series.manifest
    located, missing = decode.resolve(manifest, bundle(), keys)
    newest = last_data = None
    empties_since = 0
    scanned = 0
    for span in series.newest_first()[:max_files]:
        newest_here, data_here, trailing, total = _tail_of_file(manifest, span.path)
        scanned += 1
        if newest is None and newest_here is not None:
            newest = newest_here
        if data_here is not None:
            last_data = data_here
            empties_since += trailing
            break
        # Nothing but probes in this file: every record in it postdates the last
        # good sample, so they all count toward the outage.
        empties_since += total

    now = time.time()
    if newest is None:
        return {"ok": False, "reason": "no complete records in this archive yet",
                "now": now, "files_scanned": scanned}

    cadence = series.fast_period_s
    # Only a genuinely absent record means the capture stopped. Degraded probing is
    # far slower than the cadence, so allow generous headroom -- same rule as
    # decode.emit_last().
    stall_after = max(4 * cadence, 90)
    values, units = {}, {}
    by_key = _fields_by_key()
    if last_data is not None:
        for key, loc in located:
            values[key] = decode.value_of(last_data[2], loc)
            units[key] = by_key.get(key, {}).get("unit") or ""
    present = [b["label"] for b in manifest["blocks"] if b["index"] in newest[2]]
    return {
        "ok": last_data is not None,
        "now": now,
        "record_ts": newest[0],
        "record_age_s": now - newest[0],
        "latency_ms": newest[1],
        "data_ts": last_data[0] if last_data else None,
        "data_age_s": (now - last_data[0]) if last_data else None,
        "device_answering": bool(newest[2]),
        "logger_stalled": (now - newest[0]) > stall_after,
        "stall_after_s": stall_after,
        "empties_since": empties_since,
        "blocks_present": present,
        "values": values,
        "units": units,
        "missing": [k for k, _ in missing],
        "plan_hash": series.plan_hash,
        "files_scanned": scanned,
    }


# ----------------------------------------------------------------- field catalog

_FIELDS_BY_KEY = None


def _fields_by_key():
    global _FIELDS_BY_KEY
    if _FIELDS_BY_KEY is None:
        _FIELDS_BY_KEY = {f["key"]: f
                          for g in bundle()["groups"].values() for f in g}
    return _FIELDS_BY_KEY


def _cadence_of_field(series, key):
    f = _fields_by_key().get(key)
    if not f:
        return series.fast_period_s
    for b in series.manifest["blocks"]:
        if b["addr"] <= f["addr"] and f["addr"] + f["count"] <= b["addr"] + b["count"]:
            return b["period_s"]
    return series.fast_period_s


_CATALOG = {}


def catalog(series):
    """Every field the captured blocks cover, with what a plot needs to know.

    This is the answer to "what can I chart?" and it is derived from the manifest,
    so widening a block widens the picker with no code change -- the point of
    capture-wide-decode-narrow.

    Memoised per plan: it parses ~200 register descriptions for their inline enums,
    which is cheap once and wasteful on every request.
    """
    if series.plan_hash in _CATALOG:
        return _CATALOG[series.plan_hash]
    reg = bundle()
    by_key = _fields_by_key()
    out = []
    for key in decode.covered_fields(series.manifest, reg):
        f = by_key[key]
        if lib.dtype_of(f) == "STRING":
            continue                   # nothing to plot
        item = {"key": key, "unit": f["unit"] or "", "dtype": lib.dtype_of(f),
                "addr": f["addr"], "gain": f["gain"],
                "cadence_s": _cadence_of_field(series, key),
                "desc": (f["desc"] or "").strip()[:300]}
        labels = enum_labels(key)
        if labels:
            item["enum"] = labels
        if key in dump.ALARM_TABLE_FOR:
            item["alarm_table"] = dump.ALARM_TABLE_FOR[key]
        out.append(item)
    out.sort(key=lambda x: x["key"])
    # Mark a field as a duplicate only when the register it duplicates is itself
    # captured. Otherwise the picker would hide the only copy this archive has:
    # 30196 (third-party inverter generation) is outside every block, so its _2
    # twin at 30240 is the one that exists here.
    offered = {item["key"] for item in out}
    for item in out:
        dupe = DUPLICATE_OF.get(item["key"])
        if dupe and dupe in offered:
            item["duplicate_of"] = dupe
    _CATALOG[series.plan_hash] = out
    return out


def enum_labels(key):
    """{code: label} for a state register, from the appendix or the description.

    Reuses dump.py's two sources so the page names a state exactly as dump.py
    does -- there is one spelling of "RUNNING" in this repo, not two.
    """
    name = dump.ENUM_FOR.get(key)
    if name:
        return dict(bundle().get("enums", {}).get(name, {}))
    f = _fields_by_key().get(key)
    if not f or lib.dtype_of(f) not in ("U16", "S16") or "alarm" in key:
        return {}
    if f["gain"] not in (1, None):
        return {}
    return {str(k): v for k, v in dump.enum_map(f["desc"] or "").items()}


def alarm_bits(key, raw):
    """['Off-grid protection', ...] for the bits set in an alarm word."""
    table = bundle()["alarm_codes"].get(dump.ALARM_TABLE_FOR.get(key, ""), {})
    if not raw:
        return []
    return [table.get(str(b), f"bit{b}") for b in range(16) if int(raw) >> b & 1]


# ------------------------------------------------------------------- background

class Warmer:
    """One background thread filling the cache for files a request ran out of time
    for. One thread deliberately: the logger is the latency-sensitive process on
    this host, and this is a viewer.

    `lock` is shared with the request path, so warming and serving never decode at
    the same time -- together they are bounded to one core.
    """

    def __init__(self, cache, lock=None):
        self.cache = cache
        self.lock = lock or threading.RLock()
        self._q = []
        self._seen = set()
        self._cv = threading.Condition()
        self._thread = None
        self.done = 0

    def submit(self, series, spans, keys, bucket_s):
        with self._cv:
            if len(self._seen) > 20000:
                self._seen.clear()     # bounded: this runs for months at a time
            for span in spans:
                job = (span.path, span.size, span.mtime, bucket_s, tuple(sorted(keys)))
                if job in self._seen:
                    continue
                self._seen.add(job)
                self._q.append((series, span, list(keys), bucket_s))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="sigen-warm")
                self._thread.start()
            self._cv.notify_all()

    def pending(self):
        with self._cv:
            return len(self._q)

    def _run(self):
        while True:
            with self._cv:
                if not self._q:
                    return
                series, span, keys, bucket_s = self._q.pop()   # newest first
            try:
                with self.lock:
                    summarise_file(series, span, keys, bucket_s, self.cache)
                self.done += 1
            except Exception:
                pass          # a file that will not decode must not kill the thread
