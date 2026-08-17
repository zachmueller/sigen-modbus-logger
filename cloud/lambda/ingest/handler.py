"""
S3 -> /tmp -> ingest.py -> S3. The only part of the ingest path that knows about AWS.

Deliberately thin. Everything that decides what a tile contains lives in ingest.py and
tiles.py, which read and write plain directories and are tested with no network and no
credentials. This file moves bytes and nothing else, because it is the one part that
cannot be tested that way.

Triggered by ObjectCreated on `raw/`. What it does per event:

  1. work out which plan the arrived file belongs to, from its key;
  2. pull that plan's manifests plus the raw files whose LOCAL-time filename stamp is
     within a day either side of the arrival, into /tmp;
  3. read the arrived file's first and last record timestamps;
  4. call ingest.run_for() for exactly the spans those timestamps can have changed;
  5. copy the result up under `agg/`, with the Cache-Control ingest.py chose.

Why a day either side: raw filenames are the capture host's local wall clock, and tiles
are partitioned on UTC. A UTC day therefore overlaps up to three local dates, so a
narrower window would silently miss the records at a boundary. Over-fetching two extra
days of raw is a few hundred kilobytes; series.files_for() then makes the precise
selection, as it does locally.

Environment:
  BUCKET      the one bucket
  RAW_PREFIX  default "raw/"
  AGG_PREFIX  default "agg/"
  CAPTURE_TZ  the CAPTURE HOST's zone, e.g. Pacific/Auckland. Not cosmetic, and not
              spelled TZ -- see below.
"""
import json
import os
import re
import shutil
import sys
import time
import urllib.parse

import boto3

# Adopt the capture host's timezone before anything computes a local time.
#
# series.tz_runs() reads time.localtime(), and the page labels its axes from what that
# returns -- the archive's filenames, heartbeats and axis labels are all the capture
# host's clock. A Lambda runs in UTC, so without this the hosted page would label New
# Zealand data with UTC offsets: every chart shifted by twelve hours.
#
# It has to be done HERE rather than as a Lambda environment variable, because `TZ` is
# reserved: CloudFormation rejects a function whose config sets it. So the zone arrives
# under a name of our own and is applied with tzset(), which re-reads TZ from the
# process environment. Before the first localtime() call, and before `import ingest`
# pulls in serve.py, so nothing has cached an offset. The Python runtime ships tzdata.
os.environ["TZ"] = os.environ.get("CAPTURE_TZ", "UTC")
time.tzset()

# The archive modules are packaged flat beside this file, the way they sit in the
# repository -- Python puts the handler's directory on sys.path, so `import series`
# resolves with no package and no PYTHONPATH. Same arrangement as the capture host.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import decode      # noqa: E402
import ingest      # noqa: E402
import series      # noqa: E402

s3 = boto3.client("s3")

BUCKET = os.environ["BUCKET"]
RAW_PREFIX = os.environ.get("RAW_PREFIX", "raw/")
AGG_PREFIX = os.environ.get("AGG_PREFIX", "agg/")

WORK = "/tmp/work"
PLAN_IN_KEY = re.compile(r"/plan=([0-9a-f]{8})/")
# The stamp log.py puts in every filename, in the capture host's local time.
STAMP = re.compile(r"sigen-(\d{4})(\d{2})(\d{2})T\d{6}-[0-9a-f]{8}\.bin(\.gz)?$")


def _plan_of(key):
    m = PLAN_IN_KEY.search(key)
    if not m:
        raise ValueError(f"{key} carries no plan= segment; nothing can decode it")
    return m.group(1)


def _date_of(key):
    m = STAMP.search(key)
    return None if not m else tuple(int(g) for g in m.groups()[:3])


def _neighbouring_dates(ymd):
    """That local date and the two around it, as (y, m, d).

    A UTC day overlaps up to three local dates anywhere on earth, so this is the window
    that cannot miss a record at a boundary. Uses date arithmetic via the calendar rather
    than string edits, so month and year ends are not special cases.
    """
    import datetime
    d = datetime.date(*ymd)
    step = datetime.timedelta(days=1)
    return {(x.year, x.month, x.day) for x in (d - step, d, d + step)}


def _wanted(name, want_dates):
    """Should this object come down?

    Every manifest, always: it is the format of record, and without one beside the
    records nothing decodes. Raw files only on the dates in the window -- or all of them
    when `want_dates` is None, which no path in THIS function asks for any more. That case
    is kept for cloud/rebuild_tiles.py, which imports this module to do the whole-archive
    rebuild a Lambda timeout cannot hold.
    """
    if name.endswith(".manifest.json"):
        return True
    if want_dates is None:
        return name.endswith(".bin") or name.endswith(".bin.gz")
    return _date_of(name) in want_dates


def _download_plan(plan, want_dates):
    """Every manifest for `plan`, plus the raw files on `want_dates`. Returns the dir.

    Flat: series.discover() is non-recursive and wants the manifests beside the records,
    which is also how they sit on the capture host.
    """
    data_dir = os.path.join(WORK, "raw", plan)
    os.makedirs(data_dir, exist_ok=True)
    prefix = f"{RAW_PREFIX}plan={plan}/"
    got = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET,
                                                             Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if not _wanted(name, want_dates):
                continue
            s3.download_file(BUCKET, obj["Key"], os.path.join(data_dir, name))
            got += 1
    if not got:
        raise ValueError(f"nothing downloadable under s3://{BUCKET}/{prefix}")
    return data_dir


def _record_bounds(manifest, path):
    """(first ts, last ts) of a raw file. A whole-file scan, because records are
    variable length -- the bitmask decides each one's size, so there is nothing to seek
    to. Cheap: an hour is ~1800 headers."""
    first = last = None
    for ts, _, _ in decode.records_from(manifest, path):
        if first is None:
            first = ts
        last = ts
    return first, last


def _upload(out_dir, written):
    n = 0
    for rel, cache_control in written:
        if rel == ingest.INDEX:
            # Skipped deliberately: see _rewrite_index(). ingest.py builds an index over
            # whatever plans it can see, and an invocation can only see one.
            continue
        path = os.path.join(out_dir, rel)
        key = AGG_PREFIX + rel.replace(os.sep, "/")
        extra = {"ContentType": "application/json", "CacheControl": cache_control}
        if rel.endswith(".gz"):
            # The browser inflates it. Without this header it would download the gzip
            # bytes and JSON.parse would choke on them.
            extra["ContentEncoding"] = "gzip"
        s3.upload_file(path, BUCKET, key, ExtraArgs=extra)
        n += 1
    return n


def _rewrite_index():
    """agg/index.json, assembled from every plan's published meta.json.

    ingest.write_index() builds it from the plans it can see in a directory, which is
    right for a backfill run over a whole archive and wrong here: an invocation downloads
    ONE plan, so its index would name only that one -- and whichever rebuild ran last
    would silently drop the others. That is exactly what happened on the first backfill:
    the recovered 1 Hz series vanished from the index the moment the main plan rebuilt.

    So the index is derived from S3 instead, which is the only place that knows about all
    of them. meta.json already carries the extent and the cadence, so no decode is needed.
    """
    plans = []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=BUCKET, Prefix=AGG_PREFIX, Delimiter="/"):
        for pre in page.get("CommonPrefixes", []):
            name = pre["Prefix"].rstrip("/").rsplit("/", 1)[-1]
            if not name.startswith("plan="):
                continue
            key = f"{pre['Prefix']}{ingest.META}"
            try:
                meta = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
            except s3.exceptions.NoSuchKey:
                continue               # a plan mid-first-ingest; it will appear next time
            ext = meta.get("extent") or {}
            if ext.get("first_ts") is None:
                continue
            plans.append({"hash": meta["plan_hash"], "first_ts": ext["first_ts"],
                          "last_ts": ext["last_ts"],
                          "fast_period_s": meta.get("fast_period_s")})
    plans.sort(key=lambda p: p["last_ts"] or 0)
    body = json.dumps({"v": ingest.tiles.TILE_VERSION, "plans": plans,
                       "current": plans[-1]["hash"] if plans else None},
                      separators=(",", ":")).encode()
    s3.put_object(Bucket=BUCKET, Key=AGG_PREFIX + ingest.INDEX, Body=body,
                  ContentType="application/json", CacheControl=ingest.FRESH)
    print(f"index: {len(plans)} plan(s), current "
          f"{plans[-1]['hash'] if plans else 'none'}")
    return len(plans)


def process(key):
    plan = _plan_of(key)
    name = key.rsplit("/", 1)[-1]
    if not (name.endswith(".bin") or name.endswith(".bin.gz")):
        # A manifest upload needs no rebuild of its own: the raw file that follows it
        # will trigger one, and a manifest for a plan with no files decodes nothing.
        print(f"skip {key}: not a record file")
        return 0
    ymd = _date_of(name)
    if ymd is None:
        raise ValueError(f"{name} does not match log.py's naming; refusing to guess")

    data_dir = _download_plan(plan, _neighbouring_dates(ymd))
    found = series.discover(data_dir)
    if plan not in found:
        raise ValueError(f"no manifest for plan {plan} beside the records; "
                         f"without one the archive is undecodable")
    s = found[plan]
    first, last = _record_bounds(s.manifest, os.path.join(data_dir, name))
    if first is None:
        # A file that rotated with nothing in it yet. Not an error.
        print(f"skip {key}: no complete records")
        return 0

    out_dir = os.path.join(WORK, "agg")
    written = ingest.run_for(data_dir, out_dir, touched=[first, last], plan_hash=plan)
    n = _upload(out_dir, written)
    _rewrite_index()
    print(f"{key}: plan {plan}, records {first:.0f}..{last:.0f}, {n} objects written")
    return n


def lambda_handler(event, context):
    """S3 events, and nothing else.

    There WAS a `{"rebuild": "<planhash>"}` branch here, calling ingest.run() over a plan's
    whole raw prefix. It is gone, and its absence is the design: that job costs time
    proportional to the archive -- ~88 s per archive-day, measured -- so no fixed timeout can
    hold it. It crossed 300 s in the archive's first week; raising the limit to Lambda's 900 s
    ceiling bought about six days, and there is no third raise. An unbounded job in a function
    with a deadline fails by being KILLED PART-WAY, which here meant tiles rewritten and
    meta.json left stale, so it is not the kind of failure to leave available.

    It now lives in cloud/rebuild_tiles.py, on a workstation, where nothing is watching the
    clock -- calling this module's own _upload() so the objects are identical. See
    docs/FINDINGS.md 37.

    What remains is bounded by construction: three local dates of raw, the spans those records
    touch, ~62 s. That is the only thing that has to run unattended, and it is the only thing
    left here.
    """
    # /tmp survives between invocations on a warm container. Stale raw would be harmless
    # (series.discover re-reads the directory) but stale AGG would be uploaded again on
    # every event, so start clean rather than reason about it.
    shutil.rmtree(WORK, ignore_errors=True)
    if event.get("rebuild"):
        # Named explicitly rather than ignored: an old runbook, or a habit, would otherwise
        # get a cheerful {"objects": 0} and conclude the rebuild had nothing to do.
        raise ValueError(
            "the rebuild path was removed -- it cannot fit any Lambda timeout. Run "
            "cloud/rebuild_tiles.py --from-s3 --plan <hash> from a workstation instead. "
            "For a meta.json-only change, replay one S3 event, or wait one rotation.")
    total = 0
    for rec in event.get("Records", []):
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        total += process(key)
    return {"objects": total}
