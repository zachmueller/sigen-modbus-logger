#!/usr/bin/env python3
"""
Turning a raw archive into the precomputed tiles the hosted viewer reads. Stdlib only.

Not a CLI, and nothing here knows about S3. It reads a directory of raw archive files
and writes a directory of tiles, which is what makes it testable with no network and no
credentials -- cloud/lambda/ingest/handler.py is the thin part that syncs S3 to /tmp,
calls this, and syncs back.

**The presentation contract comes from serve.py, deliberately.** PANELS, ENERGY_TILES
and LIVE_FIELDS are imported rather than restated, so the hosted page draws the charts
the local viewer draws. A second copy of that list is exactly the drift this whole
arrangement is built to avoid, and it would show up as a hosted page quietly missing a
panel the local one has.

Which tiles exist, and why the spans are what they are:

  - **UTC spans, always.** series.py keys buckets on absolute epoch, and every ladder
    width up to 86400 divides 86400, so a UTC-aligned span never straddles a bucket. A
    local-date span would straddle twice a year at a DST transition. The page still
    labels in the capture host's clock; partitioning and labelling are separate concerns.
  - **A tile is written once, from a COMPLETE span, and then never rewritten.** That is
    what makes it safe to serve `immutable` from a CDN, and it is why coarse tiles cost
    nothing to maintain: a finished hour, day or month cannot change. The only exception
    is the current day, which is rebuilt each hour while it fills and carries a short TTL
    until it closes.
  - **Fine widths carry the panel fields; coarse widths carry everything.** An hour tile
    holds the ~38 fields the panels, energy tiles and live strip need. From 120 s up a
    tile spans a day and carries the whole ~259-field catalogue, because by then the
    per-field cost has collapsed. Materialising all of them at 30 s would make a tile
    larger than the raw .bin.gz it came from, which would defeat the point.

    What that costs, stated plainly because it is a real limit and easy to get wrong:
    the field picker works only where the day tiles do. series.choose_bucket() reaches
    120 s at a span of 15 HOURS, not the ~4 the design notes claimed -- with
    TARGET_BUCKETS at 900 a bucket of 120 s needs 108,000 s of span. So the 24 h, 3 d,
    7 d and longer views can chart any captured register; 15 m, 1 h and the default 6 h
    cannot, and meta's `picker_min_bucket_s` is what tells the page to say so rather
    than offer a field that would come back absent. The local viewer still has the whole
    catalogue at the full 2 s cadence; that is the tool for a narrow look at an odd
    register.
  - **An incomplete month has no month tile.** It is served from that month's day tiles
    instead. Complete months are therefore immutable-on-first-write, and no ingest run
    ever re-reads a month of raw. The alternative -- rebuilding month tiles hourly --
    was both the largest cost in the whole system and a source of tiles that silently
    lagged a day behind.
"""
import calendar
import gzip
import json
import os
import time

import series
import serve
import tiles

# Only these are ever written outside a tile directory.
INDEX = "index.json"
META = "meta.json"
LATEST = "latest.json"

# A finished span cannot change, so it is safe to cache forever. The current day's tile
# is rebuilt each hour while it fills, so it gets a TTL instead -- and is rewritten once
# with the immutable header when the day closes.
IMMUTABLE = "public, max-age=31536000, immutable"
FRESH = "public, max-age=60"

# How stale the hosted page may be, in seconds, before it says the logger has stopped
# rather than that the data is merely old. Two missed rotations: the tiles only appear
# when log.py rotates, so anything under an hour is normal and says nothing.
STALL_AFTER_S = 7200


# ------------------------------------------------------------------- UTC spans

def hour_span(ts):
    lo = int(ts) // 3600 * 3600
    return lo, lo + 3600


def day_span(ts):
    lo = int(ts) // 86400 * 86400
    return lo, lo + 86400


def month_span(ts):
    """The UTC calendar month containing `ts`. Not a fixed width -- 28 to 31 days."""
    g = time.gmtime(ts)
    lo = calendar.timegm((g.tm_year, g.tm_mon, 1, 0, 0, 0, 0, 0, 0))
    y, m = (g.tm_year + 1, 1) if g.tm_mon == 12 else (g.tm_year, g.tm_mon + 1)
    return lo, calendar.timegm((y, m, 1, 0, 0, 0, 0, 0, 0))


SPAN_OF = {tiles.HOUR: hour_span, tiles.DAY: day_span, tiles.MONTH: month_span}


def widths_for(kind, floor_s=1):
    """The ladder widths carried at this granularity, finest first.

    `floor_s` is the plan's fast cadence, and widths below it are skipped because they
    can never be asked for: series.choose_bucket() takes the cadence as its floor, so at
    a 2 s tick nothing ever resolves to a 1 s bucket. Generating them anyway cost 28% of
    the whole aggregate for tiles no reader could request -- and at a width finer than
    the cadence most buckets hold one sample, so min == mean == max and half are empty.
    """
    return [b for b in series.BUCKET_LADDER
            if tiles.granularity_for(b) == kind and b >= floor_s]


def path_for(plan_hash, bucket_s, start, kind):
    """Where a tile lives, relative to the aggregate root.

    Named by its UTC span rather than by an index, so a key is legible and sorts:
    hour/b30/2026/08/16/14.json.gz, day/b300/2026/08/16.json.gz.

    Granularity sits above the width on purpose. Hour tiles at fine widths are the bulk
    of the volume and the only part anyone would ever want to expire, and an S3 lifecycle
    rule needs a LITERAL prefix -- so `plan=<hash>/hour/` has to be a prefix, which it
    would not be if the width came first. No expiry is configured today; this only keeps
    the option available without a key migration.
    """
    g = time.gmtime(start)
    stem = {
        tiles.HOUR: "%04d/%02d/%02d/%02d" % (g.tm_year, g.tm_mon, g.tm_mday, g.tm_hour),
        tiles.DAY: "%04d/%02d/%02d" % (g.tm_year, g.tm_mon, g.tm_mday),
        tiles.MONTH: "%04d/%02d" % (g.tm_year, g.tm_mon),
    }[kind]
    return "plan=%s/%s/b%d/%s.json.gz" % (plan_hash, kind, bucket_s, stem)


# ------------------------------------------------------------------ field sets

def counter_keys():
    """The lifetime counters. Endpoints in every tile, never a line."""
    return [t["key"] for t in serve.ENERGY_TILES]


def fine_keys(s):
    """What an hour tile carries: whatever the panels, the energy tiles and the live
    strip need, minus the counters, restricted to what this plan actually captured."""
    covered = {c["key"] for c in series.catalog(s)}
    counters = set(counter_keys())
    wanted = serve.panel_keys() + list(serve.LIVE_FIELDS)
    return [k for k in dict.fromkeys(wanted) if k in covered and k not in counters]


def coarse_keys(s):
    """What a day or month tile carries: every plottable field the blocks cover, so the
    field picker works at 5-minute buckets and wider. Duplicate registers are dropped --
    plotting both draws one line twice, and the catalogue already marks the copy."""
    counters = set(counter_keys())
    return [c["key"] for c in series.catalog(s)
            if "duplicate_of" not in c and c["key"] not in counters]


# ----------------------------------------------------------------- tile writing

def _dump(obj):
    """`obj` as JSON bytes.

    Compact separators: a day tile is a few hundred thousand numbers, and json's default
    ", " costs a byte each. allow_nan=False because NaN is not JSON and a browser's
    parser rejects it outright -- better to fail here than to serve an unparseable tile.
    """
    return json.dumps(obj, allow_nan=False, separators=(",", ":")).encode()


def _write(out_dir, rel, obj):
    """One object, gzipped if its name says so.

    Tiles are gzipped and served with Content-Encoding: gzip, so the browser inflates
    them and no page code has to. This is not a nicety: these are long arrays of
    similar numbers, and gzip takes the whole aggregate from 141 MB to 9 MB -- 15x, and
    26x on the widest tiles. Without it the derived data would be twenty times the raw
    archive it came from.

    mtime is pinned to 0 so the bytes are a pure function of the contents. A gzip header
    carrying the build time would make every re-run of an unchanged tile a new object,
    which defeats both S3's ETag and any CDN validator.
    """
    path = os.path.join(out_dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = _dump(obj)
    if rel.endswith(".gz"):
        with gzip.GzipFile(path, "wb", compresslevel=6, mtime=0) as fh:
            fh.write(body)
    else:
        with open(path, "wb") as fh:
            fh.write(body)
    return path


def complete(kind, start, now):
    """Has this span finished? A finished tile is written once and cached forever."""
    return SPAN_OF[kind](start)[1] <= now


def write_span(s, out_dir, kind, ts, cache=None, now=None):
    """Every tile for the span containing `ts`. Returns [(relpath, cache_control)].

    A span with no records in it writes nothing rather than a tile full of nulls: the
    reader treats an absent tile as "no data here", which is the truth, and an empty
    tile would cost a request to say the same thing.
    """
    now = time.time() if now is None else now
    start, end = SPAN_OF[kind](ts)
    keys = fine_keys(s) if kind == tiles.HOUR else coarse_keys(s)
    counters = counter_keys()
    header = IMMUTABLE if complete(kind, start, now) else FRESH
    written = []
    for bucket_s in widths_for(kind, s.fast_period_s):
        tile = tiles.build_from_series(s, {c["key"]: c for c in series.catalog(s)},
                                       start, end, bucket_s, keys, counters,
                                       cache=cache)
        if not tile["records"]:
            continue
        rel = path_for(s.plan_hash, bucket_s, start, kind)
        _write(out_dir, rel, tile)
        written.append((rel, header))
    return written


def extent(s):
    """(first record, LAST record) -- the true end, not the indexed one.

    series.extent() reports the newest FILE's first record, and says so: the true end
    needs a scan. That is the right trade for the local viewer, which polls /api/latest
    separately. For tiling it is a bug: every hour after the open file's first record
    would get no tile, so an archive still on its first file would produce exactly one
    hour of tiles however long it ran.

    series.latest() already does the scan properly -- newest-first, stopping at the first
    record it finds -- so use it rather than a second implementation. No field keys: the
    values are not wanted here, only the timestamp.
    """
    first, indexed = s.extent()
    if first is None:
        return None, None
    lt = series.latest(s, [])
    end = lt.get("record_ts")
    return first, max(indexed or first, end or first)


def spans_touching(s, kind):
    """Every span of this granularity the archive has records in, oldest first."""
    first, last = extent(s)
    if first is None or last is None:
        return []
    out, ts = [], SPAN_OF[kind](first)[0]
    while ts <= last:
        out.append(ts)
        ts = SPAN_OF[kind](ts)[1]
    return out


def build_all(s, out_dir, cache=None, now=None, kinds=None):
    """Every tile the archive supports. What backfill runs, and what a test can check.

    Skips an incomplete month, which by design has no tile -- the reader falls back to
    that month's day tiles, so complete months stay immutable-on-first-write and no run
    ever re-reads a month of raw.
    """
    now = time.time() if now is None else now
    written = []
    for kind in (kinds or (tiles.HOUR, tiles.DAY, tiles.MONTH)):
        for ts in spans_touching(s, kind):
            if kind == tiles.MONTH and not complete(kind, ts, now):
                continue
            written.extend(write_span(s, out_dir, kind, ts, cache=cache, now=now))
    return written


# ----------------------------------------------------------- the small documents

def build_meta(s, capture_tz_now=None):
    """What /api/meta answers, as a static document.

    Identity is reduced to the model, exactly as serve.meta() reduces it when
    web_show_identity is false. This file is served to four people over the internet;
    the serial and the inverter's LAN address are not part of reading a chart, and
    tests/ asserts they never appear here.
    """
    first, last = extent(s)
    manifest = s.manifest
    device = dict(s.device())
    kept = {"model": device.get("model") or "unknown"}
    if not device.get("model"):
        kept["why"] = ("this manifest was written while the device was not answering: "
                       + str(device.get("error") or device.get("omitted") or "no identity"))
    kept["withheld"] = "serial, firmware and inverter address are not published"
    spans = s.spans()
    return {
        "ok": True,
        "plan_hash": s.plan_hash,
        "manifest": os.path.basename(s.manifest_path),
        "fast_period_s": s.fast_period_s,
        "device": kept,
        "panels": serve.PANELS,
        "energy_tiles": serve.ENERGY_TILES,
        "default_hours": 6,
        "blocks": [{"label": b["label"], "addr": b["addr"], "count": b["count"],
                    "period_s": b["period_s"]} for b in manifest["blocks"]],
        "extent": {"first_ts": first, "last_ts": last, "files": len(spans),
                   "bytes": sum(sp.size for sp in spans)},
        "catalog": series.catalog(s),
        # Change points across the archive, not just now: a year-long view straddles a
        # DST switch, and one offset would label half of it an hour out. Computed in the
        # capture host's zone, which is why the Lambda runs with TZ set to it.
        "tz": series.tz_runs(_tz_probe(first, last, capture_tz_now)),
        "stall_after_s": STALL_AFTER_S,
        "bucket_ladder": list(series.BUCKET_LADDER),
        "samples_per_bucket": series.SAMPLES_PER_BUCKET,
        "granularity": {k: widths_for(k, s.fast_period_s)
                        for k in (tiles.HOUR, tiles.DAY, tiles.MONTH)},
        "counters": counter_keys(),
        # What the page needs before it picks a bucket width: below picker_min_bucket_s
        # only fine_fields are materialised, so the picker has to say so rather than
        # offer a field that would come back absent. Stated here rather than hardcoded in
        # JavaScript, so widening a tile widens the picker with no code change -- and so
        # the number in the UI is derived rather than a comment someone has to remember.
        "fine_fields": fine_keys(s),
        "coarse_fields": coarse_keys(s),
        "picker_min_bucket_s": min(widths_for(tiles.DAY, s.fast_period_s)),
    }


def _tz_probe(first, last, now=None):
    """Daily probe points across the archive, so tz_runs() finds every change point.

    Sampling daily rather than per bucket: a UTC offset changes at most a couple of
    times a year, and tz_runs() collapses runs, so a day's resolution finds the
    transition to within a day -- which is what the axis needs. Probing every bucket of
    a year would be 100k localtime() calls to produce two entries.
    """
    if first is None:
        return [time.time() if now is None else now]
    last = last or first
    out, ts = [], float(first)
    while ts <= last:
        out.append(ts)
        ts += 86400
    out.append(float(last))
    return out


def latest(s):
    """What /api/latest answers, as a static document.

    Deliberately WITHOUT `logger_stalled`. series.latest() computes it against a
    threshold of a few cadences, which in a file written once an hour is always true --
    the hosted page would show a permanent red "the logger may have stopped". So the
    document carries the timestamps and `stall_after_s`, and the reader decides at render
    time. An age is only meaningful relative to when it is read.
    """
    out = series.latest(s, serve.LIVE_FIELDS)
    cat = {c["key"]: c for c in series.catalog(s)}
    labels, alarms = {}, {}
    for key, v in list(out.get("values", {}).items()):
        m = cat.get(key, {})
        if m.get("enum") and v is not None:
            labels[key] = m["enum"].get(str(int(v)), f"undocumented code {int(v)}")
        if m.get("alarm_table") and v:
            alarms[key] = series.alarm_bits(key, v)
    out["labels"] = labels
    out["alarms"] = alarms
    out["stall_after_s"] = STALL_AFTER_S
    for gone in ("logger_stalled", "now", "record_age_s", "data_age_s"):
        out.pop(gone, None)
    return out


def index(found):
    """{plans: [...], current: hash} over every series in the archive.

    `current` is the one holding the newest record, which is what the viewer opens --
    the same rule series.newest_series() applies locally. Superseded plans stay listed
    so an earlier series is reachable rather than silently dropped.
    """
    plans = []
    for plan_hash, s in sorted(found.items()):
        first, last = extent(s)
        if first is None:
            continue
        plans.append({"hash": plan_hash, "first_ts": first, "last_ts": last,
                      "fast_period_s": s.fast_period_s})
    plans.sort(key=lambda p: p["last_ts"] or 0)
    return {"v": tiles.TILE_VERSION,
            "plans": plans,
            "current": plans[-1]["hash"] if plans else None}


def write_documents(s, out_dir, capture_tz_now=None):
    """meta.json and latest.json for one plan. Always FRESH: extent moves every hour."""
    root = "plan=%s" % s.plan_hash
    return [(_rel(_write(out_dir, os.path.join(root, META),
                         build_meta(s, capture_tz_now)), out_dir), FRESH),
            (_rel(_write(out_dir, os.path.join(root, LATEST), latest(s)), out_dir), FRESH)]


def write_index(found, out_dir):
    return [(_rel(_write(out_dir, INDEX, index(found)), out_dir), FRESH)]


def _rel(path, out_dir):
    return os.path.relpath(path, out_dir)


# ------------------------------------------------------------------- the driver

def _discover(data_dir):
    found = series.discover(data_dir)
    if not found:
        raise ValueError(
            f"no decodable series in {data_dir}. A series needs both "
            f"*-<planhash>.bin/.bin.gz files and the *-<planhash>.manifest.json written "
            f"beside them.")
    return found


def run(data_dir, out_dir, now=None, cache=None):
    """Every tile and document for every series. What backfill runs.

    One series per plan hash, never merged -- that is what the hash exists to prevent,
    and series.discover() already enforces it.
    """
    found = _discover(data_dir)
    written = []
    for s in found.values():
        written.extend(build_all(s, out_dir, cache=cache or series.SummaryCache(),
                                 now=now))
        written.extend(write_documents(s, out_dir))
    written.extend(write_index(found, out_dir))
    return written


def run_for(data_dir, out_dir, touched, now=None, cache=None, plan_hash=None):
    """Only the tiles the timestamps in `touched` can have changed. What the Lambda runs.

    run() re-reads the whole archive, which is right for a backfill and wrong once an
    hour: today it takes 40 s for two days of data, and that grows without bound -- after
    a year every rotation would re-decode a year.

    What can actually change when one rotated file arrives:

      - the hour span(s) its records fall in. Usually one; two if the rotation straddled
        a UTC hour, which it does whenever rotate_minutes does not divide the hour.
      - the day span(s) containing those hours, because a day tile fills as the day runs.
      - a month, but only the moment it COMPLETES. An incomplete month has no tile by
        design, so there is nothing to keep up to date -- and once complete it is written
        once and never again.

    Everything else is already correct on disk, and a finished hour is immutable, so
    rewriting it would only churn ETags and CDN validators.
    """
    found = _discover(data_dir)
    now = time.time() if now is None else now
    cache = cache or series.SummaryCache()
    written = []
    for ph, s in found.items():
        if plan_hash and ph != plan_hash:
            continue
        spans = {(tiles.HOUR, hour_span(t)[0]) for t in touched}
        spans |= {(tiles.DAY, day_span(t)[0]) for t in touched}
        # A month gets its tile the first time it is seen complete. Cheap to re-check:
        # if it is already there this rewrites identical bytes, and if the ingest that
        # should have written it was lost, this is what repairs it.
        for t in touched:
            m_start, m_end = month_span(t)
            if m_end <= now:
                spans.add((tiles.MONTH, m_start))
            prev = month_span(m_start - 1)
            if prev[1] <= now:
                spans.add((tiles.MONTH, prev[0]))
        for kind, start in sorted(spans, key=lambda kv: (kv[0], kv[1])):
            written.extend(write_span(s, out_dir, kind, start, cache=cache, now=now))
        written.extend(write_documents(s, out_dir))
    written.extend(write_index(found, out_dir))
    return written
