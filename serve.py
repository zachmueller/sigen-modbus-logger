#!/usr/bin/env python3
"""
Local web viewer for a raw archive written by log.py. READ-ONLY, stdlib only.

    serve.py [--port N] [--bind ADDR] [--data-dir DIR] [--check] [--verbose]

Serves one page that plots what the inverter and the house have been doing,
decoded on demand from the bytes already on disk. Defaults to the last
web_default_hours (6) and scrolls back over the whole series.

**It never touches the device.** It imports decode/dump/lib for their decode half
only; lib.Modbus is never constructed, and tests/test_web.py asserts that this
file cannot become a second Modbus client. That matters because concurrent-client
behaviour against the inverter is unmeasured -- see README, "Safety and privacy".

Two things worth knowing about the design:

  - The server owns the *presentation contract*: PANELS below says which fields
    make up which chart, and /api/meta ships it to the page. Adding a chart is a
    dict here, not new JavaScript.
  - The page asks for a window, not for files. Bucket width, stride and which
    files to read are series.py's business, and every one of them is reported back
    so the page can say how the numbers were made.
  - The page is source-agnostic. web/app.js reads every answer through one seam,
    so the same renderer serves this server and the hosted viewer, which composes
    the same JSON out of precomputed tiles. Sharing a view is a URL there, which is
    why this server no longer writes self-contained HTML files.

Host, port, bind address and the default window come from config.json; see
config.py. CLI flags override it.
"""
import json
import os
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import config
import decode
import series
import tiles

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")

# Only these are servable, by name. An allowlist rather than a path-sanitising
# routine: there is no arithmetic to get wrong, and the archive, the manifests and
# config.json all live one directory up from web/.
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/charts.js": ("charts.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

# The live strip. plant_* rather than the inverter_* duplicates (FINDINGS 7).
LIVE_FIELDS = [
    "plant_sigen_photovoltaic_power", "plant_general_load_power", "plant_ess_power",
    "plant_grid_sensor_active_power", "plant_ess_soc", "plant_ess_soh",
    "plant_running_state", "plant_on_off_grid_status", "plant_ems_work_mode",
    "plant_ess_average_cell_temperature", "inverter_pcs_internal_temperature",
    "inverter_grid_frequency", "inverter_phase_a_voltage", "inverter_power_factor",
    "plant_pv_total_daily_generation", "plant_ess_available_max_charging_power",
    "plant_ess_available_max_discharging_power", "plant_system_time",
    "plant_general_alarm1", "plant_general_alarm2", "plant_general_alarm3",
    "plant_general_alarm4", "plant_general_alarm6", "plant_general_alarm7",
]

# Which chart is made of which fields. Slots are categorical palette positions,
# assigned to an ENTITY and never re-used for another one, so grid is the same
# colour on every chart it appears on.
PANELS = [
    {
        "id": "power", "title": "Power flow", "unit": "kW", "kind": "lines",
        "zero": True, "height": 260,
        "note": "Grid > 0 importing, < 0 exporting. Battery > 0 charging. "
                "PV = load + battery charge + export.",
        "series": [
            {"key": "plant_grid_sensor_active_power", "label": "Grid", "slot": 1},
            {"key": "plant_general_load_power", "label": "Load", "slot": 2},
            {"key": "plant_ess_power", "label": "Battery", "slot": 3},
            {"key": "plant_sigen_photovoltaic_power", "label": "PV", "slot": 4},
        ],
    },
    {
        "id": "soc", "title": "Battery state of charge", "unit": "%",
        "kind": "lines", "domain": [0, 100], "height": 150,
        "note": "Its own chart rather than a second axis on the power plot.",
        "series": [{"key": "plant_ess_soc", "label": "SOC", "slot": 3}],
    },
    {
        "id": "strings", "title": "PV string current", "unit": "A",
        "kind": "lines", "height": 190, "ramp": True,
        "note": "pv1..pv4 only: the other 32 documented channels return the -1 "
                "absent marker on this unit. Power needs V x I per sample, which "
                "bucketed V and I cannot reconstruct -- so current, which is what "
                "shows a shaded string.",
        "series": [
            {"key": "inverter_pv1_current", "label": "pv1", "slot": 1},
            {"key": "inverter_pv2_current", "label": "pv2", "slot": 2},
            {"key": "inverter_pv3_current", "label": "pv3", "slot": 3},
            {"key": "inverter_pv4_current", "label": "pv4", "slot": 4},
        ],
    },
    {
        "id": "stringv", "title": "PV string voltage", "unit": "V",
        "kind": "lines", "height": 190, "ramp": True, "collapsed": True,
        "note": "Strings differ in module count, so their voltages are not "
                "comparable to each other -- compare each to its own settled Voc "
                "(FINDINGS 10). Full voltage at zero current is the MPPT "
                "self-check, not a fault.",
        "series": [
            {"key": "inverter_pv1_voltage", "label": "pv1", "slot": 1},
            {"key": "inverter_pv2_voltage", "label": "pv2", "slot": 2},
            {"key": "inverter_pv3_voltage", "label": "pv3", "slot": 3},
            {"key": "inverter_pv4_voltage", "label": "pv4", "slot": 4},
        ],
    },
    {
        "id": "temps", "title": "Temperatures", "unit": "degC", "kind": "lines",
        "height": 150,
        "series": [
            {"key": "inverter_pcs_internal_temperature", "label": "PCS", "slot": 1},
            {"key": "plant_ess_average_cell_temperature", "label": "Cell avg",
             "slot": 2},
        ],
    },
    {
        "id": "freq", "title": "Grid frequency", "unit": "Hz", "kind": "lines",
        "height": 130, "collapsed": True,
        "series": [{"key": "inverter_grid_frequency", "label": "Frequency",
                    "slot": 1}],
    },
    {
        "id": "volts", "title": "Grid voltage (phase A)", "unit": "V",
        "kind": "lines", "height": 130, "collapsed": True,
        "series": [{"key": "inverter_phase_a_voltage", "label": "Voltage",
                    "slot": 1}],
    },
    {
        "id": "pf", "title": "Power factor", "unit": "", "kind": "lines",
        "height": 130, "collapsed": True, "domain": [-1, 1],
        "note": "Typed U16 upstream; read as S16, since a power factor is bounded "
                "to [-1, 1].",
        "series": [{"key": "inverter_power_factor", "label": "Power factor",
                    "slot": 1}],
    },
    {
        "id": "state", "title": "Plant state and alarms", "kind": "state",
        "height": 120,
        "note": "A band is one bucket wide; a bucket that contained a transition "
                "is marked. Alarm words are OR-ed over the bucket, so a bit set "
                "for one sample still shows.",
        # `short` is what fits in the axis gutter, which is as wide as the line
        # charts' so the shared crosshair stays aligned; `label` is the full name,
        # used in the tooltip and as the row's SVG title.
        "series": [
            {"key": "plant_running_state", "label": "Running state", "short": "State"},
            {"key": "plant_on_off_grid_status", "label": "Grid connection",
             "short": "Grid"},
            {"key": "plant_ems_work_mode", "label": "EMS work mode", "short": "EMS"},
        ],
        "alarms": ["plant_general_alarm1", "plant_general_alarm2",
                   "plant_general_alarm3", "plant_general_alarm4",
                   "plant_general_alarm6", "plant_general_alarm7"],
    },
    {
        "id": "health", "title": "Capture health", "unit": "ms", "kind": "latency",
        "height": 170, "collapsed": True,
        "note": "Tick latency over records that returned data. An outage probe's "
                "latency is the socket timeout, not the device's, so those are "
                "counted separately and shaded instead.",
    },
]

# Window totals come from the lifetime counters, not from integrating power: they
# are the independent cross-check, and they agree to about a watt-hour.
ENERGY_TILES = [
    {"key": "plant_accumulated_pv_energy", "label": "PV generated"},
    {"key": "plant_accumulated_grid_import_energy", "label": "Grid imported"},
    {"key": "plant_accumulated_grid_export_energy", "label": "Grid exported"},
    {"key": "plant_accumulated_battery_charge_energy", "label": "Battery charged"},
    {"key": "plant_accumulated_battery_discharge_energy",
     "label": "Battery discharged"},
    {"key": "plant_accumulated_consumed_energy", "label": "Load consumed"},
]

MAX_RAW_CSV_ROWS = 200000


def panel_keys(ids=None):
    """Fields the named panels need, in a stable order."""
    keys = []
    for p in PANELS:
        if ids is not None and p["id"] not in ids:
            continue
        for s in p.get("series", []):
            keys.append(s["key"])
        keys.extend(p.get("alarms", []))
    for t in ENERGY_TILES:
        if ids is None or "energy" in ids:
            keys.append(t["key"])
    return list(dict.fromkeys(keys))


# ----------------------------------------------------------------- the endpoints

class Viewer:
    """Everything the handler needs, so the handler stays about HTTP."""

    def __init__(self, cfg, data_dir, verbose=False):
        self.cfg = cfg
        self.data_dir = data_dir
        self.verbose = verbose
        self.cache = series.SummaryCache()
        # One decode at a time, across every request thread and the warm thread.
        # The server is threaded so a slow request cannot block the page's assets,
        # but decoding is CPU-bound, and N simultaneous requests would otherwise
        # occupy N cores while the logger is trying to hit a 2 s tick. Measured:
        # three concurrent 6 h windows during a deploy took the logger's 5-minute
        # median from ~90 ms to 164 ms and one tick to 1943 ms, past its own
        # 1800 ms soak gate. Re-entrant, because /api/csv builds a window.
        self.decode_lock = threading.RLock()
        self.warmer = series.Warmer(self.cache, self.decode_lock)
        self.started = time.time()
        self.requests = 0
        self.waited_s = 0.0

    def series_or_none(self):
        return series.newest_series(self.data_dir)

    # -- /api/meta ---------------------------------------------------------

    def meta(self):
        s = self.series_or_none()
        found = series.discover(self.data_dir)
        out = {
            "now": time.time(),
            "data_dir": self.data_dir,
            "default_hours": self.cfg["web_default_hours"],
            "panels": PANELS,
            "energy_tiles": ENERGY_TILES,
            "live_fields": LIVE_FIELDS,
            "bucket_ladder": list(series.BUCKET_LADDER),
            "samples_per_bucket": series.SAMPLES_PER_BUCKET,
            "plans": sorted(found),
        }
        if s is None:
            out["ok"] = False
            out["reason"] = (
                f"no decodable archive in {self.data_dir}. The viewer needs both "
                f"*-<planhash>.bin/.bin.gz files and the *-<planhash>.manifest.json "
                f"written beside them, in that directory (it does not recurse -- "
                f"point --data-dir at a subdirectory to view a superseded series).")
            return out
        first, last = s.extent()
        spans = s.spans()
        manifest = s.manifest
        device = dict(manifest.get("device") or {})
        if not self.cfg["web_show_identity"]:
            # A manifest records model, serial and the address polled, which
            # identifies an installation. The page is on the LAN; the serial is not
            # needed to read a chart. Model stays: it explains the hardware.
            kept = {"model": device.get("model")}
            if not kept["model"]:
                # The identity read is the first thing the logger does, so a
                # manifest written while the device was unreachable has none of it.
                # Say why rather than showing a bare "?".
                kept["model"] = "unknown"
                kept["why"] = ("this manifest was written while the device was not "
                               "answering: " + str(device.get("error") or
                                                   device.get("omitted") or "no identity"))
            kept["withheld"] = ("serial, firmware and inverter address are withheld; "
                                "set web_show_identity to show them")
            device = kept
        out.update({
            "ok": True,
            "plan_hash": s.plan_hash,
            "manifest": os.path.basename(s.manifest_path),
            "fast_period_s": s.fast_period_s,
            "device": device,
            "started_at": manifest.get("started_at"),
            "blocks": [{"label": b["label"], "addr": b["addr"], "count": b["count"],
                        "period_s": b["period_s"]} for b in manifest["blocks"]],
            "extent": {"first_ts": first, "last_ts": last, "files": len(spans),
                       "bytes": sum(sp.size for sp in spans)},
            "catalog": series.catalog(s),
            "tz": series.tz_runs([time.time()]),
        })
        if self.cfg["web_show_identity"]:
            out["host"] = manifest.get("host")
            out["port"] = manifest.get("port")
        return out

    # -- /api/window -------------------------------------------------------

    def window(self, q, keep_tiles=False):
        s = self.series_or_none()
        if s is None:
            raise Http(503, "no decodable archive; see /api/meta")
        first, last = s.extent()
        now = time.time()
        end = _num(q, "end", None)
        hours = _num(q, "hours", self.cfg["web_default_hours"])
        if end is None or end <= 0:
            end = max(now, last or now)
        end = min(end, now + 60)
        start = _num(q, "start", end - max(0.01, hours) * 3600.0)
        if start >= end:
            raise Http(400, "start must be before end")

        ids = _list(q, "panels")
        keys = panel_keys(None if not ids or "all" in ids else set(ids))
        extra = _list(q, "fields")
        known = {c["key"] for c in series.catalog(s)}
        unknown = [k for k in extra if k not in known]
        keys += [k for k in extra if k in known]
        bucket = _num(q, "bucket", None)
        if bucket:
            bucket = min(series.BUCKET_LADDER,
                         key=lambda b: abs(b - bucket))

        t_wait = time.perf_counter()
        with self.decode_lock:
            self.waited_s += time.perf_counter() - t_wait
            win = series.window(s, start, end, keys, bucket_s=bucket,
                                cache=self.cache, warmer=self.warmer)
        cat = {c["key"]: c for c in series.catalog(s)}
        out = {k: win[k] for k in ("start", "end", "bucket_s", "stride", "t", "tz",
                                  "records", "files", "files_read", "pending_files",
                                  "plan_hash", "samples_per_bucket", "took_ms",
                                  "unreadable")}
        out["ok"] = True
        out["health"] = win["health"]
        out["extent"] = {"first_ts": first, "last_ts": last}
        out["warming"] = self.warmer.pending()
        out["unknown_fields"] = unknown
        # A lifetime counter is on screen as one number, not 720 points, so its arrays
        # are dropped unless something asked for the field by name. tiles.columns() owns
        # that rule and the other four, because the hosted viewer's precomputed tiles
        # have to come out identically -- see tiles.py.
        counter_keys = ({t["key"] for t in ENERGY_TILES} - set(extra)
                        if not keep_tiles else frozenset())
        out["series"] = tiles.columns(win, cat, keys, counter_keys)
        out["energy"] = series.energy(win, [t["key"] for t in ENERGY_TILES])
        pv = out["energy"].get("plant_accumulated_pv_energy", {}).get("kwh")
        ex = out["energy"].get("plant_accumulated_grid_export_energy", {}).get("kwh")
        if pv:
            # Share of generation used on site rather than exported. Undefined with
            # no generation, which is why this is absent rather than 0.
            out["energy"]["self_consumption"] = round(max(0.0, pv - (ex or 0.0)) / pv, 4)
        return out

    # -- /api/latest -------------------------------------------------------

    def latest(self):
        s = self.series_or_none()
        if s is None:
            raise Http(503, "no decodable archive; see /api/meta")
        with self.decode_lock:
            out = series.latest(s, LIVE_FIELDS)
        cat = {c["key"]: c for c in series.catalog(s)}
        labels, alarms = {}, {}
        for key, v in list(out.get("values", {}).items()):
            meta = cat.get(key, {})
            if meta.get("enum") and v is not None:
                labels[key] = meta["enum"].get(str(int(v)),
                                               f"undocumented code {int(v)}")
            if meta.get("alarm_table") and v:
                alarms[key] = series.alarm_bits(key, v)
            if key in ("plant_system_time",) and v:
                labels[key] = decode.fmt_cell(key, v)
        out["labels"] = labels
        out["alarms"] = alarms
        out["plan_hash"] = s.plan_hash
        return out

    # -- /api/csv ----------------------------------------------------------

    def csv(self, q):
        """The window as CSV. `raw=1` gives per-record rows instead of buckets.

        The bucketed form is what the page's table view shows, so the download and
        the screen agree. The raw form is the same shape decode.py emits.
        """
        s = self.series_or_none()
        if s is None:
            raise Http(503, "no decodable archive; see /api/meta")
        if q.get("raw", ["0"])[0] not in ("0", "", "false"):
            return self._csv_raw(s, q)
        win = self.window(q, keep_tiles=True)
        keys = [k for k in win["series"] if not win["series"][k].get("empty")]
        head = ["time"]
        for k in keys:
            col = win["series"][k]
            if "mean" in col:
                head += [f"{k}_mean", f"{k}_min", f"{k}_max"]
            elif "last" in col:
                head.append(f"{k}_last")
            else:
                head.append(f"{k}_bits")
        rows = ["# sigen viewer  bucket %ss  stride %s  records %s"
                % (win["bucket_s"], win["stride"], win["records"]),
                ",".join(head)]
        for i, t in enumerate(win["t"]):
            cells = [time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))]
            for k in keys:
                col = win["series"][k]
                for name in ("mean", "min", "max") if "mean" in col else (
                        ("last",) if "last" in col else ("bits",)):
                    v = col[name][i]
                    cells.append("" if v is None else f"{v:g}")
            rows.append(",".join(cells))
        return "\n".join(rows) + "\n"

    def _csv_raw(self, s, q):
        with self.decode_lock:
            return self._csv_raw_locked(s, q)

    def _csv_raw_locked(self, s, q):
        now = time.time()
        end = _num(q, "end", None) or now
        hours = _num(q, "hours", self.cfg["web_default_hours"])
        start = _num(q, "start", end - max(0.01, hours) * 3600.0)
        keys = _list(q, "fields") or decode.DEFAULT_FIELDS
        located, missing = decode.resolve(s.manifest, series.bundle(), keys)
        cols = [k for k, _ in located]
        rows = ["host_time,latency_ms," + ",".join(cols)]
        n = 0
        for span in s.files_for(start, end):
            for ts, latency, blocks in decode.records(s.manifest, [span.path]):
                if not (start <= ts < end):
                    continue
                cells = [time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
                         str(latency)]
                cells += [decode.fmt_cell(k, decode.value_of(blocks, loc))
                          for k, loc in located]
                rows.append(",".join(cells))
                n += 1
                if n >= MAX_RAW_CSV_ROWS:
                    rows.append(f"# stopped at {MAX_RAW_CSV_ROWS} rows -- narrow the "
                                f"window, or use decode.py for a bulk export")
                    return "\n".join(rows) + "\n"
        if missing:
            rows.append("# skipped: " + ", ".join(f"{k} ({why})" for k, why in missing))
        return "\n".join(rows) + "\n"

    # -- --check -----------------------------------------------------------

    def report(self):
        """What the server can see, without starting it. For debugging a deploy."""
        m = self.meta()
        if not m.get("ok"):
            print(m["reason"])
            return False
        first = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["extent"]["first_ts"]))
        last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["extent"]["last_ts"]))
        print(f"data dir   {self.data_dir}")
        print(f"plan       {m['plan_hash']}  ({m['manifest']}, "
              f"{m['fast_period_s']}s fast tier)")
        print(f"device     {m['device'].get('model', '?')}")
        print(f"archive    {m['extent']['files']} files, "
              f"{m['extent']['bytes'] / 1e6:.1f} MB, {first} -> {last}")
        print(f"plottable  {len(m['catalog'])} fields "
              f"({sum(1 for c in m['catalog'] if 'duplicate_of' in c)} marked as "
              f"duplicates of another register)")
        if len(m["plans"]) > 1:
            print(f"NOTE       {len(m['plans'])} plans in this directory "
                  f"({', '.join(m['plans'])}); the viewer shows {m['plan_hash']}, "
                  f"the one with the newest record")
        t0 = time.perf_counter()
        w = self.window({"hours": [str(self.cfg["web_default_hours"])]})
        print(f"window     {self.cfg['web_default_hours']}h -> {len(w['t'])} buckets of "
              f"{w['bucket_s']}s, stride {w['stride']}, {w['records']} records, "
              f"{w['files_read']}/{w['files']} files, "
              f"{(time.perf_counter() - t0) * 1000:.0f} ms")
        lt = self.latest()
        age = lt.get("data_age_s")
        print(f"latest     {'data' if lt['ok'] else 'NO DATA'} "
              + (f"{age:.0f}s old" if age is not None else "")
              + (", device not answering" if not lt.get("device_answering") else "")
              + (", LOGGER MAY BE STALLED" if lt.get("logger_stalled") else ""))
        return True


class Http(Exception):
    """An HTTP status to return, rather than a traceback."""

    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def _num(q, name, default):
    raw = q.get(name, [None])[0]
    if raw in (None, "", "now"):
        return default
    try:
        return float(raw)
    except ValueError:
        raise Http(400, f"{name}={raw!r} is not a number")


def _list(q, name):
    raw = q.get(name, [None])[0]
    if not raw:
        return []
    return [x for x in raw.split(",") if x]


# ---------------------------------------------------------------------- the HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "sigen-viewer"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        viewer = self.server.viewer
        viewer.requests += 1
        parsed = urlparse(self.path)
        route, q = parsed.path, parse_qs(parsed.query)
        try:
            if route in STATIC:
                return self._static(route)
            if route == "/api/meta":
                return self._json(viewer.meta())
            if route == "/api/window":
                return self._json(viewer.window(q))
            if route == "/api/latest":
                return self._json(viewer.latest())
            if route == "/api/csv":
                return self._text(viewer.csv(q), "text/csv; charset=utf-8",
                                  filename="sigen-window.csv")
            if route == "/api/stats":
                return self._json({"cache": viewer.cache.stats(),
                                   "warming": viewer.warmer.pending(),
                                   "warmed": viewer.warmer.done,
                                   "requests": viewer.requests,
                                   "decode_wait_s": round(viewer.waited_s, 3),
                                   "uptime_s": time.time() - viewer.started})
            raise Http(404, f"no such path {route}")
        except Http as e:
            self._json({"ok": False, "error": e.message}, status=e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass                       # the browser navigated away mid-response
        except Exception:
            traceback.print_exc()
            self._json({"ok": False,
                        "error": "internal error; see the viewer log"}, status=500)

    def _static(self, route):
        name, ctype = STATIC[route]
        path = os.path.join(WEB_DIR, name)
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            raise Http(404, f"{name} is missing from {WEB_DIR}")
        self._send(body, ctype, cache="no-cache")

    def _json(self, obj, status=200):
        body = json.dumps(obj, allow_nan=False, default=str).encode()
        self._send(body, "application/json; charset=utf-8", status=status)

    def _text(self, text, ctype, filename=None):
        extra = ({"Content-Disposition": f'attachment; filename="{filename}"'}
                 if filename else None)
        self._send(text.encode(), ctype, extra=extra)

    def _send(self, body, ctype, status=200, cache="no-store", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        # Nothing here is cross-origin, and the page is same-origin by
        # construction: no CORS header, so a hostile page on the LAN cannot read
        # the archive through a visitor's browser.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        """Quiet by default: a 10 s poll would otherwise write ~9k lines a day.

        Errors and page loads still log, so the viewer log stays useful.
        """
        line = fmt % args
        path = getattr(self, "path", "")     # unset if the request line was garbage
        noisy = path.startswith("/api/") and " 200 " in line
        if self.server.viewer.verbose or not noisy:
            # On a dual-stack socket an IPv4 client arrives as ::ffff:192.168.1.5;
            # log the address someone would recognise.
            who = self.client_address[0]
            if who.startswith("::ffff:"):
                who = who[7:]
            sys.stdout.write("  [%s] %s %s\n" %
                             (time.strftime("%Y-%m-%d %H:%M:%S"), who, line))
            sys.stdout.flush()


WILDCARD = ("0.0.0.0", "", "*", "::")


class Server(ThreadingHTTPServer):
    """Dual-stack when asked to bind everything.

    An IPv4-only listener is a trap on a LAN with IPv6, which every modern macOS
    network is. mDNS advertises the host's AAAA records alongside its A record, and
    macOS prefers IPv6 -- so a browser opening http://host.local:8787 tries the v6
    address first and gets CONNECTION REFUSED from a v4-only socket. curl survives
    it (Happy Eyeballs falls back to v4 after the refusal, which is why the API
    tested fine from the command line) and a browser shows an error page. So a
    wildcard bind opens an AF_INET6 socket with IPV6_V6ONLY off, which accepts both
    families on one socket.

    An explicit address is honoured as given: 127.0.0.1 stays v4-only, ::1 stays v6.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, viewer):
        self.viewer = viewer
        host, port = addr
        if host in WILDCARD:
            self.address_family = socket.AF_INET6
            try:
                super().__init__(("::", port), Handler)
                return
            except OSError:
                # A host with IPv6 disabled outright. v4-only is then correct.
                self.address_family = socket.AF_INET
                addr = ("0.0.0.0", port)
        elif ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__(addr, Handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass          # then it is v6-only; the message below says so
        super().server_bind()

    def families(self):
        """What the listening socket actually accepts. Reported at startup, because
        "it is listening" and "your browser can reach it" are different claims."""
        if self.address_family != socket.AF_INET6:
            return "IPv4"
        try:
            v6only = self.socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
        except OSError:
            v6only = 1
        return "IPv6 only" if v6only else "IPv4 and IPv6"

    def handle_error(self, request, client_address):
        """A browser that navigates away mid-response is not an error.

        socketserver's default prints a full traceback, which over a long run fills
        the viewer's log with noise that looks like a fault. Real errors still
        print -- the handler catches those itself and logs them.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    args = sys.argv[1:]
    port = bind = data_dir = None
    mode, verbose = "serve", False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--port":
            port = int(args[i + 1]); i += 2
        elif a == "--bind":
            bind = args[i + 1]; i += 2
        elif a in ("--data-dir", "--out"):
            data_dir = args[i + 1]; i += 2
        elif a == "--check":
            mode = "check"; i += 1
        elif a in ("-v", "--verbose"):
            verbose = True; i += 1
        elif a in ("-h", "--help"):
            sys.exit(__doc__.strip())
        else:
            sys.exit(f"unknown argument {a!r}; try --help")

    # require_host=False: the viewer never connects to the inverter, so an
    # archive copied to a machine that has no inverter still opens.
    cfg = config.load(require_host=False, overrides={
        "web_port": port, "web_bind": bind, "data_dir": data_dir})
    viewer = Viewer(cfg, cfg["data_dir"], verbose)

    if mode == "check":
        sys.exit(0 if viewer.report() else 1)

    try:
        os.nice(5)   # the logger is the latency-sensitive process on this host
    except (OSError, AttributeError):
        pass

    addr = (cfg["web_bind"], cfg["web_port"])
    try:
        httpd = Server(addr, viewer)
    except OSError as e:
        sys.exit(f"cannot bind {addr[0]}:{addr[1]}: {e}\n"
                 f"  Another viewer may already be running: "
                 f"pgrep -f serve.py")
    port = cfg["web_port"]
    ip = _lan_ipv4() if cfg["web_bind"] in WILDCARD else None
    print(f"Sigenergy viewer on http://{_display_host(cfg['web_bind'])}:{port}")
    if ip:
        # Lead with the address that always works. A .local name resolves fine and
        # can still hand a browser an IPv6 address it has no route to, which shows
        # up as ERR_ADDRESS_UNREACHABLE and looks like the server is down.
        print(f"  open     http://{ip}:{port}/   <- use this if the .local name "
              f"gives ERR_ADDRESS_UNREACHABLE")
    print(f"  archive  {cfg['data_dir']}")
    print(f"  socket   accepts {httpd.families()}")
    print(f"  read-only: this process never opens a Modbus connection")
    if cfg["web_bind"] not in ("127.0.0.1", "localhost", "::1"):
        print(f"  LAN-visible on {cfg['web_bind']}:{cfg['web_port']} — anyone who can "
              f"reach this host can read your telemetry. Set web_bind to 127.0.0.1 "
              f"and use an SSH tunnel if that is not what you want.")
    if not cfg["web_show_identity"]:
        print("  identity withheld: model only, no serial/firmware/inverter address")
    # The plist runs with -u, but a manual `python3 serve.py > log` block-buffers,
    # and a banner that appears minutes later is a banner nobody sees.
    print(flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()


def _display_host(bind):
    if bind in WILDCARD:
        try:
            return socket.gethostname()
        except OSError:
            return "localhost"
    return bind


def _lan_ipv4():
    """This host's outbound IPv4 address, or None.

    Worth printing because a `.local` name is not a reliable way to reach the
    viewer: mDNS advertises the host's IPv6 addresses alongside its IPv4 one, a
    browser prefers IPv6, and a client with no route to the advertised ULA or
    link-local address fails with ERR_ADDRESS_UNREACHABLE -- having resolved the
    name perfectly well. An IP works from anything on the LAN.

    Uses a connect() on a UDP socket to a TEST-NET-1 address, which sends no
    packets and touches no device; it only asks the kernel which local address it
    would route from.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    main()
