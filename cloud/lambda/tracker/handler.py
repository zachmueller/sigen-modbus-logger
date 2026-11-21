"""
The device tracker's API: devices, their states, and timestamped changes between them.

    GET   /api/tracker/time                     {recv, send} -- for the page's clock sync
    GET   /api/tracker/devices                  every device, with its states
    POST  /api/tracker/devices                  {name, desc?}         -> a device with off/on
    PATCH /api/tracker/devices                  {device, name?, desc?, off?, archived?}
    POST  /api/tracker/states                   {device, name}        -> a new state
    PATCH /api/tracker/states                   {device, state, name?, order?, archived?}
    GET   /api/tracker/events?device=&since=    newest first
    POST  /api/tracker/events                   {id, device, state, tPhone, sent, ...}
    PATCH /api/tracker/events                   {device, sk, tEdit?, deleted?, note?}
    GET   /api/tracker/export                   everything, for offline analysis

Every route is behind the API's Lambda authorizer (cloud/lambda/authorizer/), and through
CloudFront behind the edge gate as well. This handler checks once more that the authorizer
ran: a route wired up without it must fail closed here rather than open.

**Ids are stable and never reused.** A device or state is renamed by changing its `name`;
everything that refers to it -- events, the device's `off` -- holds the id. Nothing is
deleted: devices and states are archived, events are soft-deleted, so an analysis run next
month sees what was undone and when.

**An event records every clock it can, and corrects none of them.** The phone's own time of
the tap, the phone's estimate of its offset from the server and how good that estimate was,
when the gateway received it, and the newest point the Pi's stream had delivered to the
phone when the button was pressed. `tEst` is the best single estimate, computed once, here,
and never changed; a correction is a separate `tEdit`. The analysis decides which to trust.

**Retries are idempotent.** The page generates the event id and keeps unsent taps in an
outbox; the same tap arriving twice is the same item, not two state changes.
"""
import base64
import json
import os
import re
import secrets
import time

TABLE = os.environ.get("TABLE", "")
CFG = "CFG"

ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,40}$")
MAX_NAME = 60
MAX_DESC = 300
MAX_NOTE = 200


class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


# --------------------------------------------------------------------- storage seam

class Store:
    """The tracker table via boto3. Tests substitute an in-memory fake with the same methods."""

    def __init__(self, table_name):
        import boto3
        self.table = boto3.resource("dynamodb").Table(table_name)

    def config(self):
        from boto3.dynamodb.conditions import Key
        out, kw = [], {"KeyConditionExpression": Key("pk").eq(CFG)}
        while True:
            page = self.table.query(**kw)
            out.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return out
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def get(self, pk, sk):
        return self.table.get_item(Key={"pk": pk, "sk": sk}).get("Item")

    def put_new(self, item):
        """Put if absent. False if an item with that key already exists."""
        from botocore.exceptions import ClientError
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def update(self, pk, sk, fields, remove=()):
        """SET fields, REMOVE names, on an item that must already exist."""
        names, values, sets = {}, {}, []
        for i, (k, v) in enumerate(fields.items()):
            names[f"#f{i}"] = k
            values[f":v{i}"] = v
            sets.append(f"#f{i} = :v{i}")
        rems = []
        for j, k in enumerate(remove):
            names[f"#r{j}"] = k
            rems.append(f"#r{j}")
        expr = ("SET " + ", ".join(sets) if sets else "") + \
               (" REMOVE " + ", ".join(rems) if rems else "")
        kw = {"Key": {"pk": pk, "sk": sk}, "UpdateExpression": expr.strip(),
              "ConditionExpression": "attribute_exists(pk)",
              "ExpressionAttributeNames": names, "ReturnValues": "ALL_NEW"}
        if values:
            kw["ExpressionAttributeValues"] = values
        from botocore.exceptions import ClientError
        try:
            return self.table.update_item(**kw).get("Attributes")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    def events(self, device, since=None, limit=None, newest_first=True):
        from boto3.dynamodb.conditions import Key
        cond = Key("pk").eq("EV#" + device)
        if since is not None:
            cond = cond & Key("sk").gte(f"{int(since):013d}")
        out, kw = [], {"KeyConditionExpression": cond, "ScanIndexForward": not newest_first}
        while True:
            if limit:
                kw["Limit"] = limit - len(out)
            page = self.table.query(**kw)
            out.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page or (limit and len(out) >= limit):
                return out
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# ---------------------------------------------------------------------- the model

def new_id(prefix):
    return prefix + secrets.token_urlsafe(8).replace("-", "x").replace("_", "y")


def plain(v):
    from decimal import Decimal
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v


def text(body, key, limit, required=False):
    v = body.get(key)
    if v is None:
        if required:
            raise Refused(400, f"{key} is required")
        return None
    if not isinstance(v, str) or not v.strip():
        raise Refused(400, f"{key} must be non-empty text")
    v = v.strip()
    if len(v) > limit:
        raise Refused(400, f"{key} is longer than {limit} characters")
    return v


def number(body, key, required=False):
    v = body.get(key)
    if v is None:
        if required:
            raise Refused(400, f"{key} is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 1e15:
        raise Refused(400, f"{key} must be a number")
    return v


def ident(body, key):
    v = body.get(key)
    if not isinstance(v, str) or not ID_RE.match(v):
        raise Refused(400, f"{key} must be an id")
    return v


def devices(store):
    """{device id: device dict with a `states` list}, from the CFG partition."""
    items = [plain(i) for i in store.config()]
    out = {}
    for i in items:
        parts = i["sk"].split("#")
        if len(parts) == 2:                       # D#<did>
            out[parts[1]] = dict({k: v for k, v in i.items() if k not in ("pk", "sk")},
                                 id=parts[1], states=[])
    for i in items:
        parts = i["sk"].split("#")
        if len(parts) == 4 and parts[1] in out:   # D#<did>#S#<sid>
            out[parts[1]]["states"].append(
                dict({k: v for k, v in i.items() if k not in ("pk", "sk")}, id=parts[3]))
    for d in out.values():
        d["states"].sort(key=lambda s: (s.get("order", 0), s.get("created", 0)))
    return out


def create_device(store, body, by, now_ms):
    name = text(body, "name", MAX_NAME, required=True)
    desc = text(body, "desc", MAX_DESC) or ""
    did, off, on = new_id("d"), new_id("s"), new_id("s")
    store.put_new({"pk": CFG, "sk": f"D#{did}", "name": name, "desc": desc, "off": off,
                   "created": now_ms, "by": by, "archived": False})
    # Every device starts with the two states every device has. The off one is the default
    # the page shows before any event, and the one a device cannot lose.
    store.put_new({"pk": CFG, "sk": f"D#{did}#S#{off}", "name": "off", "order": 0,
                   "created": now_ms, "archived": False})
    store.put_new({"pk": CFG, "sk": f"D#{did}#S#{on}", "name": "on", "order": 1,
                   "created": now_ms, "archived": False})
    return devices(store)[did]


def patch_device(store, body, now_ms):
    did = ident(body, "device")
    known = devices(store)
    if did not in known:
        raise Refused(404, "no such device")
    fields = {}
    for key, limit in (("name", MAX_NAME), ("desc", MAX_DESC)):
        if key in body:
            fields[key] = text(body, key, limit, required=True) if key == "name" else \
                (body[key] or "").strip()[:limit]
    if "off" in body:
        sid = ident(body, "off")
        if sid not in {s["id"] for s in known[did]["states"] if not s.get("archived")}:
            raise Refused(400, "off must be one of this device's states")
        fields["off"] = sid
    if "archived" in body:
        fields["archived"] = bool(body["archived"])
    if not fields:
        raise Refused(400, "nothing to change")
    fields["updated"] = now_ms
    store.update(CFG, f"D#{did}", fields)
    return devices(store)[did]


def create_state(store, body, now_ms):
    did = ident(body, "device")
    known = devices(store)
    if did not in known:
        raise Refused(404, "no such device")
    name = text(body, "name", MAX_NAME, required=True)
    sid = new_id("s")
    order = max((s.get("order", 0) for s in known[did]["states"]), default=0) + 1
    store.put_new({"pk": CFG, "sk": f"D#{did}#S#{sid}", "name": name, "order": order,
                   "created": now_ms, "archived": False})
    return devices(store)[did]


def patch_state(store, body, now_ms):
    did, sid = ident(body, "device"), ident(body, "state")
    known = devices(store)
    if did not in known or sid not in {s["id"] for s in known[did]["states"]}:
        raise Refused(404, "no such state")
    fields = {}
    if "name" in body:
        fields["name"] = text(body, "name", MAX_NAME, required=True)
    if "order" in body:
        fields["order"] = number(body, "order", required=True)
    if "archived" in body:
        if body["archived"] and known[did]["off"] == sid:
            raise Refused(400, "the off state cannot be archived; make another state the off "
                               "state first")
        fields["archived"] = bool(body["archived"])
    if not fields:
        raise Refused(400, "nothing to change")
    fields["updated"] = now_ms
    store.update(CFG, f"D#{did}#S#{sid}", fields)
    return devices(store)[did]


# Fields the page sends with a tap. Everything else on an event is set here.
CLIENT_NUMBERS = ("tPhone", "sent", "off", "rtt", "syncAge", "piT", "piSeenPhone", "piLat")


def best_estimate(ev):
    """The tap in server time: the phone's clock corrected by its measured offset, or, without
    a sync, the gateway's receive time less how long the tap sat on the phone before sending."""
    if ev.get("off") is not None:
        return int(round(ev["tPhone"] + ev["off"]))
    waited = max(0, (ev.get("sent") or ev["tPhone"]) - ev["tPhone"])
    return int(round(ev["tGw"] - waited))


def create_event(store, body, by, t_gw, now_ms):
    eid = ident(body, "id")
    did, sid = ident(body, "device"), ident(body, "state")
    known = devices(store)
    if did not in known:
        raise Refused(404, "no such device")
    if sid not in {s["id"] for s in known[did]["states"]}:
        raise Refused(400, "state is not one of this device's states")
    ev = {"id": eid, "device": did, "state": sid, "by": by,
          "tGw": t_gw, "tServer": now_ms, "deleted": False}
    for key in CLIENT_NUMBERS:
        v = number(body, key, required=(key == "tPhone"))
        if v is not None:
            ev[key] = v
    ev["tEst"] = best_estimate(ev)
    sk = f"{ev['tEst']:013d}#{eid}"
    from decimal import Decimal
    item = {k: (Decimal(str(v)) if isinstance(v, float) else v) for k, v in ev.items()}
    item.update(pk="EV#" + did, sk=sk)
    if not store.put_new(item):
        # The same tap again (an outbox retry): the first one stands, and is what is returned.
        existing = store.get("EV#" + did, sk)
        return dict(event_out(plain(existing)), duplicate=True)
    return event_out(plain(item))


def patch_event(store, body, by, now_ms):
    did = ident(body, "device")
    sk = body.get("sk")
    if not isinstance(sk, str) or not re.match(r"^\d{13}#[A-Za-z0-9_-]{6,40}$", sk):
        raise Refused(400, "sk must be the key an event was created with")
    fields, remove = {}, []
    if "tEdit" in body:
        if body["tEdit"] is None:
            remove.append("tEdit")
        else:
            fields["tEdit"] = int(number(body, "tEdit", required=True))
    if "deleted" in body:
        fields["deleted"] = bool(body["deleted"])
    if "note" in body:
        fields["note"] = (body["note"] or "")[:MAX_NOTE]
    if not fields and not remove:
        raise Refused(400, "nothing to change")
    # tEst, tPhone and the rest are deliberately not editable: a correction is recorded beside
    # the evidence, never in place of it.
    fields.update(edited=now_ms, editedBy=by)
    out = store.update("EV#" + did, sk, fields, remove)
    if out is None:
        raise Refused(404, "no such event")
    return event_out(plain(out))


def event_out(item):
    return {k: v for k, v in item.items() if k != "pk"}


def list_events(store, params):
    did = params.get("device")
    if not isinstance(did, str) or not ID_RE.match(did):
        raise Refused(400, "device must be an id")
    since = params.get("since")
    try:
        since = int(since) if since not in (None, "") else None
    except ValueError:
        raise Refused(400, "since must be ms since the epoch")
    limit = min(500, int(params.get("limit") or 200))
    return [event_out(plain(e)) for e in store.events(did, since, limit)]


# ------------------------------------------------------------------------ handler

def respond(status, body):
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(body, separators=(",", ":"))}


def caller(event):
    """The authorizer's verdict, or None. None is a refusal: see the module docstring."""
    ctx = (event.get("requestContext") or {}).get("authorizer") or {}
    who = (ctx.get("lambda") or {}).get("email_hash")
    return who if isinstance(who, str) and who else None


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
    t_gw = (event.get("requestContext") or {}).get("timeEpoch") or int(time.time() * 1000)
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", "GET")
    path = (event.get("rawPath") or "").rstrip("/")
    route = path.rsplit("/api/tracker/", 1)[-1] if "/api/tracker/" in path else ""
    try:
        by = caller(event)
        if by is None:
            raise Refused(401, "not signed in")
        if store is None:
            if _store is None:
                _store = Store(TABLE)
            store = _store
        now = int(time.time() * 1000)
        if route == "time" and method == "GET":
            return respond(200, {"ok": True, "recv": t_gw, "send": int(time.time() * 1000)})
        if route == "devices" and method == "GET":
            return respond(200, {"ok": True, "devices": list(devices(store).values())})
        if route == "devices" and method == "POST":
            return respond(200, {"ok": True, "device": create_device(store, body_of(event), by, now)})
        if route == "devices" and method == "PATCH":
            return respond(200, {"ok": True, "device": patch_device(store, body_of(event), now)})
        if route == "states" and method == "POST":
            return respond(200, {"ok": True, "device": create_state(store, body_of(event), now)})
        if route == "states" and method == "PATCH":
            return respond(200, {"ok": True, "device": patch_state(store, body_of(event), now)})
        if route == "events" and method == "GET":
            params = event.get("queryStringParameters") or {}
            return respond(200, {"ok": True, "events": list_events(store, params)})
        if route == "events" and method == "POST":
            return respond(200, {"ok": True,
                                 "event": create_event(store, body_of(event), by, t_gw, now)})
        if route == "events" and method == "PATCH":
            return respond(200, {"ok": True, "event": patch_event(store, body_of(event), by, now)})
        if route == "export" and method == "GET":
            devs = devices(store)
            evs = []
            for did in devs:
                evs.extend(event_out(plain(e)) for e in store.events(did, newest_first=False))
            return respond(200, {"ok": True, "exported": now, "devices": list(devs.values()),
                                 "events": evs})
        return respond(404, {"ok": False, "error": f"no route for {method} {path}"})
    except Refused as r:
        return respond(r.status, {"ok": False, "error": r.message})
    except Exception as e:
        print(json.dumps({"error": type(e).__name__, "detail": str(e)[:300], "path": path}))
        return respond(500, {"ok": False, "error": "the tracker API failed; see its log"})
