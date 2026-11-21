"""
The live API: seed a page with the last five minutes, and admit it to the stream.

    GET  /api/live/seed        the five-minute table, newest last, plus how to draw it
    POST /api/live/session     {"op": "connect" | "renew" | "release", "slot"?, "lease"?}
    POST /api/tracker/live     the same, for a signed-in caller (the authorizer has run)

Behind CloudFront, same-origin, so there is no CORS. Python 3.12; boto3 from the runtime.

**Admission is a lease on one of ten client ids, and that is the whole cost cap.** A browser
that wants the stream claims a slot -- a conditional write on a `slot` item in the live table --
and gets back a presigned WebSocket URL whose credentials may connect to IoT as THAT slot's
client id and no other. IoT allows one connection per client id, so ten ids is ten concurrent
viewers, enforced by IoT itself rather than by anything here that could be got around. A slot
whose lease is not renewed expires on its own; a tab that closes without saying so frees its
slot within LEASE_S.

Two of the ten are reserved for signed-in callers, so a public page that has filled up cannot
lock the household out of the device tracker, which needs a stream of its own.

**Why the URL is not the gate.** IoT does not enforce X-Amz-Expires on these URLs (measured:
a URL used twenty seconds after a ten-second expiry connected), so a URL is good for as long as
the credentials that signed it -- STS_SECONDS. That is fine, and it is why the session policy
matters: a copied URL can still only ever be slot N, and using it knocks slot N's owner off
rather than adding an eleventh viewer.

**The presign is hand-rolled**, in stdlib hmac, because botocore has no presigner for the IoT
device gateway's WebSocket handshake. Two details are load-bearing and were each measured
against the real endpoint (docs/FINDINGS.md): the service name is `iotdevicegateway`, and the
session token is appended AFTER the signature is computed -- signed into the canonical query
string, the handshake is refused with no reason given.
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
from decimal import Decimal

import livefields

if os.environ.get("CAPTURE_TZ"):
    # NOT the Lambda's TZ, which it reserves. Same arrangement as the share handler: every
    # label the page draws is in the capture host's local time.
    os.environ["TZ"] = os.environ["CAPTURE_TZ"]
    time.tzset()

TABLE = os.environ.get("TABLE", "")
VIEWER_ROLE_ARN = os.environ.get("VIEWER_ROLE_ARN", "")
IOT_ENDPOINT = os.environ.get("IOT_ENDPOINT", "")
TOPIC = os.environ.get("TOPIC", "sigen/live")
CLIENT_PREFIX = os.environ.get("CLIENT_PREFIX", "sigen-view-")
REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

SLOTS = int(os.environ.get("SLOTS", "10"))
PUBLIC_SLOTS = int(os.environ.get("PUBLIC_SLOTS", "8"))   # the rest are for signed-in callers
LEASE_S = int(os.environ.get("LEASE_S", "150"))
RENEW_S = int(os.environ.get("RENEW_S", "60"))
WINDOW_S = int(os.environ.get("WINDOW_S", "300"))
STS_SECONDS = 900                                         # STS's minimum
SEED_MAX_AGE_S = 2                                        # CloudFront collapses a crowd to one

# Only these come back to the page. `pk` and `ttl` are storage, not data.
DROP = {"pk", "ttl"}


class Refused(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


# --------------------------------------------------------------------- storage seam

class Store:
    """The live table, via boto3. Tests substitute an in-memory fake with the same methods."""

    def __init__(self, table_name):
        import boto3
        self.table = boto3.resource("dynamodb").Table(table_name)

    def query_live(self, lo_ms, hi_ms):
        from boto3.dynamodb.conditions import Key
        out, kw = [], {"KeyConditionExpression":
                       Key("pk").eq("live") & Key("t").between(lo_ms, hi_ms)}
        while True:
            page = self.table.query(**kw)
            out.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return out
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def claim(self, slot, lease, exp, auth, now):
        """Take `slot` if it is free or expired. True if taken."""
        from botocore.exceptions import ClientError
        try:
            self.table.put_item(
                Item={"pk": "slot", "t": slot, "lease": lease, "exp": exp, "auth": auth},
                ConditionExpression="attribute_not_exists(pk) OR #e < :now",
                ExpressionAttributeNames={"#e": "exp"},
                ExpressionAttributeValues={":now": now})
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def renew(self, slot, lease, exp, now):
        """Extend `slot` if `lease` still holds it. True if renewed."""
        from botocore.exceptions import ClientError
        try:
            self.table.update_item(
                Key={"pk": "slot", "t": slot},
                UpdateExpression="SET #e = :exp",
                ConditionExpression="#l = :lease AND #e >= :now",
                ExpressionAttributeNames={"#e": "exp", "#l": "lease"},
                ExpressionAttributeValues={":exp": exp, ":lease": lease, ":now": now})
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def release(self, slot, lease):
        from botocore.exceptions import ClientError
        try:
            self.table.delete_item(
                Key={"pk": "slot", "t": slot},
                ConditionExpression="#l = :lease",
                ExpressionAttributeNames={"#l": "lease"},
                ExpressionAttributeValues={":lease": lease})
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def soonest_expiry(self):
        from boto3.dynamodb.conditions import Key
        items = self.table.query(KeyConditionExpression=Key("pk").eq("slot")).get("Items", [])
        exps = [int(i["exp"]) for i in items if "exp" in i]
        return min(exps) if exps else None


def assume_viewer(slot):
    """Credentials that may connect as this slot's client id, and subscribe to the topic."""
    import boto3
    client_arn = f"arn:aws:iot:{REGION}:{account_of(VIEWER_ROLE_ARN)}:client/{client_id(slot)}"
    topic_arn = f"arn:aws:iot:{REGION}:{account_of(VIEWER_ROLE_ARN)}:topic/{TOPIC}"
    filter_arn = f"arn:aws:iot:{REGION}:{account_of(VIEWER_ROLE_ARN)}:topicfilter/{TOPIC}"
    # The session policy INTERSECTS the role's: the role allows all ten ids, this allows one.
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "iot:Connect", "Resource": client_arn},
        {"Effect": "Allow", "Action": "iot:Subscribe", "Resource": filter_arn},
        {"Effect": "Allow", "Action": "iot:Receive", "Resource": topic_arn},
    ]}
    r = boto3.client("sts").assume_role(
        RoleArn=VIEWER_ROLE_ARN, RoleSessionName=f"live-slot-{slot}",
        DurationSeconds=STS_SECONDS, Policy=json.dumps(policy))
    c = r["Credentials"]
    return c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"]


def account_of(arn):
    return arn.split(":")[4] if arn.count(":") >= 5 else ""


def client_id(slot):
    return f"{CLIENT_PREFIX}{slot}"


# ------------------------------------------------------------------------- presign

def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _enc(s):
    return urllib.parse.quote(s, safe="-_.~")


def presign(host, region, access_key, secret_key, token=None, now=None):
    """wss://host/mqtt?... signed with SigV4 for the IoT device gateway.

    The token is appended after signing, not signed. See the module docstring.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    scope = f"{day}/{region}/iotdevicegateway/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(query.items()))
    canonical = "\n".join(["GET", "/mqtt", canonical_query, f"host:{host}\n", "host",
                           hashlib.sha256(b"").hexdigest()])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), day)
    for part in (region, "iotdevicegateway", "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"wss://{host}/mqtt?{canonical_query}&X-Amz-Signature={signature}"
    if token:
        url += "&X-Amz-Security-Token=" + _enc(token)
    return url


# ---------------------------------------------------------------------------- time

def tz_runs(start, end, step=3600):
    """[[ts, utc_offset_s, name]] change points, the shape web/charts.js takes as `tz`.

    The same contract as series.tz_runs(), sampled hourly across [start, end]. A page that
    stays open through a DST switch keeps labelling correctly for the two days this covers.
    """
    out = []
    ts = int(start)
    while ts <= end:
        lt = time.localtime(ts)
        off = lt.tm_gmtoff if lt.tm_gmtoff is not None else -time.timezone
        if not out or out[-1][1] != off:
            out.append([ts, off, time.strftime("%Z", lt)])
        ts += step
    return out


# --------------------------------------------------------------------------- routes

def plain(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v


def seed(store, now_ms):
    items = store.query_live(now_ms - WINDOW_S * 1000, now_ms + 60_000)
    msgs = [{k: plain(v) for k, v in it.items() if k not in DROP} for it in items]
    msgs.sort(key=lambda m: m.get("t", 0))
    now_s = now_ms // 1000
    return {"ok": True, "gen": now_ms, "window_s": WINDOW_S,
            "tz": tz_runs(now_s - 2 * WINDOW_S, now_s + 2 * 86400),
            "series": livefields.series(), "msgs": msgs}


def _slot_order(authed):
    """Which slots this caller may take, in the order to try them.

    A signed-in caller tries the reserved slots first, so it does not take a public slot that
    an anonymous visitor could have had.
    """
    public = list(range(PUBLIC_SLOTS))
    reserved = list(range(PUBLIC_SLOTS, SLOTS))
    return reserved + public if authed else public


def session(store, body, authed, now_s, assume=None, sign=None):
    assume = assume or assume_viewer
    sign = sign or (lambda ak, sk, tok: presign(IOT_ENDPOINT, REGION, ak, sk, tok))
    op = body.get("op")
    slot, lease = body.get("slot"), body.get("lease")
    if op not in ("connect", "renew", "release"):
        raise Refused(400, 'op must be "connect", "renew" or "release"')
    if slot is not None and (not isinstance(slot, int) or isinstance(slot, bool)
                             or not 0 <= slot < SLOTS):
        raise Refused(400, f"slot must be an integer from 0 to {SLOTS - 1}")
    if lease is not None and (not isinstance(lease, str) or len(lease) > 64):
        raise Refused(400, "lease must be the string a previous connect returned")
    exp = now_s + LEASE_S

    if op == "release":
        if slot is None or lease is None:
            raise Refused(400, "release needs the slot and lease it was given")
        return {"ok": True, "released": store.release(slot, lease)}

    if op == "renew":
        if slot is None or lease is None:
            raise Refused(400, "renew needs the slot and lease it was given")
        if not store.renew(slot, lease, exp, now_s):
            raise Refused(409, "that lease has lapsed; connect again", lost=True)
        return {"ok": True, "slot": slot, "lease": lease, "leaseS": LEASE_S,
                "renewS": RENEW_S}

    # connect. A page reloading, or reconnecting after the socket dropped, names the slot it
    # holds, and keeps it: it would otherwise churn through slots and could be refused one.
    held = (slot is not None and lease is not None
            and slot in _slot_order(authed) and store.renew(slot, lease, exp, now_s))
    if not held:
        lease = secrets.token_urlsafe(16)
        slot = next((s for s in _slot_order(authed)
                     if store.claim(s, lease, exp, authed, now_s)), None)
        if slot is None:
            soonest = store.soonest_expiry()
            raise Refused(429, "the live view is full -- every viewer slot is taken",
                          full=True,
                          retry_s=max(5, (soonest - now_s) if soonest else LEASE_S))
    ak, sk, tok = assume(slot)
    return {"ok": True, "slot": slot, "clientId": client_id(slot), "topic": TOPIC,
            "url": sign(ak, sk, tok), "lease": lease, "leaseS": LEASE_S, "renewS": RENEW_S}


# -------------------------------------------------------------------------- handler

def respond(status, body, cache=None):
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json",
                        "Cache-Control": cache or "no-store"},
            "body": json.dumps(body, separators=(",", ":"))}


def authed_of(event):
    """True once the API Gateway authorizer has admitted the caller."""
    ctx = (event.get("requestContext") or {}).get("authorizer") or {}
    lam = ctx.get("lambda") or {}
    return bool(lam.get("email_hash"))


def body_of(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if not raw:
        return {}
    try:
        b = json.loads(raw)
    except ValueError:
        raise Refused(400, "the body is not JSON")
    if not isinstance(b, dict):
        raise Refused(400, "the body must be a JSON object")
    return b


_store = None


def lambda_handler(event, context, store=None):
    global _store
    t_recv = (event.get("requestContext") or {}).get("timeEpoch") or int(time.time() * 1000)
    http = (event.get("requestContext") or {}).get("http") or {}
    method, path = http.get("method", "GET"), event.get("rawPath", "")
    try:
        if store is None:
            if _store is None:
                _store = Store(TABLE)
            store = _store
        if method == "GET" and path.endswith("/live/seed"):
            return respond(200, seed(store, int(time.time() * 1000)),
                           cache=f"public, max-age={SEED_MAX_AGE_S}")
        if method == "POST" and (path.endswith("/live/session")
                                 or path.endswith("/tracker/live")):
            out = session(store, body_of(event), authed_of(event), int(time.time()))
            # Two timestamps for the page's clock sync: when the GATEWAY received the request
            # and when this function sent the answer. Using both means a cold start's second of
            # init is subtracted out instead of being read as clock offset.
            out["recv"] = t_recv
            out["send"] = int(time.time() * 1000)
            return respond(200, out)
        return respond(404, {"ok": False, "error": f"no route for {method} {path}"})
    except Refused as r:
        return respond(r.status, dict({"ok": False, "error": r.message}, **r.extra))
    except Exception as e:                       # one JSON line, and a body the page can print
        print(json.dumps({"error": type(e).__name__, "detail": str(e)[:300],
                          "path": path}))
        return respond(500, {"ok": False, "error": "the live API failed; see its log"})
