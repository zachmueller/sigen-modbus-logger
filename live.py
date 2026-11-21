#!/usr/bin/env python3
"""
The live sidecar: what log.py has just archived, decoded and published to AWS IoT Core.

Usage:
  live.py              receive from log.py's tap, decode, publish over MQTT (the unit runs this)
  live.py --stdout     receive and decode, print each message as a JSON line; no MQTT, no network
  live.py --check      check the live settings and the certificate, and that paho imports; exit

It never opens a connection to the inverter. Every value it publishes was read by log.py, on
log.py's schedule, and offered here afterwards over a Unix datagram socket (see livetap.py) --
so the live stream adds no load to a device whose concurrent-client behaviour is unmeasured,
and anything that goes wrong in this process is invisible to the archive.

**What is published, per record** (topic `live_topic`, QoS 0, compact JSON):

    data    {"v":1,"t":<ms>,"m":<mask>,"lat":<ms>,"p":<fast_period_s>,"<key>":<value>,...}
    marker  {"v":1,"t":<ms>,"m":0,"lat":<ms>,"q":<quiet bound s>}

`t` is the archive record's own timestamp, so a live point names the archive record it came
from. Only metrics whose block arrived and is not a sentinel appear: a tick that lost one
block still publishes the others. A record that carries none of the seven -- the slow tiers'
own ticks -- publishes nothing. A record with no blocks at all is the logger probing a device
that is not answering, and becomes a MARKER, which is how the page can tell "the inverter is
silent" (orange) from "nothing reached the cloud" (grey), the distinction the archive viewer
draws as its two hatches.

**No replay, by design.** While the broker is unreachable, records are counted and dropped;
anything older than STALE_S when it arrives is dropped too. The hourly archive fills every
gap the live stream has, and a live chart that replayed a backlog would draw the past as if it
were now.

The MQTT client is paho-mqtt, from apt (python3-paho-mqtt), imported lazily and only here --
the same containment as boto3 in sync.py. `--stdout` and `--check` without the network work on
a host that has never installed it.
"""
import json
import math
import os
import signal
import socket
import ssl
import stat
import sys
import time

import config
import decode
import dump
import livefields
import livetap

STALE_S = 5.0            # older than this on arrival is no longer "live"
HEARTBEAT_S = 300        # the same five minutes as the logger's heartbeat
MANIFEST_RETRY_S = 30    # how often to look again for a plan's manifest that was missing
RECV_BYTES = 65536


def quiet_bound(cfg):
    """The longest silence, in seconds, between two records while the logger is still running.

    A marker carries this so the page knows how long to wait after one before concluding that
    NOTHING is arriving any more. It is the larger of two cases in log.py's capture loop:

      degraded: one probe every degrade_probe_s ticks, each up to 2 attempts x timeout_s,
                after which the scheduler rebases rather than catching up;
      not yet:  the first dead ticks, each 3 blocks x 2 attempts x timeout_s, plus a period.

    36 s and 42 s with the defaults. A fixed 35 s would paint "no data" inside every probe
    interval of an ordinary outage.
    """
    degraded = cfg["degrade_probe_s"] + 2 * cfg["timeout_s"]
    early = 6 * cfg["timeout_s"] + cfg["fast_period_s"]
    return int(math.ceil(max(degraded, early)))


def find_manifest(data_dir, plan_hash):
    """The newest manifest in data_dir for this plan, or None."""
    suffix = f"-{plan_hash}.manifest.json"
    try:
        names = sorted((n for n in os.listdir(data_dir)
                        if n.startswith("sigen-") and n.endswith(suffix)), reverse=True)
    except OSError:
        return None
    for name in names:
        try:
            with open(os.path.join(data_dir, name)) as fh:
                m = json.load(fh)
        except Exception:
            continue                 # half-written, or not ours: try the next one
        if m.get("plan_hash") == plan_hash and m.get("blocks"):
            return m
    return None


class Decoder:
    """Tap datagram -> message dict, or None. Keeps the counts the heartbeat reports."""

    def __init__(self, data_dir, bundle, q, log=None):
        self.data_dir = data_dir
        self.bundle = bundle
        self.q = q
        self.say = log or (lambda *_a, **_k: None)
        self.plans = {}          # plan hash -> (sizes, located, fast_period_s)
        self.retry_at = {}       # plan hash -> monotonic time to look for its manifest again
        self.counts = dict(received=0, data=0, markers=0, slow_only=0, stale=0,
                           undecodable=0, unknown_plan=0)

    def _plan(self, plan_hash):
        plan = self.plans.get(plan_hash)
        if plan is not None:
            return plan
        now = time.monotonic()
        if now < self.retry_at.get(plan_hash, 0):
            return None
        m = find_manifest(self.data_dir, plan_hash)
        if m is None:
            if plan_hash not in self.retry_at:
                self.say(f"  [plan] no manifest for plan {plan_hash} in {self.data_dir}; its "
                         f"records are dropped until one appears")
            self.retry_at[plan_hash] = now + MANIFEST_RETRY_S
            return None
        located, missing = decode.resolve(m, self.bundle, livefields.KEYS)
        plan = (decode.block_sizes(m), located, m.get("fast_period_s"))
        self.plans[plan_hash] = plan
        self.retry_at.pop(plan_hash, None)
        self.say(f"  [plan] decoding plan {plan_hash}: {len(located)} of "
                 f"{len(livefields.KEYS)} live metrics located"
                 + (f"; missing {', '.join(k for k, _ in missing)}" if missing else ""))
        return plan

    def message(self, datagram, now):
        self.counts["received"] += 1
        try:
            plan_hash, rec = livetap.parse(datagram)
        except ValueError:
            self.counts["undecodable"] += 1
            return None
        plan = self._plan(plan_hash)
        if plan is None:
            self.counts["unknown_plan"] += 1
            return None
        sizes, located, period = plan
        try:
            ts, mask, latency, blocks = decode.parse_record(sizes, rec)
        except ValueError:
            self.counts["undecodable"] += 1
            return None
        if now - ts > STALE_S:
            self.counts["stale"] += 1
            return None
        t = int(round(ts * 1000))
        if mask == 0:
            self.counts["markers"] += 1
            return {"v": 1, "t": t, "m": 0, "lat": latency, "q": self.q}
        msg = {"v": 1, "t": t, "m": mask, "lat": latency, "p": period}
        found = 0
        for key, loc in located:
            v = decode.value_of(blocks, loc)
            if v is None or isinstance(v, str):
                continue
            msg[key] = round(v, livefields.DECIMALS[key])
            found += 1
        if not found:
            self.counts["slow_only"] += 1
            return None
        self.counts["data"] += 1
        return msg


def encode(msg):
    return json.dumps(msg, separators=(",", ":"))


# ------------------------------------------------------------------------- sinks

class StdoutSink:
    """--stdout: the same messages, printed. Always 'connected'."""

    connected = True

    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.published = self.skipped = 0
        self.last = None

    def start(self):
        pass

    def publish(self, msg):
        print(encode(msg), file=self.out, flush=True)
        self.published += 1
        self.last = msg
        return True

    def stop(self):
        pass


class MqttSink:
    """paho-mqtt over mutual TLS. paho's own thread owns the connection and reconnects."""

    def __init__(self, cfg, log=None):
        import paho.mqtt.client as mqtt      # lazy: see the module docstring
        self.say = log or (lambda *_a, **_k: None)
        self.endpoint, self.port = cfg["live_endpoint"], cfg["live_port"]
        self.topic, self.client_id = cfg["live_topic"], cfg["live_client_id"]
        self.connected = False
        self.published = self.skipped = 0
        self.connects = 0
        self.last = None
        self.stopping = False
        kw = {"client_id": self.client_id, "clean_session": True,
              "protocol": mqtt.MQTTv311}
        # paho 2.0 made the callback API version a required first argument; Bookworm ships
        # 1.6, which does not have it. The callbacks below take *args, so either works.
        if hasattr(mqtt, "CallbackAPIVersion"):
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, **kw)
        else:
            c = mqtt.Client(**kw)
        ctx = ssl.create_default_context()        # system trust store: Amazon Root CA 1
        ctx.load_cert_chain(cfg["live_cert"], cfg["live_key"])
        c.tls_set_context(ctx)
        c.reconnect_delay_set(min_delay=1, max_delay=120)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        self.client = c

    @staticmethod
    def _code(rc):
        return getattr(rc, "value", rc)

    def _on_connect(self, *args):
        # v1: (client, userdata, flags, rc)   v2: (client, userdata, flags, reason, props)
        rc = args[3] if len(args) > 3 else None
        if self._code(rc) == 0:
            self.connects += 1
            self.connected = True
            self.say(f"  [mqtt] connected to {self.endpoint}:{self.port} as "
                     f"{self.client_id}" + (f" (reconnect {self.connects - 1})"
                                            if self.connects > 1 else ""))
        else:
            self.connected = False
            self.say(f"  [mqtt] connection refused: {rc}")

    def _on_disconnect(self, *args):
        # v1: (client, userdata, rc)   v2: (client, userdata, flags, reason, props)
        rc = args[3] if len(args) > 3 else (args[2] if len(args) > 2 else None)
        was = self.connected
        self.connected = False
        if self.stopping:
            self.say("  [mqtt] disconnected (shutting down)")
        elif was:
            self.say(f"  [mqtt] disconnected ({rc}); records are dropped until paho "
                     f"reconnects")

    def start(self):
        self.client.connect_async(self.endpoint, self.port, keepalive=60)
        self.client.loop_start()

    def publish(self, msg):
        # The flag, not paho's queue, decides: a QoS 0 publish made while disconnected must
        # be dropped here, never held and flushed on reconnect as a burst of the past.
        if not self.connected:
            self.skipped += 1
            return False
        info = self.client.publish(self.topic, encode(msg), qos=0)
        if getattr(info, "rc", 1) == 0:
            self.published += 1
            self.last = msg
            return True
        self.skipped += 1
        return False

    def stop(self):
        self.stopping = True
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()


# --------------------------------------------------------------------------- loop

def write_state(log_dir, state):
    """logs/live.state, for bin/status.sh. Best-effort and atomic; never raises."""
    if not log_dir:
        return
    path = os.path.join(log_dir, "live.state")
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, sort_keys=True)
            fh.write("\n")      # so `cat` in status.sh does not run onto the next line
        os.replace(tmp, path)
    except Exception:
        pass


def snapshot(decoder, sink):
    return dict(decoder.counts, at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                connected=bool(sink.connected), published=sink.published,
                dropped_disconnected=sink.skipped, last=sink.last)


def heartbeat_line(decoder, sink):
    c = decoder.counts
    return (f"  [{time.strftime('%Y-%m-%d %H:%M:%S')}] received {c['received']}  "
            f"published {sink.published}  markers {c['markers']}  "
            f"dropped: disconnected {sink.skipped}, stale {c['stale']}, "
            f"slow-only {c['slow_only']}, undecodable {c['undecodable']}, "
            f"unknown-plan {c['unknown_plan']}  "
            f"connected {'yes' if sink.connected else 'NO'}")


def run(cfg, sink, log=None, stop=None, bundle=None, heartbeat_s=HEARTBEAT_S):
    """Receive until stop["now"]. Returns the Decoder, for the caller's counts."""
    say = log or (lambda *_a, **_k: None)
    stop = stop if stop is not None else {"now": False}
    decoder = Decoder(cfg["data_dir"], bundle or dump.load_regmap(), quiet_bound(cfg), say)
    sock = livetap.bind(cfg["live_socket"])
    sock.settimeout(0.5)
    sink.start()
    last_beat = time.monotonic()
    try:
        while not stop["now"]:
            try:
                data = sock.recv(RECV_BYTES)
            except (socket.timeout, InterruptedError):
                data = None
            if data:
                msg = decoder.message(data, time.time())
                if msg is not None:
                    sink.publish(msg)
            if time.monotonic() - last_beat >= heartbeat_s:
                last_beat = time.monotonic()
                say(heartbeat_line(decoder, sink))
                write_state(cfg.get("log_dir"), snapshot(decoder, sink))
    finally:
        try:
            sink.stop()
        finally:
            sock.close()
            try:
                os.unlink(cfg["live_socket"])
            except OSError:
                pass
        write_state(cfg.get("log_dir"), snapshot(decoder, sink))
    return decoder


# -------------------------------------------------------------------------- check

def check(cfg, need_mqtt=True):
    """Problems with the live settings, as a list of strings. Empty means ready."""
    problems = []
    sockpath = cfg["live_socket"]
    p = livetap.path_problem(sockpath)
    if p:
        problems.append(f"live_socket: {p}")
    elif sockpath.startswith("/run/") and \
            os.path.dirname(sockpath) != f"/run/{cfg['live_unit']}":
        # The unit's RuntimeDirectory= creates /run/<live_unit> and nothing else under /run,
        # and the unit cannot write anywhere else there.
        problems.append(f"live_socket {sockpath} is under /run but not in "
                        f"/run/{cfg['live_unit']}/, the directory the unit creates")
    if not need_mqtt:
        return problems
    if not cfg.get("live_endpoint"):
        problems.append("no live_endpoint: the IoT Core data endpoint, host only "
                        "(aws iot describe-endpoint --endpoint-type iot:Data-ATS)")
    elif "://" in cfg["live_endpoint"] or "/" in cfg["live_endpoint"]:
        problems.append(f"live_endpoint {cfg['live_endpoint']!r} must be a bare host name")
    for key in ("live_cert", "live_key"):
        path = cfg.get(key)
        if not path or not os.path.isfile(path):
            problems.append(f"{key}: no file at {path}")
            continue
        try:
            with open(path):
                pass
        except OSError as e:
            problems.append(f"{key}: cannot read {path}: {e}")
            continue
        if key == "live_key":
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o077:
                problems.append(f"live_key {path} is mode {mode:o}; it is the credential, "
                                f"chmod 600")
    if not any(p.startswith(("live_cert", "live_key")) for p in problems):
        try:
            ssl.create_default_context().load_cert_chain(cfg["live_cert"], cfg["live_key"])
        except Exception as e:
            problems.append(f"live_cert and live_key do not load as a pair: {e}")
    try:
        import paho.mqtt                     # noqa: F401  (lazy, as in MqttSink)
    except ImportError:
        problems.append("paho-mqtt is not installed: sudo apt install python3-paho-mqtt")
    return problems


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    unknown = [a for a in args if a not in ("--stdout", "--check")]
    if unknown:
        print(f"unknown argument(s) {' '.join(unknown)}; try --help", file=sys.stderr)
        return 2
    cfg = config.load(require_host=False)

    def say(s):
        print(s, flush=True)

    if "--check" in args:
        problems = check(cfg)
        print(f"live stream: {'ENABLED' if cfg['live_enabled'] else 'disabled'} "
              f"(live_enabled)")
        print(f"  socket   {cfg['live_socket']}")
        print(f"  endpoint {cfg['live_endpoint'] or '-'}:{cfg['live_port']}  "
              f"topic {cfg['live_topic']}  client {cfg['live_client_id']}")
        print(f"  cert     {cfg['live_cert']}")
        print(f"  key      {cfg['live_key']}")
        try:
            import paho.mqtt
            print(f"  paho     {paho.mqtt.__version__}")
        except ImportError:
            pass
        for p in problems:
            print(f"  PROBLEM  {p}")
        print("  ready" if not problems else f"  {len(problems)} problem(s)")
        return 1 if problems else 0

    if "--stdout" in args:
        problems = check(cfg, need_mqtt=False)
        sink = StdoutSink()
    else:
        if not cfg["live_enabled"]:
            # Exit 0, and the unit is Restart=on-failure: a host that has turned live off
            # should get one line saying so, not a restart every thirty seconds.
            say("live_enabled is false in config.json; nothing to do")
            return 0
        problems = check(cfg)
        sink = None
    if problems:
        for p in problems:
            print(f"live.py: {p}", file=sys.stderr)
        return 1
    if sink is None:
        sink = MqttSink(cfg, say)

    stop = {"now": False}

    def on_sig(*_):
        stop["now"] = True
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    if not isinstance(sink, StdoutSink):
        say(f"Sigenergy live stream: {cfg['live_socket']} -> "
            f"{cfg['live_endpoint']}:{cfg['live_port']} topic {cfg['live_topic']}")
        say(f"  {len(livefields.KEYS)} metrics; markers carry q={quiet_bound(cfg)} s; "
            f"records older than {STALE_S:g} s are dropped; nothing is replayed")
        say("")
    run(cfg, sink, say if not isinstance(sink, StdoutSink) else
        (lambda s: print(s, file=sys.stderr, flush=True)), stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
