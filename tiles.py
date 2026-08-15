#!/usr/bin/env python3
"""
The wire format the page reads, and the precomputed tiles that carry it. Stdlib only.

Not a CLI. Imported by two callers that must not disagree:

  - serve.py, which answers a window live by decoding the archive on the LAN;
  - cloud/lambda/ingest, which precomputes the same columns per tile so the hosted
    viewer needs no server at read time.

**Why one module rather than two implementations.** web/app.js reads one shape. If the
live path and the precomputed path build it differently -- a different rounding, a
different rule for when a field is "empty" -- then the hosted page and the local page
disagree about the same archive, and the disagreement is quiet: both render, both look
plausible, and only a careful side-by-side would show it. So the column shaping lives
here once and tests/ asserts the two callers produce identical output for the same span.

A word on the word "tile", which this repository unfortunately uses for two things. In
web/ and in serve.PANELS a *tile* is a stat card on the page (`ENERGY_TILES`, `.tile`).
Here a *tile* is a precomputed chunk of aggregated series -- one UTC hour, day or month
at one bucket width, the map-tile sense. This module only ever means the second.

Two boundary rules the whole scheme rests on:

  - **Tiles are partitioned on UTC boundaries, never on local dates.** series.py keys
    buckets on absolute epoch, and every ladder width up to 86400 divides 86400 -- so a
    UTC-aligned tile never straddles a bucket and adjacent tiles concatenate exactly.
    A local-date tile would straddle twice a year at a DST transition and leave a
    one-hour seam. The page still LABELS in the capture host's local clock, from the tz
    change points series.tz_runs() emits; partitioning and labelling are separate.
  - **A tile covers [start, end), exclusive.** See series.window's `end_exclusive`.
"""
import series

TILE_VERSION = 1

# Which ladder width goes in which tile granularity, coarsest bound first. The fine
# widths are per-hour and carry only the fields the panels draw; from 120 s up, a tile
# spans a UTC day and carries the whole catalogue, because at that width the per-field
# cost has collapsed. Materialising all ~259 fields at 30 s would make a tile LARGER
# than the raw .bin.gz it was derived from, which would defeat the point of aggregating.
HOUR, DAY, MONTH = "hour", "day", "month"

GRANULARITY = (
    (60, HOUR),          # 1 s .. 60 s   -> one UTC hour
    (1800, DAY),         # 120 s .. 1800 s -> one UTC day
    (86400, MONTH),      # 3600 s .. 86400 s -> one UTC month
)


def granularity_for(bucket_s):
    """Which tile span carries this bucket width."""
    for limit, name in GRANULARITY:
        if bucket_s <= limit:
            return name
    return MONTH


def decimals_for(gain):
    """Digits worth printing: the register's own resolution, not float noise."""
    try:
        g = int(gain or 1)
    except (TypeError, ValueError):
        return 3
    return max(0, len(str(g)) - 1) if g > 1 else 0


def round_list(values, dec):
    """Rounded to the register's own resolution, with integral results as ints.

    round(241.3, 0) is 241.0, which JSON writes as "241.0": two bytes spent on a
    decimal point and a zero nobody reads. Over 900 buckets of min/mean/max that is
    ~8% of a window -- and in a tile it is bytes over the wire on every visit, paid
    again for every reader. The number is unchanged: JSON has one number type, and the
    page formats every value to the register's decimals before showing it either way.
    """
    out = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        r = round(v, dec)
        out.append(int(r) if r == int(r) else r)
    return out


def columns(win, catalog, keys, counter_keys=frozenset()):
    """{key: column} in the one shape web/app.js reads.

    `win` is series.window() output, `catalog` is {key: series.catalog entry}, `keys`
    is what to emit and in what order. A key with no column in `win` is skipped rather
    than emitted empty -- it was not captured, which is a different statement from
    "captured and absent".

    Exactly one of five forms per column, because the page draws each differently and
    sending arrays it will not read is waste:

      tile_only   a lifetime counter. One number on screen, not 720 points, so the
                  arrays are dropped. The total comes from counters() instead.
      empty       captured, but every sample in the span was absent, a sentinel or the
                  -1 not-present marker. One field instead of 720 nulls -- a third of
                  a window at night, and most of a day tile for the 32 PV channels this
                  unit does not have.
      bits        an alarm word. OR-ed over the bucket, so a bit set for one sample
                  still shows.
      last/min/max  a state register with an enum. A mean of two state codes is not a
                  state, so the bucket's last is what the band shows; min != max is how
                  "changed during this bucket" is detected.
      mean/min/max  everything else. The line is the mean; the band is min..max.
    """
    out = {}
    for key in keys:
        col = win["series"].get(key)
        if col is None:
            continue
        meta = catalog.get(key, {})
        dec = decimals_for(meta.get("gain", 1))
        item = {"unit": col["unit"], "cadence_s": col["cadence_s"]}
        if key in counter_keys:
            item["tile_only"] = True
        elif all(v is None for v in col["mean"]):
            item["empty"] = True
        elif meta.get("alarm_table"):
            item["bits"] = col["bits"]
        elif meta.get("enum"):
            item["last"] = col["last"]
            item["min"] = col["min"]
            item["max"] = col["max"]
        else:
            item["mean"] = round_list(col["mean"], dec)
            item["min"] = round_list(col["min"], dec)
            item["max"] = round_list(col["max"], dec)
        out[key] = item
    return out


def counters(win, keys):
    """{key: {first, last, first_ts, last_ts, reset}} for the lifetime counters.

    RAW endpoints, not a delta, because a window total spanning several tiles is
    last(last tile) - first(first tile) -- so a tile that carried only its own delta
    could not be composed with its neighbours. serve.py's live path uses
    series.energy() for the same numbers; this is the composable form of it.

    `reset` here is only what happened WITHIN this tile. A counter stepping back across
    a tile boundary is invisible from inside either one, so whoever concatenates tiles
    has to compare each tile's `first` against the previous tile's `last` as well. That
    is a real case, not a theoretical one: the counter loses an unflushed increment
    across a power cut (FINDINGS 11), and the result must read as "total is a floor",
    never as negative kWh.
    """
    out = {}
    for key in keys:
        col = win["series"].get(key)
        if not col or col["first_value"] is None or col["last_value"] is None:
            continue
        out[key] = {
            "first": col["first_value"],
            "last": col["last_value"],
            "first_ts": col["first_ts"],
            "last_ts": col["last_ts"],
            "reset": bool(col["reset"]),
        }
    return out


def bucket_count(start, end, bucket_s):
    """Buckets in [start, end). What `n` in a tile means, and what `t` is rebuilt from."""
    return max(0, int((end - start) // bucket_s))


def build(win, catalog, plan_hash, start, end, bucket_s, field_keys, counter_keys):
    """One tile, ready to serialise.

    `win` must have come from series.window(..., end_exclusive=True) over exactly
    [start, end) at exactly `bucket_s`, or the arrays will not line up with `n` -- which
    is asserted rather than trusted, because the failure is a chart silently shifted by
    one bucket per tile.

    `t` is deliberately absent: it is start + i * bucket_s, and sending it costs about
    10 KB a tile to say something the reader can compute.
    """
    n = bucket_count(start, end, bucket_s)
    if len(win["t"]) != n:
        raise ValueError(
            f"tile [{start}, {end}) at {bucket_s}s wants {n} buckets but the window has "
            f"{len(win['t'])}. Pass end_exclusive=True and a bucket-aligned span: "
            f"_grid() is end-inclusive, so the boundary bucket belongs to the next tile.")

    health = win["health"]
    return {
        "v": TILE_VERSION,
        "plan": plan_hash,
        "bucket_s": bucket_s,
        "start": int(start),
        "n": n,
        "series": columns(win, catalog, field_keys),
        "counters": counters(win, counter_keys),
        "health": {
            "records": health["records"],
            "empty": health["empty"],
            "latency_median": health["latency_median"],
            "latency_p95": health["latency_p95"],
            "latency_max": health["latency_max"],
        },
        # Clamped to the tile, so a tile is self-contained: the reader uses this to tell
        # "the logger wrote nothing here" from "this bucket is outside the archive", and
        # a span reaching past the tile would make that call about data it does not hold.
        "covered": [[max(a, start), min(b, end)]
                    for a, b in (health.get("covered") or [])
                    if a is not None and b is not None and b > start and a < end],
        "records": sum(health["records"]),
    }


def build_from_series(s, catalog, start, end, bucket_s, field_keys, counter_keys,
                      cache=None):
    """build(), driving series.window() with the arguments a tile requires.

    The one place that pairs end_exclusive=True with a bucket-aligned span, so no caller
    has to remember to. warm_budget_s=0: the budget exists to keep an interactive request
    responsive by handing cold files to a background thread, and an ingest run has
    nothing to be responsive to -- a partial tile would be written as if it were whole.
    """
    win = series.window(s, start, end, list(field_keys) + list(counter_keys),
                        bucket_s=bucket_s, cache=cache, warm_budget_s=0,
                        end_exclusive=True)
    if win["pending_files"]:
        raise ValueError(f"tile [{start}, {end}) left {win['pending_files']} file(s) "
                         f"unread; a partial tile must not be written as a whole one")
    return build(win, catalog, s.plan_hash, start, end, bucket_s,
                 field_keys, counter_keys)
