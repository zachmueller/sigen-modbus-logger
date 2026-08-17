"""
Freezing one view into `share/<uid>/` -- an immutable, public copy of what was on screen.

Invoked by `POST /api/share` from the gated viewer. What it produces is the same tile layout
web/tiles.js already reads, under a different prefix, so a share page is the ordinary page
pointed at ordinary tiles. There is no share renderer.

**Copies, not pointers.** A pointer into `agg/*` could not be read anonymously -- that prefix
is gated -- and re-aggregating history would silently change what someone was sent. A copy
costs a few hundred kilobytes and means a link keeps showing what it showed on the day.

**The renderer is copied too, which it was not at first.** The paragraph above was true of the
data and false of the code: `/p/<uid>` served one `site/share-view` object that loaded
`/app.js`, `/charts.js` and `/style.css` from the bucket root, and every one of those is
overwritten in place by `cdk deploy SigenSite`. So a share was frozen in its numbers and live in
its drawing, and a link sent today would be rendered by whatever existed when it was opened.
Each share now also copies one page -- `page.html`, taken byte for byte out of the immutable
`site/v/<VIEWER_VERSION>/` bundle the deploy published -- and that page names its own bundle's
assets. The JS and CSS are NOT copied per share: they are content-addressed, so every share made
from the same `web/` shares one bundle, and a redeploy of unchanged code overwrites it in place.

**The tile geometry is imported, never restated.** series.choose_bucket picks the width,
tiles.granularity_for maps it to hour/day/month, and ingest.path_for names the object. Those
are the same functions the ingest Lambda writes tiles with and the same arithmetic
web/tiles.js reads them with, so a share cannot land on a key nothing looks for. Restating
any of it here is the drift this whole arrangement exists to prevent.

**Why this is not behind API Gateway.** The read gate is a Lambda@Edge on the CloudFront
behaviour, so anything reachable *beside* CloudFront is ungated. For the OAuth callback that
is harmless -- it needs a valid authorization code. For an endpoint that mints PUBLIC copies
of private telemetry it would be a hole: anyone with the URL could create shares. So this is
a Lambda Function URL with `AWS_IAM` auth, fronted by CloudFront Origin Access Control, and
IAM refuses anyone who is not this distribution. The alternative -- verifying the Cognito id
token here as well -- would mean an RS256 verification, and the Python runtime ships no
crypto library that does it, leaving only a hand-rolled one.

That choice has one cost, and it is paid elsewhere: OAC signs the origin request with SigV4,
and a function URL rejects an unsigned payload, so a POST body only arrives here if something
put its SHA-256 in `x-amz-content-sha256` first. The read gate does it -- see signPayload() in
cloud/lambda/auth-edge/index.js. Without it Lambda refuses the request 403 BEFORE invoking
this module, so nothing appears in its log at all. See docs/FINDINGS.md 27.

Environment:
  BUCKET          the one bucket
  AGG_PREFIX      default "agg/"     -- read
  SHARE_PREFIX    default "share/"   -- written
  SITE_PREFIX     default "site/"    -- read, for the pinned viewer bundle only
  VIEWER_VERSION  which bundle under SITE_PREFIX + "v/" a share is pinned to
  SITE_DOMAIN     for the URL handed back to the page
  CAPTURE_TZ      the capture host's zone; see the tzset note below
"""
import json
import os
import time
import traceback
import uuid

import boto3

# Same reason as the ingest handler: `TZ` is reserved by Lambda and CloudFormation refuses to
# set it, so the zone arrives under our own name and is applied before anything imports a
# module that might compute a local time. Nothing here labels an axis, but meta.json is
# copied through and a future change that touches series.tz_runs() would be wrong by twelve
# hours with no visible cause.
os.environ["TZ"] = os.environ.get("CAPTURE_TZ", "UTC")
time.tzset()

import ingest      # noqa: E402
import series      # noqa: E402
import tiles       # noqa: E402

s3 = boto3.client("s3")

BUCKET = os.environ["BUCKET"]
AGG_PREFIX = os.environ.get("AGG_PREFIX", "agg/")
SHARE_PREFIX = os.environ.get("SHARE_PREFIX", "share/")
SITE_PREFIX = os.environ.get("SITE_PREFIX", "site/")
VIEWER_VERSION = os.environ.get("VIEWER_VERSION", "")
SITE_DOMAIN = os.environ.get("SITE_DOMAIN", "")

# Matches the textarea's maxlength in web/index.html. Truncated rather than refused: losing
# the tail of a note is a smaller failure than losing the share.
MAX_NOTE_CHARS = 2000
# A frozen span is finished by definition, so every object here is immutable.
IMMUTABLE = "public, max-age=31536000, immutable"
UID_HEX = 8
# The share's own copy of the viewer page. `/p/<uid>` serves this object, so a share carries the
# renderer it was made with rather than borrowing whichever one is current.
PAGE = "page.html"
PAGE_TYPE = "text/html; charset=utf-8"


class BadRequest(Exception):
    """Something the caller can fix, reported as 400 with the reason."""


# ------------------------------------------------------------------ tile geometry

def width_for(meta, span_s):
    """The bucket width this window will resolve to. Mirrors web/tiles.js widthFor().

    choose_bucket says what the ladder wants; the plan only has tiles at the widths it
    materialised, so snap UP to one of those -- coarser is fewer requests and a smaller
    payload, and going finer would fetch more points than the chart has pixels for.
    """
    want = series.choose_bucket(span_s, meta.get("fast_period_s") or 1)
    have = sorted(w for k in (tiles.HOUR, tiles.DAY, tiles.MONTH)
                  for w in (meta.get("granularity") or {}).get(k) or [])
    if not have:
        raise BadRequest("this archive has no tiles at any width")
    if want in have:
        return want
    for b in have:
        if b >= want:
            return b
    return have[-1]


def covering(kind, start, end):
    """The UTC span starts whose tiles cover [start, end). Mirrors tiles.js tilePaths()."""
    out, ts = [], start
    while ts < end:
        lo, hi = ingest.SPAN_OF[kind](ts)
        out.append(lo)
        ts = hi
    return out


# ------------------------------------------------------------------ copying

def _get_json(key):
    return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())


def _put_json(key, obj):
    s3.put_object(Bucket=BUCKET, Key=key,
                  Body=json.dumps(obj, allow_nan=False, separators=(",", ":")).encode(),
                  ContentType="application/json", CacheControl=IMMUTABLE)


def _keys_under(prefix):
    """The set of keys under `prefix`. Listed, deliberately, rather than probed.

    The obvious implementation is head_object on each candidate and treat 404 as absent. It
    does not work here, and the reason is already recorded in this repository as FINDINGS 11:
    **without s3:ListBucket, S3 answers 403 for a key that does not exist**, because it will
    not confirm absence to a caller that cannot list. The first version of this function
    probed, and every share failed with `An error occurred (403) when calling HeadObject`
    while checking whether a brand-new share id was free.

    Catching 403 as "absent" would fix the symptom and reintroduce the bug web/tiles.js
    refuses to have: a genuine permission failure would read as "the logger was off" and
    produce a share full of gaps rather than an error. Listing has no such ambiguity -- an
    absent tile is simply not in the set, and a broken policy raises.

    list_objects_v2 also SENDS a prefix, which is what lets the IAM grant stay scoped with an
    `s3:prefix` condition; head_object sends none, so a scoped ListBucket would not have
    helped it anyway.
    """
    out = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            out.add(obj["Key"])
    return out


def _require(key, missing):
    """`key`, or a BadRequest saying `missing`. Established by LISTING, for the reason above.

    A one-key listing: the key is its own prefix, so this is a single request, and it answers
    "is it there?" without asking S3 to confirm an absence it will not confirm. Every read
    whose absence is a real possibility goes through here -- probing with head_object and
    reporting the 403 as absence is exactly the collapse _keys_under() exists to avoid.
    """
    if key not in _keys_under(key):
        raise BadRequest(missing)
    return key


def _copy(src_key, dst_key, content_type=None):
    """One object into the share. The caller has already established that `src_key` exists.

    MetadataDirective REPLACE, not COPY, because Cache-Control has to change: an agg tile for
    the current day carries max-age=60 so it can be rebuilt, while a share is frozen and
    immutable forever. The other two headers are read off the source rather than guessed --
    Content-Encoding especially, since dropping it would hand the browser gzip bytes to
    JSON.parse.

    `content_type` overrides what the source says, for the one caller that knows better than the
    upload did: the viewer page is served as the share's whole document, and a page delivered as
    application/octet-stream downloads instead of rendering -- the failure site-stack.ts already
    carries a long comment about.
    """
    head = s3.head_object(Bucket=BUCKET, Key=src_key)
    extra = {"ContentType": content_type or head.get("ContentType") or "application/json",
             "CacheControl": IMMUTABLE}
    if head.get("ContentEncoding"):
        extra["ContentEncoding"] = head["ContentEncoding"]
    s3.copy_object(Bucket=BUCKET, Key=dst_key,
                   CopySource={"Bucket": BUCKET, "Key": src_key},
                   MetadataDirective="REPLACE", **extra)
    return True


def page_key(version=None):
    """The pinned page a share of this deployment copies. One place, because the backfill script
    and this handler have to agree on it exactly."""
    return f"{SITE_PREFIX}v/{version or VIEWER_VERSION}/share-view.html"


def _fresh_uid():
    """An unused 8-hex id. Checked, because a collision would OVERWRITE someone's share."""
    for _ in range(8):
        uid = uuid.uuid4().hex[:UID_HEX]
        if not _keys_under(f"{SHARE_PREFIX}{uid}/"):
            return uid
    raise BadRequest("could not find an unused share id")


# ------------------------------------------------------------------ the window

def _resolve_window(body, meta):
    """(start, end, hours) for the requested view.

    Mirrors web/app.js windowURL() exactly, because the frozen page will rebuild its window
    with that function against the meta written below. `hours: null` is the "All" preset,
    which app.js turns into the archive's whole extent -- and in a share the extent IS the
    frozen window, so it reproduces the same span.
    """
    ext = meta.get("extent") or {}
    hours = body.get("hours", None)
    if hours is None:
        first, last = ext.get("first_ts"), ext.get("last_ts")
        if first is None or last is None:
            raise BadRequest("this archive has no records to share")
        return float(first), float(last) + 1, None
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        raise BadRequest("hours must be a number, or null for the whole archive")
    if not 0 < hours <= 24 * 366 * 10:
        raise BadRequest("hours must be positive and less than a decade")
    end = body.get("end")
    if end in (None, "", 0):
        end = ext.get("last_ts")
    if end is None:
        raise BadRequest("this archive has no records to share")
    try:
        end = float(end)
    except (TypeError, ValueError):
        raise BadRequest("end must be a unix timestamp")
    return end - hours * 3600, end, hours


def _strings(value, limit=400):
    """A list of harmless identifiers. Field and panel ids reach a KEY, so anything that
    could climb out of the prefix is refused rather than sanitised."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise BadRequest("panels and fields must be arrays")
    out = []
    for v in value[:limit]:
        if not isinstance(v, str):
            raise BadRequest("panels and fields must be arrays of strings")
        v = v.strip()
        if v and all(c.isalnum() or c in "_-." for c in v) and ".." not in v:
            out.append(v)
    return out


# ------------------------------------------------------------------ the handler

def create(body):
    index = _get_json(AGG_PREFIX + ingest.INDEX)
    plan = body.get("plan") or index.get("current")
    if not plan or not all(c in "0123456789abcdef" for c in str(plan)):
        raise BadRequest("no plan hash to share from")
    # Listed, then read. Catching ClientError around the read instead reported a REFUSAL as
    # "no published tiles for plan x" -- a broken policy wearing the costume of an unpublished
    # plan, which is the same collapse in the other direction.
    meta = _get_json(_require(f"{AGG_PREFIX}plan={plan}/{ingest.META}",
                              f"no published tiles for plan {plan}"))

    start, end, hours = _resolve_window(body, meta)
    if start >= end:
        raise BadRequest("the window starts after it ends")
    width = width_for(meta, end - start)
    kind = tiles.granularity_for(width)

    # One listing for the whole width, then set membership per span. An absent tile is how
    # ingest.py records a span with no data, and copying nothing preserves that gap exactly
    # as the live page drew it.
    present = _keys_under(f"{AGG_PREFIX}plan={plan}/{kind}/b{width}/")
    # Before anything is written, because a share without it is a page that hard-errors on a
    # fetch it makes unconditionally -- and because head_object on a missing key answers 403,
    # so the copy below would have surfaced as an unhandled ClientError rather than a reason.
    latest = _require(f"{AGG_PREFIX}plan={plan}/{ingest.LATEST}",
                      f"plan {plan} has no {ingest.LATEST}, which every share page fetches")
    # Also before anything is written, and for a second reason beside the first: a share whose
    # page is missing is not a partial share, it is a link that 404s. If VIEWER_VERSION names a
    # bundle this bucket does not hold, the deploy that set it did not publish -- which is worth
    # saying out loud rather than discovering from a dead link.
    if not VIEWER_VERSION:
        raise BadRequest("this deployment did not say which viewer bundle to pin a share to, "
                         "so a share would drift on the next deploy")
    page = _require(page_key(),
                    f"the viewer bundle {VIEWER_VERSION} is not published, so a share made now "
                    f"would have no page of its own")
    uid = _fresh_uid()
    dest = f"{SHARE_PREFIX}{uid}/"
    copied = 0
    for span_start in covering(kind, start, end):
        rel = ingest.path_for(plan, width, span_start, kind)
        if AGG_PREFIX + rel in present:
            _copy(AGG_PREFIX + rel, dest + rel)
            copied += 1
    if not copied:
        # Every tile was absent, so the window has no records in it. Better to say so than
        # to hand back a link to an empty chart.
        raise BadRequest("that window has no data in it, so there is nothing to share")

    # latest.json is copied AS IT WAS, which is the point: it is what was current when the
    # view was shared. web/tiles.js reads the ages against meta.shared_at rather than the
    # clock, so a share opened next week still reports what it reported that day instead of
    # claiming the logger has been down for a week.
    #
    # Required, not optional: tiles.js fetches it non-optionally, so a share without it
    # would surface as a hard error on a page that otherwise works. Its presence was
    # established above, before the first write.
    _copy(latest, f"{dest}plan={plan}/{ingest.LATEST}")

    # The renderer, beside the data it renders. A verbatim copy rather than a page generated
    # here: the pinned bundle's own HTML already resolves the share id out of `/p/<uid>`, so
    # there is nothing to substitute -- and "the same bytes the deploy published" is then true by
    # construction instead of being a claim about a string operation.
    _copy(page, dest + PAGE, content_type=PAGE_TYPE)

    note = str(body.get("note") or "")[:MAX_NOTE_CHARS].strip()
    share_meta = dict(meta)
    # ONLY the width that was copied. web/tiles.js picks its width from this, so narrowing it
    # makes it impossible for the frozen page to ask for a tile that was never copied --
    # which would otherwise read as a gap in the data rather than a bug.
    share_meta["granularity"] = {k: ([width] if k == kind else [])
                                 for k in (tiles.HOUR, tiles.DAY, tiles.MONTH)}
    # The extent IS the frozen window. app.js prints it as the archive's span and, for the
    # "All" preset, rebuilds the window from it.
    share_meta["extent"] = dict(meta.get("extent") or {}, first_ts=start, last_ts=end)
    # What app.js needs to restore the screen. `end` is absolute even when the sharer was
    # following "now": null would mean "the newest there is", and a frozen view has no such
    # thing to track.
    share_meta["view"] = {"hours": hours, "end": end,
                          "expanded": _strings(body.get("panels")),
                          "custom": _strings(body.get("fields"))}
    share_meta["note"] = note
    share_meta["shared_at"] = time.time()
    # Recorded rather than used: the page it names is already copied in beside this file, so
    # nothing reads this back. It is here so that a share can be asked which renderer drew it,
    # which is the question this whole arrangement exists to be able to answer.
    share_meta["viewer"] = VIEWER_VERSION
    _put_json(f"{dest}plan={plan}/{ingest.META}", share_meta)

    # One plan, its extent already narrowed, so ensureMeta() resolves without an agg lookup.
    _put_json(dest + ingest.INDEX, {
        "v": tiles.TILE_VERSION,
        "plans": [{"hash": plan, "first_ts": start, "last_ts": end,
                   "fast_period_s": meta.get("fast_period_s")}],
        "current": plan,
    })

    print(json.dumps({"share": uid, "plan": plan, "bucket_s": width, "kind": kind,
                      "tiles": copied, "hours": hours, "viewer": VIEWER_VERSION}))
    return {"ok": True, "uid": uid, "url": f"https://{SITE_DOMAIN}/p/{uid}",
            "tiles": copied, "bucket_s": width, "start": start, "end": end}


def _reply(status, obj):
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(obj)}


def lambda_handler(event, context):
    method = (((event or {}).get("requestContext") or {}).get("http") or {}).get("method")
    if method and method.upper() != "POST":
        return _reply(405, {"ok": False, "error": "use POST"})
    raw = (event or {}).get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise BadRequest("the body must be a JSON object")
        return _reply(200, create(body))
    except BadRequest as e:
        return _reply(400, {"ok": False, "error": str(e)})
    except s3.exceptions.ClientError as e:
        # Ours, not the caller's -- and it must not escape. An unhandled exception behind a
        # function URL is a 502 carrying {"message": "Internal Server Error"}, which has no
        # `error` for the page to read, so it renders as a bare "502 " and says nothing about
        # where to look. FINDINGS 11 was a 403 on HeadObject that took a traceback to find;
        # this is what makes the next one legible from the page.
        err = e.response.get("Error") or {}
        op = getattr(e, "operation_name", None) or "an S3 call"
        print(json.dumps({"error": "s3", "operation": op, "code": err.get("Code"),
                          "message": err.get("Message")}))
        traceback.print_exc()
        return _reply(500, {"ok": False,
                            "error": f"S3 refused {op} ({err.get('Code') or 'no code'}) -- "
                                     f"the share Lambda's log has the key and the traceback"})
    except (ValueError, KeyError) as e:
        # A malformed body, not a broken service. Say which without echoing it back.
        return _reply(400, {"ok": False, "error": f"could not read the request: {e}"})
