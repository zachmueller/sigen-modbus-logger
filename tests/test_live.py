#!/usr/bin/env python3
"""
Offline tests for the live stream's Pi side: livetap.py, live.py, livefields.py and the parts
of log.py, decode.py and config.py they touch. NO HARDWARE, NO NETWORK, NO MQTT BROKER.

    python3 -m unittest discover -s tests -v

What these are for. The live stream is an experiment riding beside a logger whose archive is
the record of truth, so most of what matters here is negative:

  - nothing the live path does can stop, slow or crash capture;
  - it reads nothing from the device that log.py did not already read;
  - it never decodes a record against the wrong plan's offsets;
  - and it never replays the past as if it were now.

Each of those is a test rather than a comment.
"""
import base64
import datetime
import gzip
import hashlib
import importlib.util
import io
import itertools
import json
import math
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config          # noqa: E402
import decode          # noqa: E402
import dump            # noqa: E402
import livefields      # noqa: E402
import live            # noqa: E402
import livetap         # noqa: E402
import log             # noqa: E402
import panels          # noqa: E402
from test_offline import DeadModbus, FakeModbus, base_cfg   # noqa: E402
from test_web import ViewerFixture                          # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# sha256 prefix of a presigned URL for fixed inputs (TestPresign). Recorded only after the
# signature it contains matched botocore's independent SigV4 chain, and after the same code
# connected to the real IoT endpoint; it pins that output so a refactor cannot drift from it.
KNOWN_PRESIGN = "a24b60bd8149b619"

# One fast-tier record: the 12-byte header plus plant_live, plant_energy and inv_ac_dc.
FAST_RECORD = 12 + (124 + 87 + 106) * 2


def short_tmp():
    """A temp dir short enough for a Unix socket path on macOS (104 bytes) as well as Linux."""
    return tempfile.mkdtemp(prefix="lv-", dir="/tmp")


def recv_all(sock, wait=0.2):
    out = []
    sock.settimeout(wait)
    while True:
        try:
            out.append(sock.recv(65536))
        except socket.timeout:
            return out


class AnsweringModbus(FakeModbus):
    """FakeModbus with the real transport's constructor, so capture() can build one.

    capture() calls Modbus(host, port, timeout); FakeModbus takes a register image instead,
    which is why nothing drove the loop with a HEALTHY device before.
    """

    def __init__(self, host=None, port=502, timeout=6.0):
        super().__init__()
        if host is not None:
            self.host = host


# --------------------------------------------------------------------------- tap

class TestFraming(unittest.TestCase):
    def test_a_datagram_round_trips(self):
        d = livetap.frame("08c047b8", b"\x01\x02record")
        self.assertEqual(livetap.parse(d), ("08c047b8", b"\x01\x02record"))

    def test_anything_else_is_refused_not_misread(self):
        for bad in (b"", b"SGL1", b"XXXX08c047b8rec", b"SGL108c0"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                livetap.parse(bad)

    def test_a_plan_hash_must_be_eight_characters(self):
        # The sidecar slices the hash out by length, so a short one would shift the record by
        # a byte and every value would decode wrong rather than fail.
        with self.assertRaises(ValueError):
            livetap.frame("08c047", b"")


class TestTheTapNeverHurtsTheCaller(unittest.TestCase):
    """Tap.send() runs once per tick inside the capture loop. It must never raise or wait."""

    def setUp(self):
        self.dir = short_tmp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "live.sock")
        self.lines = []
        self.tap = livetap.Tap(self.path, "08c047b8", log=self.lines.append)
        self.addCleanup(self.tap.close)

    def test_no_listener_is_a_quiet_false(self):
        for _ in range(5):
            self.assertFalse(self.tap.send(b"rec"))
        self.assertEqual(self.tap.dropped, 5)
        # A state change is logged; a state is not. A sidecar down for a day must not write
        # 43,200 identical lines into sigen.log.
        self.assertEqual(len(self.lines), 1, self.lines)
        self.assertIn("not delivering", self.lines[0])
        self.assertIn("capture is unaffected", self.lines[0])

    def test_it_delivers_and_says_so_once(self):
        rx = livetap.bind(self.path)
        self.addCleanup(rx.close)
        for i in range(3):
            self.assertTrue(self.tap.send(b"rec%d" % i))
        got = recv_all(rx)
        self.assertEqual([livetap.parse(d) for d in got],
                         [("08c047b8", b"rec%d" % i) for i in range(3)])
        self.assertEqual([l for l in self.lines if "delivering to" in l and "not" not in l],
                         [f"  [live] tap delivering to {self.path}"])

    def test_it_recovers_when_the_receiver_comes_back(self):
        rx = livetap.bind(self.path)
        self.assertTrue(self.tap.send(b"a"))
        rx.close()
        os.unlink(self.path)                      # what systemd does to RuntimeDirectory
        self.assertFalse(self.tap.send(b"b"))
        rx = livetap.bind(self.path)
        self.addCleanup(rx.close)
        self.assertTrue(self.tap.send(b"c"))
        self.assertEqual([livetap.parse(d)[1] for d in recv_all(rx)], [b"c"])
        self.assertEqual(len(self.lines), 3, "delivering, not delivering, delivering")

    def test_a_full_queue_fails_fast_instead_of_blocking(self):
        # A wedged sidecar: bound, never reading. The kernel queue fills, and from then on
        # every send must fail IMMEDIATELY -- a blocking send here is a stalled capture tick.
        rx = livetap.bind(self.path)
        self.addCleanup(rx.close)
        payload = b"x" * 900                      # about a full-mask record
        slowest, refused = 0.0, 0
        for _ in range(20000):
            t0 = time.perf_counter()
            ok = self.tap.send(payload)
            slowest = max(slowest, time.perf_counter() - t0)
            if not ok:
                refused += 1
                if refused > 50:
                    break
        self.assertGreater(refused, 0, "the queue never filled, so this proved nothing")
        self.assertLess(slowest, 0.05, f"a send took {slowest * 1000:.1f} ms")
        self.assertTrue(any("not keeping up" in l for l in self.lines), self.lines)
        # And the property itself, because the timing above cannot see it on macOS: there a
        # full Unix datagram queue answers ENOBUFS at once even to a BLOCKING socket, whereas
        # Linux -- the Pi -- would block the sender until the reader drains. A timeout of
        # 0.0 is what "non-blocking" means to Python; settimeout(0.5) would pass the timing
        # here and stall capture there.
        self.assertEqual(self.tap.sock.gettimeout(), 0.0)

    def test_an_unforeseen_exception_is_swallowed(self):
        self.tap.sock.close()
        self.tap.sock = mock.Mock()
        self.tap.sock.sendto.side_effect = RuntimeError("something nobody predicted")
        self.assertFalse(self.tap.send(b"rec"))

    def test_an_impossible_path_makes_an_inert_tap_not_an_exception(self):
        lines = []
        tap = livetap.Tap("/tmp/" + "x" * 200, "08c047b8", log=lines.append)
        self.assertFalse(tap.send(b"rec"))
        self.assertIn("tap disabled", lines[0])
        tap = livetap.Tap(self.path, "short", log=lines.append)   # bad hash: same
        self.assertFalse(tap.send(b"rec"))


class TestBind(unittest.TestCase):
    def setUp(self):
        self.dir = short_tmp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "live.sock")

    def test_the_socket_is_private_to_its_user(self):
        s = livetap.bind(self.path)
        self.addCleanup(s.close)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_a_stale_socket_from_a_crash_is_replaced(self):
        s = livetap.bind(self.path)
        s.close()                                  # the file stays, nobody bound to it
        s = livetap.bind(self.path)
        self.addCleanup(s.close)

    def test_a_live_socket_is_not_stolen(self):
        # `live.py --stdout` started by hand next to the running unit would otherwise unlink
        # the unit's socket and silently take the stream away from it.
        s = livetap.bind(self.path)
        self.addCleanup(s.close)
        with self.assertRaises(FileExistsError) as cm:
            livetap.bind(self.path)
        self.assertIn("in use", str(cm.exception))

    def test_a_file_that_is_not_a_socket_is_never_deleted(self):
        with open(self.path, "w") as fh:
            fh.write("precious")
        with self.assertRaises(FileExistsError):
            livetap.bind(self.path)
        with open(self.path) as fh:
            self.assertEqual(fh.read(), "precious")


# ------------------------------------------------------------------ parse_record

class TestParseRecord(ViewerFixture):
    def test_it_agrees_with_the_file_reader_for_every_kind_of_record(self):
        sizes = decode.block_sizes(self.manifest)
        recs = [(1000.0, 0b000111, 90, self.block_payload([0, 1, 2])),
                (1002.0, 0b000101, 95, self.block_payload([0, 2])),     # one block lost
                (1003.0, 0b001000, 40, self.block_payload([3])),        # slow tier only
                (1004.0, 0, 6500, b""),                                  # outage probe
                (1005.0, self.full_mask(), 99, self.block_payload())]
        path = self.write_records("20260814T090000", recs)
        from_file = list(decode.records_from(self.manifest, path))
        with open(path, "rb") as fh:
            blob = fh.read()
        off, mine = 0, []
        for ts, mask, lat, payload in recs:
            n = decode.HEADER_LEN + len(payload)
            ts2, mask2, lat2, blocks = decode.parse_record(sizes, blob[off:off + n])
            self.assertEqual(mask2, mask)
            mine.append((ts2, lat2, blocks))
            off += n
        self.assertEqual(mine, from_file)

    def test_a_length_that_disagrees_with_the_mask_is_an_error(self):
        sizes = decode.block_sizes(self.manifest)
        rec = struct.pack(decode.HEADER, 1.0, 0b111, 1) + self.block_payload([0, 1])
        with self.assertRaises(ValueError):
            decode.parse_record(sizes, rec)
        with self.assertRaises(ValueError):
            decode.parse_record(sizes, b"short")

    def test_a_mask_naming_blocks_the_plan_lacks_is_an_error(self):
        sizes = decode.block_sizes(self.manifest)
        with self.assertRaises(ValueError):
            decode.parse_record(sizes, struct.pack(decode.HEADER, 1.0, 1 << 9, 1))


# --------------------------------------------------------------- capture + tap

class CaptureWithTap(ViewerFixture):
    """capture() for real, with live_enabled, against a receiver bound where the tap sends."""

    def setUp(self):
        super().setUp()
        self.sdir = short_tmp()
        self.addCleanup(shutil.rmtree, self.sdir, True)
        self.sock_path = os.path.join(self.sdir, "live.sock")
        self.real = log.Modbus
        self.addCleanup(lambda: setattr(log, "Modbus", self.real))

    def run_capture(self, modbus, seconds=3, **over):
        log.Modbus = modbus
        opts = dict(live_enabled=True, live_socket=self.sock_path,
                    log_dir=self.tmp, install_dir=self.tmp)
        opts.update(over)
        cfg = base_cfg(**opts)
        return self.capture_stdout(log.capture, cfg, self.tiers, self.tmp, seconds, 0,
                                   False, self.bundle)

    def archive_bytes(self):
        out = b""
        for name in sorted(os.listdir(self.tmp)):
            p = os.path.join(self.tmp, name)
            if name.endswith(".bin.gz"):
                with gzip.open(p, "rb") as fh:
                    out += fh.read()
            elif name.endswith(".bin"):
                with open(p, "rb") as fh:
                    out += fh.read()
        return out


class TestCaptureFeedsTheTap(CaptureWithTap):
    def test_a_healthy_device_streams_exactly_the_archived_bytes(self):
        rx = livetap.bind(self.sock_path)
        self.addCleanup(rx.close)
        out = self.run_capture(AnsweringModbus)
        got = recv_all(rx)
        self.assertGreaterEqual(len(got), 2, out)
        hashes = {livetap.parse(d)[0] for d in got}
        self.assertEqual(hashes, {self.manifest["plan_hash"]})
        # Byte for byte: the stream and the archive cannot disagree about what a tick held.
        self.assertEqual(b"".join(livetap.parse(d)[1] for d in got), self.archive_bytes())
        masks = {struct.unpack(log.HEADER, livetap.parse(d)[1][:log.HEADER_LEN])[1]
                 for d in got}
        self.assertEqual(masks, {0b111}, "fast ticks carry the three fast blocks")
        self.assertIn("live tap ON", out)
        self.assertIn("live tap: ", out)

    def test_an_outage_streams_the_empty_probes_too(self):
        rx = livetap.bind(self.sock_path)
        self.addCleanup(rx.close)
        self.run_capture(DeadModbus, degrade_after=1, degrade_probe_s=1)
        got = recv_all(rx)
        self.assertTrue(got)
        for d in got:
            rec = livetap.parse(d)[1]
            self.assertEqual(len(rec), log.HEADER_LEN, "a probe is a header and nothing else")
            self.assertEqual(struct.unpack(log.HEADER, rec)[1], 0)
        self.assertEqual(b"".join(livetap.parse(d)[1] for d in got), self.archive_bytes())

    def test_no_sidecar_means_one_line_and_a_complete_archive(self):
        out = self.run_capture(AnsweringModbus)
        self.assertEqual(out.count("[live] tap not delivering"), 1, out)
        self.assertGreaterEqual(len(self.archive_bytes()), 2 * FAST_RECORD,
                                "the archive must be whole")

    def test_a_tap_that_breaks_its_promise_still_cannot_stop_capture(self):
        # livetap.Tap promises never to raise. This is the case where that promise has been
        # broken by some later change -- and capture must not notice.
        real_close = livetap.Tap.close

        def broken_close(tap):
            real_close(tap)                 # release the socket, then break the promise
            raise RuntimeError("boom")
        with mock.patch.object(livetap.Tap, "send", side_effect=RuntimeError("boom")), \
                mock.patch.object(livetap.Tap, "close", broken_close):
            out = self.run_capture(AnsweringModbus)
        self.assertIn("capture finished", out)
        self.assertGreaterEqual(len(self.archive_bytes()), 2 * FAST_RECORD)
        with mock.patch.object(livetap, "Tap", side_effect=RuntimeError("boom")):
            out = self.run_capture(AnsweringModbus)
        self.assertIn("tap not started", out)
        self.assertIn("capture finished", out)

    def test_live_off_means_no_tap_at_all(self):
        rx = livetap.bind(self.sock_path)
        self.addCleanup(rx.close)
        out = self.run_capture(AnsweringModbus, live_enabled=False)
        self.assertEqual(recv_all(rx), [])
        self.assertNotIn("[live]", out)
        self.assertNotIn("live tap", out)

    def test_the_live_stream_adds_no_reads(self):
        # The design's first rule. Same ticks, same device: the reads with the tap on must be
        # exactly the reads with it off.
        reads = {}
        for enabled in (False, True):
            seen = []

            class Counting(AnsweringModbus):
                def read(self, unit, addr, count, fc, retries=2):
                    seen.append((unit, addr, count, fc))
                    return super().read(unit, addr, count, fc, retries)
            self.run_capture(Counting, live_enabled=enabled)
            reads[enabled] = seen
        self.assertEqual(reads[True], reads[False])


# ---------------------------------------------------------------------- decoder

class DecoderFixture(ViewerFixture):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.tmp, f"sigen-20260814-{self.manifest['plan_hash']}"
                                         f".manifest.json"), "w") as fh:
            json.dump(self.manifest, fh)
        self.cfg = base_cfg(data_dir=self.tmp)
        self.lines = []
        self.dec = live.Decoder(self.tmp, self.bundle, live.quiet_bound(self.cfg),
                                self.lines.append)
        self.now = time.time()

    def dgram(self, mask, indices, ts=None, lat=90, plan_hash=None):
        rec = struct.pack(log.HEADER, self.now if ts is None else ts, mask, lat)
        rec += self.block_payload(indices)
        return livetap.frame(plan_hash or self.manifest["plan_hash"], rec)


class TestDecoder(DecoderFixture):
    VALUES = {
        "plant_general_load_power": 0.75,
        "plant_sigen_photovoltaic_power": 3.2,
        "plant_grid_sensor_active_power": -1.234,     # exporting
        "plant_ess_power": 2.5,
        "plant_ess_soc": 55.5,
        "inverter_grid_frequency": 50.01,
        "inverter_phase_a_voltage": 238.45,
    }

    def test_a_fast_record_becomes_the_seven_metrics_exactly(self):
        for k, v in self.VALUES.items():
            self.set_scaled(k, v)
        msg = self.dec.message(self.dgram(0b111, [0, 1, 2], lat=123), self.now)
        expect = {"v": 1, "t": int(round(self.now * 1000)), "m": 0b111, "lat": 123,
                  "p": self.cfg["fast_period_s"]}
        expect.update(self.VALUES)
        self.assertEqual(msg, expect)
        # And no float artefacts on the wire: 1.234 must not travel as 1.2340000000000002.
        wire = live.encode(msg)
        for v in self.VALUES.values():
            self.assertIn(repr(v), wire)
        self.assertNotIn(" ", wire, "compact JSON")

    def test_a_lost_block_drops_only_its_own_metrics(self):
        # The degraded probe's recovery record reads plant_live alone (log.py: due[:1]).
        # It carries grid, PV, battery and SOC -- and NOT load, which is in plant_energy.
        msg = self.dec.message(self.dgram(0b001, [0]), self.now)
        self.assertEqual(set(msg) - {"v", "t", "m", "lat", "p"},
                         {"plant_grid_sensor_active_power", "plant_sigen_photovoltaic_power",
                          "plant_ess_power", "plant_ess_soc"})
        msg = self.dec.message(self.dgram(0b101, [0, 2]), self.now)
        self.assertNotIn("plant_general_load_power", msg)
        self.assertIn("inverter_grid_frequency", msg)

    def test_a_slow_tier_record_publishes_nothing(self):
        for mask, idx in ((8, [3]), (16, [4]), (32, [5])):
            self.assertIsNone(self.dec.message(self.dgram(mask, idx), self.now))
        self.assertEqual(self.dec.counts["slow_only"], 3)

    def test_an_outage_probe_becomes_a_marker_with_its_quiet_bound(self):
        msg = self.dec.message(self.dgram(0, [], lat=6500), self.now)
        self.assertEqual(msg, {"v": 1, "t": int(round(self.now * 1000)), "m": 0,
                               "lat": 6500, "q": live.quiet_bound(self.cfg)})

    def test_a_sentinel_is_omitted_not_published_as_a_number(self):
        self.set_raw("plant_ess_soc", 0xFFFF)
        msg = self.dec.message(self.dgram(0b111, [0, 1, 2]), self.now)
        self.assertNotIn("plant_ess_soc", msg)
        self.assertIn("plant_ess_power", msg)

    def test_the_past_is_never_published_as_now(self):
        self.assertIsNone(self.dec.message(
            self.dgram(0b111, [0, 1, 2], ts=self.now - live.STALE_S - 1), self.now))
        self.assertIsNone(self.dec.message(
            self.dgram(0, [], ts=self.now - 60), self.now))
        self.assertEqual(self.dec.counts["stale"], 2)

    def test_an_unknown_plan_is_refused_until_its_manifest_exists(self):
        # The reason the hash travels at all: decoding a retuned plan at the old offsets
        # gives plausible numbers that are all wrong.
        d = self.dgram(0b111, [0, 1, 2], plan_hash="deadbeef")
        self.assertIsNone(self.dec.message(d, self.now))
        self.assertEqual(self.dec.counts["unknown_plan"], 1)
        self.assertTrue(any("no manifest for plan deadbeef" in l for l in self.lines))
        with mock.patch.object(live, "MANIFEST_RETRY_S", 0):
            self.dec.retry_at["deadbeef"] = 0
            man = dict(self.manifest, plan_hash="deadbeef")
            with open(os.path.join(self.tmp, "sigen-20260815-deadbeef.manifest.json"),
                      "w") as fh:
                json.dump(man, fh)
            self.assertIsNotNone(self.dec.message(d, self.now))

    def test_garbage_is_counted_not_fatal(self):
        self.assertIsNone(self.dec.message(b"not a datagram", self.now))
        bad = livetap.frame(self.manifest["plan_hash"],
                            struct.pack(log.HEADER, self.now, 0b111, 1) + b"short")
        self.assertIsNone(self.dec.message(bad, self.now))
        self.assertEqual(self.dec.counts["undecodable"], 2)


class TestQuietBound(unittest.TestCase):
    def test_defaults(self):
        # degraded: 30 + 2 x 6 = 42; before degrading: 6 x 6 + 2 = 38.
        self.assertEqual(live.quiet_bound(base_cfg()), 42)

    def test_it_is_never_shorter_than_an_ordinary_probe_interval(self):
        # The failure it exists to prevent: "no data" painted inside every probe interval.
        for probe, timeout, period in itertools.product((10, 30, 60), (1.0, 6.0), (1, 2)):
            cfg = base_cfg(degrade_probe_s=probe, timeout_s=timeout, fast_period_s=period)
            q = live.quiet_bound(cfg)
            self.assertGreaterEqual(q, probe + 2 * timeout)
            self.assertGreaterEqual(q, 6 * timeout + period)


# -------------------------------------------------------------------- run loop

class TestRunLoop(DecoderFixture):
    def test_records_in_messages_out_and_tidy_on_exit(self):
        sdir = short_tmp()
        self.addCleanup(shutil.rmtree, sdir, True)
        cfg = base_cfg(data_dir=self.tmp, log_dir=self.tmp,
                       live_socket=os.path.join(sdir, "live.sock"))
        out = io.StringIO()
        sink = live.StdoutSink(out)
        stop = {"now": False}
        th = threading.Thread(target=live.run, args=(cfg, sink, None, stop, self.bundle))
        th.start()
        for _ in range(50):
            if os.path.exists(cfg["live_socket"]):
                break
            time.sleep(0.02)
        tap = livetap.Tap(cfg["live_socket"], self.manifest["plan_hash"])
        self.addCleanup(tap.close)
        self.now = time.time()
        tap.send(livetap.parse(self.dgram(0b111, [0, 1, 2]))[1])
        tap.send(livetap.parse(self.dgram(8, [3]))[1])
        tap.send(livetap.parse(self.dgram(0, []))[1])
        time.sleep(0.4)
        stop["now"] = True
        th.join(5)
        self.assertFalse(th.is_alive())
        msgs = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual([m["m"] for m in msgs], [0b111, 0])
        self.assertFalse(os.path.exists(cfg["live_socket"]), "the socket is removed at exit")
        with open(os.path.join(self.tmp, "live.state")) as fh:
            state = json.load(fh)
        self.assertEqual(state["received"], 3)
        self.assertEqual(state["markers"], 1)
        self.assertEqual(state["last"]["m"], 0)


class TestNothingIsPublishedWhileDisconnected(unittest.TestCase):
    """MqttSink without paho: the connection flag, the callbacks, and the drop rule."""

    def sink(self):
        s = live.MqttSink.__new__(live.MqttSink)
        s.say = lambda *_: None
        s.endpoint, s.port, s.topic, s.client_id = "e", 8883, "sigen/live", "sigen-pi"
        s.connected, s.published, s.skipped, s.connects, s.last = False, 0, 0, 0, None
        s.stopping = False
        s.client = mock.Mock()
        s.client.publish.return_value = mock.Mock(rc=0)
        return s

    def test_a_publish_while_disconnected_is_dropped_not_queued(self):
        s = self.sink()
        self.assertFalse(s.publish({"t": 1}))
        s.client.publish.assert_not_called()
        self.assertEqual(s.skipped, 1)

    def test_both_paho_callback_shapes_drive_the_flag(self):
        s = self.sink()
        s._on_connect(None, None, {}, 0)                       # paho 1.x
        self.assertTrue(s.connected)
        self.assertTrue(s.publish({"t": 1}))
        s.client.publish.assert_called_once_with("sigen/live", '{"t":1}', qos=0)
        s._on_disconnect(None, None, 1)                        # paho 1.x
        self.assertFalse(s.connected)
        rc = mock.Mock(value=0)
        s._on_connect(None, None, {}, rc, None)                # paho 2.x, a ReasonCode
        self.assertTrue(s.connected)
        s._on_disconnect(None, None, {}, mock.Mock(value=7), None)
        self.assertFalse(s.connected)
        s._on_connect(None, None, {}, mock.Mock(value=5), None)  # refused: not authorised
        self.assertFalse(s.connected)


# ------------------------------------------------------------------------ check

class TestCheck(unittest.TestCase):
    def setUp(self):
        self.dir = short_tmp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def cfg(self, **over):
        c = base_cfg(live_enabled=True, live_endpoint="abc-ats.iot.ap-southeast-2.amazonaws.com",
                     live_cert=os.path.join(self.dir, "c.crt"),
                     live_key=os.path.join(self.dir, "k.key"),
                     live_socket="/run/sigen-live/live.sock")
        c.update(over)
        return c

    def keypair(self, name="", mode=0o600):
        if not shutil.which("openssl"):
            self.skipTest("no openssl to make a throwaway certificate")
        crt = os.path.join(self.dir, f"c{name}.crt")
        key = os.path.join(self.dir, f"k{name}.key")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", crt, "-days", "1", "-subj", "/CN=test"],
                       check=True, capture_output=True)
        os.chmod(key, mode)
        return crt, key

    def problems(self, cfg):
        return [p for p in live.check(cfg) if "paho" not in p]   # paho: host-dependent

    def test_a_complete_setup_has_no_problems(self):
        crt, key = self.keypair()
        self.assertEqual(self.problems(self.cfg(live_cert=crt, live_key=key)), [])

    def test_each_missing_piece_is_named(self):
        ps = self.problems(self.cfg(live_endpoint=None))
        self.assertTrue(any("no live_endpoint" in p for p in ps))
        self.assertTrue(any(p.startswith("live_cert: no file") for p in ps))
        self.assertTrue(any(p.startswith("live_key: no file") for p in ps))
        ps = self.problems(self.cfg(live_endpoint="mqtts://x/y"))
        self.assertTrue(any("bare host name" in p for p in ps))

    def test_a_readable_key_is_a_problem(self):
        crt, key = self.keypair(mode=0o644)
        ps = self.problems(self.cfg(live_cert=crt, live_key=key))
        self.assertTrue(any("chmod 600" in p for p in ps), ps)

    def test_a_mismatched_pair_is_caught_before_the_broker_refuses_it(self):
        crt, _ = self.keypair("a")
        _, key = self.keypair("b")
        ps = self.problems(self.cfg(live_cert=crt, live_key=key))
        self.assertTrue(any("do not load as a pair" in p for p in ps), ps)

    def test_a_socket_under_run_must_be_in_the_units_own_directory(self):
        ps = live.check(self.cfg(live_socket="/run/elsewhere/live.sock"), need_mqtt=False)
        self.assertTrue(any("/run/sigen-live/" in p for p in ps), ps)
        self.assertEqual(live.check(self.cfg(live_socket="/home/pi/sigen/logs/live.sock"),
                                    need_mqtt=False), [])


# ----------------------------------------------------------------------- config

class TestLiveConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sigen-cfg-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.json")
        old = os.environ.get("SIGEN_CONFIG")
        os.environ["SIGEN_CONFIG"] = self.path
        self.addCleanup(lambda: os.environ.__setitem__("SIGEN_CONFIG", old) if old
                        else os.environ.pop("SIGEN_CONFIG", None))

    def write(self, d):
        with open(self.path, "w") as fh:
            json.dump(d, fh)

    def test_defaults_follow_the_unit_name_and_install_dir(self):
        self.write({"host": "h", "install_dir": "/opt/sigen", "live_unit": "my-live"})
        cfg = config.load()
        self.assertEqual(cfg["live_socket"], "/run/my-live/live.sock")
        self.assertEqual(cfg["live_cert"], "/opt/sigen/live-cert/certificate.pem.crt")
        self.assertEqual(cfg["live_key"], "/opt/sigen/live-cert/private.pem.key")
        self.assertFalse(cfg["live_enabled"])

    def test_nothing_in_the_live_block_can_stop_the_logger_starting(self):
        # config.load() is log.py's first call. Enabled with no endpoint, no certificate and
        # a socket path no kernel will accept must still load: live.py --check is the place
        # that refuses, and it refuses only live.py.
        self.write({"host": "h", "live_enabled": True, "live_endpoint": None,
                    "live_socket": "/" + "x" * 300, "live_cert": "/nope", "live_key": "/no"})
        cfg = config.load()
        self.assertTrue(cfg["live_enabled"])

    def test_the_live_unit_may_not_collide_with_the_logger(self):
        # The one live mistake that COULD stop capture: install-live.sh overwriting the
        # logger's unit file. So this one is checked by config.py.
        self.write({"host": "h", "live_unit": "sigen-logger"})
        with self.assertRaises(SystemExit) as cm:
            config.load()
        self.assertIn("must all differ", str(cm.exception))

    def test_sh_exports_what_the_live_scripts_read(self):
        self.write({"host": "h", "install_dir": "/opt/sigen", "live_enabled": True})
        buf = io.StringIO()
        with redirect_stdout(buf):
            config.emit_sh(config.load())
        out = buf.getvalue()
        for line in ("SIGEN_LIVE_UNIT=sigen-live", "SIGEN_LIVE_ENABLED=True",
                     "SIGEN_LIVE_SOCKET=/run/sigen-live/live.sock",
                     "SIGEN_LIVE_CERT=/opt/sigen/live-cert/certificate.pem.crt",
                     "SIGEN_LIVE_KEY=/opt/sigen/live-cert/private.pem.key"):
            self.assertIn(line, out)
        with open(os.path.join(HERE, "deploy", "systemd", "install-live.sh")) as fh:
            self.assertIn('"$SIGEN_LIVE_ENABLED" != "True"', fh.read(),
                          "the installer must compare against the literal emit_sh prints")


# -------------------------------------------------------------------- the fields

class TestLiveFields(unittest.TestCase):
    def setUp(self):
        cfg = base_cfg()
        self.tiers = log.build_tiers(cfg)
        self.manifest = log.build_manifest(FakeModbus(), dump.load_regmap(), cfg, self.tiers)
        self.bundle = dump.load_regmap()
        self.by_key = {f["key"]: f for g in self.bundle["groups"].values() for f in g}

    def test_every_metric_is_in_a_fast_block(self):
        # A metric in a slow block would appear once a minute, and the "no batching, every
        # tick" design would quietly not be true for it.
        located, missing = decode.resolve(self.manifest, self.bundle, livefields.KEYS)
        self.assertEqual(missing, [])
        fast = {i for i, t in enumerate(self.tiers) if t[5] == base_cfg()["fast_period_s"]}
        for key, (bidx, _, _) in located:
            self.assertIn(bidx, fast, key)

    def test_rounding_is_the_registers_own_resolution(self):
        for key in livefields.KEYS:
            gain = self.by_key[key]["gain"] or 1
            self.assertEqual(livefields.DECIMALS[key], round(math.log10(gain)), key)

    def test_presentation_comes_from_panels_not_a_second_copy(self):
        for item in livefields.series():
            hits = [(p, s) for p in panels.PANELS for s in p.get("series", [])
                    if s["key"] == item["key"]]
            self.assertTrue(hits, item["key"])
            p, s = hits[0]
            self.assertEqual((item["label"], item["slot"], item["unit"]),
                             (s["label"], s["slot"], p["unit"]))
        soc = next(i for i in livefields.series() if i["key"] == "plant_ess_soc")
        self.assertEqual(soc["domain"], [0, 100])
        self.assertEqual([i["key"] for i in livefields.series()], list(livefields.KEYS))

    def test_the_signed_powers_say_which_way_is_which(self):
        s = {i["key"]: i for i in livefields.series()}
        self.assertEqual(s["plant_grid_sensor_active_power"]["sign"],
                         ["importing", "exporting"])
        self.assertEqual(s["plant_ess_power"]["sign"], ["charging", "discharging"])


# ------------------------------------------------------------- the unit and deploy

class TestTheLiveUnit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sigen-cfg-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        path = os.path.join(self.dir, "config.json")
        with open(path, "w") as fh:
            json.dump({"host": "h", "install_dir": "/opt/sigen", "run_as_user": "someone"},
                      fh)
        old = os.environ.get("SIGEN_CONFIG")
        os.environ["SIGEN_CONFIG"] = path
        self.addCleanup(lambda: os.environ.__setitem__("SIGEN_CONFIG", old) if old
                        else os.environ.pop("SIGEN_CONFIG", None))
        d = os.path.join(HERE, "deploy", "systemd")
        self.unit = config.render(config.load(), os.path.join(d, "sigen-live.service.template"))
        self.logger = config.render(config.load(),
                                    os.path.join(d, "sigen-logger.service.template"))

    def directives(self, text):
        return [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]

    def test_it_renders_completely(self):
        self.assertNotIn("@", self.unit)
        for line in ("User=someone", "ExecStart=/usr/bin/python3 -u /opt/sigen/live.py",
                     "RuntimeDirectory=sigen-live", "ConditionPathExists=/opt/sigen/config.json",
                     "Restart=on-failure"):
            self.assertIn(line, self.directives(self.unit))

    def test_it_is_coupled_to_the_logger_in_no_direction(self):
        # Requires= / BindsTo= in either file would let one unit's failure stop the other.
        coupling = ("Requires=", "Requisite=", "BindsTo=", "PartOf=", "Before=", "After=",
                    "Wants=", "Upholds=", "PropagatesStopTo=", "StopPropagatedFrom=")
        for line in self.directives(self.unit):
            if line.startswith(coupling):
                self.assertNotIn("sigen-logger", line)
        for line in self.directives(self.logger):
            if line.startswith(coupling):
                self.assertNotIn("sigen-live", line)

    def test_it_loses_every_contention_with_the_logger(self):
        d = self.directives(self.unit)
        self.assertIn("Nice=10", d)
        self.assertIn("IOSchedulingClass=idle", d)
        self.assertTrue(any(l.startswith("CPUQuota=") for l in d))
        self.assertTrue(any(l.startswith("MemoryMax=") for l in d))
        self.assertIn("Nice=-5", self.directives(self.logger), "and the logger still wins")

    def test_the_default_socket_is_in_the_directory_the_unit_creates(self):
        cfg = config.load()
        self.assertEqual(os.path.dirname(cfg["live_socket"]), "/run/" + cfg["live_unit"])
        self.assertIn("RuntimeDirectory=" + cfg["live_unit"], self.directives(self.unit))


class TestEveryPlaceThatListsUnitsKnowsTheLiveOne(unittest.TestCase):
    """A fourth unit touches every file that enumerates the other three."""

    def read(self, *parts):
        with open(os.path.join(HERE, *parts)) as fh:
            return fh.read()

    def test_bootstrap_installs_it_last_and_survives_its_failure(self):
        sh = self.read("deploy", "pi", "bootstrap.sh")
        self.assertIn("python3-paho-mqtt", sh)
        i_live = sh.index("install-live.sh")
        self.assertGreater(i_live, sh.index("install-watchdog.sh"))
        self.assertGreater(i_live, sh.index("install-logger.sh"))
        self.assertRegex(sh, r'install-live\.sh" \|\| \{', "a live failure must not abort")

    def test_provision_pushes_the_cert_and_restarts_the_unit(self):
        sh = self.read("deploy", "pi", "provision.sh")
        self.assertIn("--exclude 'live-cert/'", sh, "the key must not travel with the code")
        self.assertIn("install -m 600 /tmp/sigen-live.key", sh)
        self.assertIn("restart ${SIGEN_LIVE_UNIT}.service", sh)
        self.assertLess(sh.index("restart ${SIGEN_LIVE_UNIT}.service"),
                        sh.index("restart ${SIGEN_LOGGER_UNIT}.service"),
                        "the socket should exist before the logger starts offering to it")

    def test_sanitize_stops_it_and_removes_the_key(self):
        sh = self.read("deploy", "pi", "sanitize.sh")
        self.assertIn('stop "${SIGEN_LIVE_UNIT}.service"', sh)
        self.assertIn('rm -rf "$DIR/live-cert"', sh)

    def test_its_logs_rotate(self):
        lr = self.read("deploy", "pi", "logrotate.sigen")
        self.assertIn("@LOG_DIR@/live.log", lr)
        self.assertIn("@LOG_DIR@/live.err", lr)

    def test_status_reports_it(self):
        sh = self.read("bin", "status.sh")
        self.assertIn('unit_state "${SIGEN_LIVE_UNIT}.service"', sh)
        self.assertIn("live.state", sh)

    def test_the_key_is_gitignored(self):
        self.assertIn("\nlive-cert/\n", self.read(".gitignore"))

    def test_the_installers_are_executable(self):
        for name in ("install-live.sh", "uninstall-live.sh"):
            path = os.path.join(HERE, "deploy", "systemd", name)
            self.assertTrue(os.access(path, os.X_OK), f"{name} is not executable")

    def test_leak_check_would_catch_the_key_iot_hands_out(self):
        # The key IoT issues with a device certificate is PKCS#1, whose PEM header has a word
        # (RSA) between BEGIN and PRIVATE; the old pattern allowed nothing there. The headers
        # are assembled from parts so that this file does not itself contain what the leak
        # check scans for -- it did at first, and leak-check.sh flagged it.
        sh = self.read("bin", "leak-check.sh")
        pats = re.search(r"^PATTERNS='([^']*)'", sh, re.M).group(1).split()
        for kind in ("RSA ", "", "EC "):
            header = "-----" + "BEGIN " + kind + "PRIVATE" + " KEY-----"
            self.assertTrue(any(re.search(p, header) for p in pats), header)


# ------------------------------------------------------------------- the cloud stack

class TestTheLiveStack(unittest.TestCase):
    """cloud/infrastructure/lib/live-stack.ts, read as source: there is no CDK test harness."""

    STACK = os.path.join(HERE, "cloud", "infrastructure", "lib", "live-stack.ts")
    CONFIG_TS = os.path.join(HERE, "cloud", "infrastructure", "lib", "config.ts")

    def setUp(self):
        with open(self.STACK) as fh:
            self.src = fh.read()

    def const(self, name):
        m = re.search(rf"export const {name} = '([^']*)';", self.src)
        self.assertIsNotNone(m, name)
        return m.group(1)

    def test_the_pi_and_the_stack_agree_on_the_topic_and_client_id(self):
        # Two copies by necessity -- one in Python on the Pi, one in TypeScript in the cloud --
        # and a mismatch fails as "not authorized" at connect time, with nothing saying why.
        self.assertEqual(self.const("LIVE_TOPIC"), config.DEFAULTS["live_topic"])
        self.assertEqual(self.const("PI_CLIENT_ID"), config.DEFAULTS["live_client_id"])

    def test_the_pi_may_connect_and_publish_and_nothing_else(self):
        policy = self.src[self.src.index("'PiPolicy'"):self.src.index("// Attached by id")]
        self.assertEqual(sorted(set(re.findall(r"Action: '([^']+)'", policy))),
                         ["iot:Connect", "iot:Publish"])
        self.assertNotIn("*", re.sub(r"/\*.*?\*/|//[^\n]*", "", policy),
                         "no wildcard in the Pi's policy")
        self.assertNotIn("dynamodb", policy.lower())

    def test_the_rule_adds_the_keys_the_message_does_not_carry(self):
        sql = re.search(r"sql: `([^`]*)`\s*\+\s*`([^`]*)`", self.src)
        self.assertIsNotNone(sql)
        text = sql.group(1) + sql.group(2)
        self.assertIn("'live' AS pk", text)
        self.assertIn("floor(timestamp() / 1000) + ${LIVE_WINDOW_S} AS ttl", text)
        self.assertIn("FROM '${LIVE_TOPIC}'", text)
        self.assertIn("timeToLiveAttribute: 'ttl'", self.src)
        self.assertIn("name: 'pk', type: cdk.aws_dynamodb.AttributeType.STRING", self.src)
        self.assertIn("name: 't', type: cdk.aws_dynamodb.AttributeType.NUMBER", self.src)

    def test_the_rule_can_write_only_the_live_partition(self):
        self.assertIn("'dynamodb:LeadingKeys': ['live']", self.src)
        self.assertIn("errorAction", self.src, "a rejected write must leave a trace")

    def test_the_tables_are_provisioned_inside_the_free_allowance(self):
        # On-demand has no free tier. 25 RCU and 25 WCU are always free, across all tables.
        rcu = sum(int(x) for x in re.findall(r"readCapacity: (\d+)", self.src))
        wcu = sum(int(x) for x in re.findall(r"writeCapacity: (\d+)", self.src))
        self.assertNotIn("PAY_PER_REQUEST", self.src)
        self.assertLessEqual(rcu, 25)
        self.assertLessEqual(wcu, 25)

    def test_no_reserved_concurrency(self):
        # The account's Lambda quota was 10, and reserving any of it is rejected outright
        # (data-stack.ts). Asserted so the first function this stack grows cannot try.
        self.assertNotIn("reservedConcurrentExecutions", self.src)

    def test_cloud_example_names_exactly_the_keys_config_ts_accepts(self):
        with open(os.path.join(HERE, "cloud.example.json")) as fh:
            example = {k for k in json.load(fh) if not k.startswith("_")}
        with open(self.CONFIG_TS) as fh:
            ts = fh.read()
        block = ts[ts.index("const known = new Set(["):]
        block = block[:block.index("]);")]
        known = set(re.findall(r"'([a-z_]+)'", block))
        self.assertEqual(example, known)

    def test_leak_check_knows_the_live_identifiers(self):
        with open(os.path.join(HERE, "bin", "leak-check.sh")) as fh:
            sh = fh.read()
        for key in ("live_iot_endpoint", "live_cert_id", "live_api_domain"):
            self.assertIn(f'"{key}"', sh)


# ------------------------------------------------------------------- the live API

def load_live_handler(**env):
    """cloud/lambda/live/handler.py as a module, with its environment set first."""
    old = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        spec = importlib.util.spec_from_file_location(
            "live_handler", os.path.join(HERE, "cloud", "lambda", "live", "handler.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeStore:
    """The live table's two partitions, in memory, with the real conditions."""

    def __init__(self, live=()):
        self.live = list(live)
        self.slots = {}

    def query_live(self, lo, hi):
        return [m for m in self.live if lo <= m["t"] <= hi]

    def claim(self, slot, lease, exp, auth, now):
        cur = self.slots.get(slot)
        if cur and cur["exp"] >= now:
            return False
        self.slots[slot] = {"lease": lease, "exp": exp, "auth": auth}
        return True

    def renew(self, slot, lease, exp, now):
        cur = self.slots.get(slot)
        if not cur or cur["lease"] != lease or cur["exp"] < now:
            return False
        cur["exp"] = exp
        return True

    def release(self, slot, lease):
        cur = self.slots.get(slot)
        if cur and cur["lease"] == lease:
            del self.slots[slot]
            return True
        return False

    def soonest_expiry(self):
        return min((v["exp"] for v in self.slots.values()), default=None)


class LiveApiFixture(unittest.TestCase):
    def setUp(self):
        self.h = load_live_handler(SLOTS=10, PUBLIC_SLOTS=8, LEASE_S=150,
                                   CLIENT_PREFIX="sigen-view-", TOPIC="sigen/live")
        self.store = FakeStore()
        self.assumed = []

    def assume(self, slot):
        self.assumed.append(slot)
        return "AKIDEXAMPLE", "secret", "token"

    def session(self, body, authed=False, now=1_000_000):
        return self.h.session(self.store, body, authed, now, assume=self.assume,
                              sign=lambda ak, sk, tok: f"wss://example/mqtt?{ak}")


class TestAdmission(LiveApiFixture):
    def test_an_anonymous_viewer_gets_a_public_slot_and_a_url_for_it(self):
        r = self.session({"op": "connect"})
        self.assertEqual(r["slot"], 0)
        self.assertEqual(r["clientId"], "sigen-view-0")
        self.assertTrue(r["url"].startswith("wss://"))
        self.assertEqual(self.assumed, [0], "credentials are minted for the slot granted")

    def test_ten_is_the_cap_and_two_of_them_are_kept_for_the_household(self):
        got = [self.session({"op": "connect"})["slot"] for _ in range(8)]
        self.assertEqual(got, list(range(8)))
        with self.assertRaises(self.h.Refused) as cm:
            self.session({"op": "connect"})
        self.assertEqual(cm.exception.status, 429)
        self.assertTrue(cm.exception.extra["full"])
        self.assertGreater(cm.exception.extra["retry_s"], 0)
        # A signed-in caller still gets in -- to a reserved slot, first.
        self.assertEqual(self.session({"op": "connect"}, authed=True)["slot"], 8)
        self.assertEqual(self.session({"op": "connect"}, authed=True)["slot"], 9)
        with self.assertRaises(self.h.Refused):
            self.session({"op": "connect"}, authed=True)

    def test_a_signed_in_caller_prefers_a_reserved_slot(self):
        self.assertEqual(self.session({"op": "connect"}, authed=True)["slot"], 8)

    def test_a_reload_keeps_its_slot(self):
        first = self.session({"op": "connect"})
        again = self.session({"op": "connect", "slot": first["slot"], "lease": first["lease"]})
        self.assertEqual((again["slot"], again["lease"]), (first["slot"], first["lease"]))
        self.assertEqual(len(self.store.slots), 1)

    def test_an_anonymous_caller_cannot_hold_a_reserved_slot_by_naming_it(self):
        mine = self.session({"op": "connect"}, authed=True)       # slot 8
        r = self.session({"op": "connect", "slot": mine["slot"], "lease": mine["lease"]})
        self.assertLess(r["slot"], 8)

    def test_a_lapsed_lease_frees_the_slot(self):
        for _ in range(8):
            self.session({"op": "connect"}, now=1000)
        r = self.session({"op": "connect"}, now=1000 + 151)
        self.assertEqual(r["slot"], 0)

    def test_renew_and_release(self):
        a = self.session({"op": "connect"})
        self.assertTrue(self.session({"op": "renew", "slot": a["slot"], "lease": a["lease"]})["ok"])
        with self.assertRaises(self.h.Refused) as cm:
            self.session({"op": "renew", "slot": a["slot"], "lease": "not-mine"})
        self.assertEqual(cm.exception.status, 409)
        self.assertTrue(cm.exception.extra["lost"])
        self.assertTrue(self.session({"op": "release", "slot": a["slot"],
                                      "lease": a["lease"]})["released"])
        self.assertEqual(self.store.slots, {})

    def test_bad_requests_are_400_not_500(self):
        for body in ({"op": "steal"}, {"op": "connect", "slot": 10},
                     {"op": "connect", "slot": True}, {"op": "renew"},
                     {"op": "connect", "slot": 1, "lease": "x" * 100}):
            with self.assertRaises(self.h.Refused, msg=body) as cm:
                self.session(body)
            self.assertEqual(cm.exception.status, 400)


class TestSeed(LiveApiFixture):
    def test_only_the_window_and_never_the_storage_fields(self):
        now = 2_000_000_000_000
        from decimal import Decimal
        self.store.live = [
            {"pk": "live", "t": Decimal(now - 400_000), "ttl": Decimal(1), "m": Decimal(7)},
            {"pk": "live", "t": Decimal(now - 1000), "ttl": Decimal(1), "m": Decimal(7),
             "plant_ess_soc": Decimal("55.5")},
            {"pk": "live", "t": Decimal(now - 3000), "ttl": Decimal(1), "m": Decimal(0),
             "q": Decimal(42)},
        ]
        # The fake's window check runs on the Decimals the real table returns.
        out = self.h.seed(self.store, now)
        self.assertEqual([m["t"] for m in out["msgs"]], [now - 3000, now - 1000])
        self.assertNotIn("pk", out["msgs"][0])
        self.assertNotIn("ttl", out["msgs"][0])
        self.assertIsInstance(out["msgs"][1]["plant_ess_soc"], float)
        self.assertIsInstance(out["msgs"][1]["t"], int)
        self.assertEqual([s["key"] for s in out["series"]], list(livefields.KEYS))
        self.assertTrue(out["tz"] and len(out["tz"][0]) == 3)
        json.dumps(out)                                  # it must serialise

    def test_the_handler_routes_and_caches_the_seed_briefly(self):
        ev = {"rawPath": "/api/live/seed", "requestContext": {"http": {"method": "GET"}}}
        r = self.h.lambda_handler(ev, None, store=self.store)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(r["headers"]["Cache-Control"], "public, max-age=2")
        ev = {"rawPath": "/api/live/session", "body": '{"op":"nope"}',
              "requestContext": {"http": {"method": "POST"}, "timeEpoch": 5}}
        r = self.h.lambda_handler(ev, None, store=self.store)
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(r["headers"]["Cache-Control"], "no-store")
        self.assertFalse(json.loads(r["body"])["ok"])
        ev = {"rawPath": "/api/live/other", "requestContext": {"http": {"method": "GET"}}}
        self.assertEqual(self.h.lambda_handler(ev, None, store=self.store)["statusCode"], 404)

    def test_the_session_answer_carries_both_server_timestamps(self):
        self.h.assume_viewer = self.assume
        self.h.presign = lambda *a, **k: "wss://example/mqtt"
        ev = {"rawPath": "/api/live/session", "body": '{"op":"connect"}',
              "requestContext": {"http": {"method": "POST"}, "timeEpoch": 1234}}
        body = json.loads(self.h.lambda_handler(ev, None, store=self.store)["body"])
        self.assertEqual(body["recv"], 1234)
        self.assertGreaterEqual(body["send"], body["recv"])

    def test_only_the_authorizer_makes_a_caller_signed_in(self):
        self.assertFalse(self.h.authed_of({}))
        self.assertFalse(self.h.authed_of({"requestContext": {"authorizer": {}}}))
        self.assertTrue(self.h.authed_of(
            {"requestContext": {"authorizer": {"lambda": {"email_hash": "ab12"}}}}))


class TestPresign(LiveApiFixture):
    NOW = datetime.datetime(2026, 11, 21, 8, 30, 0, tzinfo=datetime.timezone.utc)

    def test_the_token_is_appended_after_signing(self):
        # Measured against the real endpoint: signed into the canonical query, the handshake is
        # refused. So the URL without the token must carry the same signature as with it.
        a = self.h.presign("h.example", "ap-southeast-2", "AKID", "SECRET", None, now=self.NOW)
        b = self.h.presign("h.example", "ap-southeast-2", "AKID", "SECRET", "TOK/+=", now=self.NOW)
        self.assertTrue(b.startswith(a + "&X-Amz-Security-Token="))
        self.assertTrue(b.endswith("TOK%2F%2B%3D"))
        self.assertIn("%2Fap-southeast-2%2Fiotdevicegateway%2Faws4_request", a)

    def test_the_signature_matches_botocore(self):
        # An independent implementation of the same SigV4 chain, over the same canonical request.
        try:
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest
            from botocore.credentials import Credentials
        except ImportError:
            self.skipTest("botocore not installed")
        url = self.h.presign("h.example", "ap-southeast-2", "AKID", "SECRET", None, now=self.NOW)
        query = url.split("?", 1)[1]
        mine = query.rsplit("&X-Amz-Signature=", 1)[1]
        canonical_query = query.rsplit("&X-Amz-Signature=", 1)[0]
        canonical = "\n".join(["GET", "/mqtt", canonical_query, "host:h.example\n", "host",
                               hashlib.sha256(b"").hexdigest()])
        auth = SigV4Auth(Credentials("AKID", "SECRET"), "iotdevicegateway", "ap-southeast-2")
        req = AWSRequest(method="GET", url="https://h.example/mqtt")
        req.context["timestamp"] = self.NOW.strftime("%Y%m%dT%H%M%SZ")
        sts = auth.string_to_sign(req, canonical)
        self.assertEqual(mine, auth.signature(sts, req))

    def test_a_known_answer(self):
        # Pinned, so a refactor of the presigner that changes its output fails here and not
        # as an unexplained handshake refusal in a browser.
        url = self.h.presign("h.example", "ap-southeast-2", "AKID", "SECRET", None, now=self.NOW)
        self.assertEqual(hashlib.sha256(url.encode()).hexdigest()[:16], KNOWN_PRESIGN)


class TestTheLivePackage(unittest.TestCase):
    """cloud/lambda/live/PACKAGE.txt is the live handler's import closure, exactly."""

    def test_the_list_is_the_closure(self):
        import ast
        local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}

        def imports(path):
            with open(path) as fh:
                tree = ast.parse(fh.read())
            out = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    out |= {a.name.split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                    out.add(n.module.split(".")[0])
            return out & local
        seen, stack = set(), [os.path.join(HERE, "cloud", "lambda", "live", "handler.py")]
        while stack:
            for dep in imports(stack.pop()):
                if dep not in seen:
                    seen.add(dep)
                    stack.append(os.path.join(HERE, dep + ".py"))
        with open(os.path.join(HERE, "cloud", "lambda", "live", "PACKAGE.txt")) as fh:
            listed = {l.strip() for l in fh if l.strip() and not l.startswith("#")}
        self.assertEqual(listed, {m + ".py" for m in seen})


class TestTheViewerCap(unittest.TestCase):
    """The cost cap, read out of live-stack.ts and the handler."""

    def setUp(self):
        with open(os.path.join(HERE, "cloud", "infrastructure", "lib", "live-stack.ts")) as fh:
            self.ts = fh.read()
        with open(os.path.join(HERE, "cloud", "lambda", "live", "handler.py")) as fh:
            self.py = fh.read()

    def test_ten_enumerated_client_ids_and_no_wildcard(self):
        self.assertIn("export const VIEWER_SLOTS = 10;", self.ts)
        self.assertIn("Array.from({ length: VIEWER_SLOTS }", self.ts)
        role = self.ts[self.ts.index("'ViewerRole'"):self.ts.index("// ---- the function")]
        self.assertNotIn("*", re.sub(r"/\*.*?\*/|//[^\n]*", "", role))
        self.assertEqual(sorted(set(re.findall(r"actions: \['([^']+)'\]", role))),
                         ["iot:Connect", "iot:Receive", "iot:Subscribe"])

    def test_the_session_policy_names_exactly_one_client(self):
        # Behaviour, not source text: capture what assume_viewer() actually asks STS for.
        h = load_live_handler(VIEWER_ROLE_ARN="arn:aws:iam::123456789012:role/viewer",
                              AWS_REGION="ap-southeast-2", TOPIC="sigen/live",
                              CLIENT_PREFIX="sigen-view-")
        calls = []

        class FakeSTS:
            def assume_role(self, **kw):
                calls.append(kw)
                return {"Credentials": {"AccessKeyId": "A", "SecretAccessKey": "S",
                                        "SessionToken": "T"}}
        fake = type(sys)("boto3")
        fake.client = lambda name: FakeSTS()
        with mock.patch.dict(sys.modules, {"boto3": fake}):
            self.assertEqual(h.assume_viewer(3), ("A", "S", "T"))
        policy = json.loads(calls[0]["Policy"])
        connect = [st for st in policy["Statement"] if st["Action"] == "iot:Connect"]
        self.assertEqual([st["Resource"] for st in connect],
                         ["arn:aws:iot:ap-southeast-2:123456789012:client/sigen-view-3"])
        self.assertNotIn("*", calls[0]["Policy"])
        self.assertEqual({st["Action"] for st in policy["Statement"]},
                         {"iot:Connect", "iot:Subscribe", "iot:Receive"})
        self.assertEqual(calls[0]["DurationSeconds"], 900)

    def test_the_public_routes_are_throttled(self):
        for route in ("'GET /api/live/seed'", "'POST /api/live/session'"):
            self.assertRegex(self.ts, re.escape(route) + r"[^\]]*, \d+, \d+\]")
        self.assertIn("defaultRouteSettings: { throttlingRateLimit: 1", self.ts)
        # The stage names routes by key, which CloudFormation cannot see as a dependency; the
        # first deploy created the stage first and was refused. The order is now explicit.
        self.assertIn("for (const r of routeResources) stage.node.addDependency(r);", self.ts)

    def test_the_function_writes_only_slot_leases(self):
        self.assertIn("'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem'", self.ts)
        self.assertIn("'dynamodb:LeadingKeys': ['slot']", self.ts)


class TestTheLivePage(unittest.TestCase):
    """What the site stack publishes for the live view, and what the page loads."""

    SITE = os.path.join(HERE, "cloud", "infrastructure", "lib", "site-stack.ts")

    def setUp(self):
        with open(self.SITE) as fh:
            self.ts = fh.read()

    def listed(self, name):
        m = re.search(rf"const {name} = \[(.*?)\] as const;", self.ts, re.S)
        self.assertIsNotNone(m, name)
        return re.findall(r"'([^']+)'", m.group(1))

    def test_every_file_the_page_loads_is_published(self):
        with open(os.path.join(HERE, "web", "live.html")) as fh:
            refs = re.findall(r'(?:src|href)="(/[^"]*)"', fh.read())
        published = {"/" + f for f in self.listed("WEB_ASSETS") + self.listed("LIVE_ASSETS")}
        self.assertTrue(refs)
        self.assertEqual({r for r in refs if r != "/"} - published, set())

    def test_the_live_files_stay_out_of_the_share_bundle(self):
        self.assertRegex(self.ts, r"VIEWER_SOURCES = \['index\.html', \.\.\.WEB_ASSETS\] as const;")
        self.assertFalse(set(self.listed("LIVE_ASSETS")) & set(self.listed("WEB_ASSETS")))
        self.assertIn("live", self.listed("HTML_ENTRY_POINTS"))

    def test_dev_files_are_never_published(self):
        for name in ("WEB_ASSETS", "LIVE_ASSETS"):
            self.assertFalse([f for f in self.listed(name) if f.startswith("dev/")])
        with open(os.path.join(HERE, "web", "live.html")) as fh:
            self.assertNotIn("/dev/", fh.read(), "the published page must not load the mock")

    def test_one_expression_gates_the_page_and_its_api(self):
        self.assertEqual(self.ts.count("const livePublic = cfg.livePublic;"), 1)
        self.assertIn("...(livePublic ? {} : { edgeLambdas: gate }),", self.ts)
        self.assertIn("...(livePublic ? {} : { edgeLambdas: gateSigningBody }),", self.ts)

    def test_the_archive_viewer_does_not_link_to_it(self):
        # Two tests treat every root-absolute link in index.html as a bundle asset.
        with open(os.path.join(HERE, "web", "index.html")) as fh:
            self.assertNotIn('href="/live"', fh.read())

    def test_the_vendored_client_is_the_one_the_notices_describe(self):
        path = os.path.join(HERE, "web", "vendor", "mqtt.min.js")
        with open(path, "rb") as fh:
            blob = fh.read()
        self.assertEqual(len(blob), 340797)
        self.assertEqual(hashlib.sha256(blob).hexdigest(),
                         "75873d04a6400bbbac093b3602caf06f44ed92ee032925e1f4f21c03eba05a4e")
        with open(os.path.join(HERE, "THIRD_PARTY_NOTICES.md")) as fh:
            notices = fh.read()
        self.assertIn("mqtt@5.16.1", notices)
        self.assertIn("75873d04a6400bbbac093b3602caf06f44ed92ee032925e1f4f21c03eba05a4e", notices)

    @staticmethod
    def code_of(path):
        """JS with comments removed, so an assertion cannot be satisfied by prose about it."""
        with open(path) as fh:
            js = fh.read()
        return re.sub(r"/\*.*?\*/|//[^\n]*", "", js, flags=re.S)

    def test_the_page_hands_mqtt_js_the_url_verbatim_and_reconnects_itself(self):
        js = self.code_of(os.path.join(HERE, "web", "livechart.js"))
        self.assertIn("transformWsUrl: () => url", js)
        self.assertIn("reconnectPeriod: 0", js)
        self.assertIn("granted[0].qos === 128", js, "a refused SUBACK must not read as quiet")

    def test_the_seed_never_waits_for_the_stream(self):
        # A browser that could not open the WebSocket used to draw nothing at all, behind a pill
        # saying "reconnecting" -- the seed was only fetched after IoT acknowledged a
        # subscription that never came. Both pages now seed before starting the stream, and
        # poll the seed whenever the stream is not live.
        for name in ("live.js", "devices.js"):
            js = self.code_of(os.path.join(HERE, "web", name))
            # Immediately before BOTH starts: on load, and on the tab becoming visible again.
            self.assertEqual(len(re.findall(r"seed\(null\);\s*stream\.start\(\);", js)), 2,
                             f"{name} must seed before every stream.start()")
            self.assertIn("polling(s !== 'live' && s !== 'paused');", js, name)
        lc = self.code_of(os.path.join(HERE, "web", "livechart.js"))
        self.assertIn("if (!subscribed) this.failures++;", lc)
        self.assertIn("everLive: this.everLive", lc)

    def test_grid_is_the_default_and_column_is_one_toggle_away(self):
        with open(os.path.join(HERE, "web", "live.html")) as fh:
            html = fh.read()
        self.assertIn('data-layout="grid" aria-pressed="true"', html)
        self.assertIn('data-layout="column" aria-pressed="false"', html)
        js = self.code_of(os.path.join(HERE, "web", "live.js"))
        self.assertIn("|| localStorage.getItem(LAYOUT_KEY) || 'grid') === 'column' ? 'column' : 'grid';", js)
        # Column: the smaller of the room on screen and the chart's own width.
        self.assertIn("Math.min(window.innerHeight - chrome, width)", js)
        with open(os.path.join(HERE, "web", "live.css")) as fh:
            css = fh.read()
        self.assertIn('.live-page[data-layout="column"] main { max-width: none; }', css)

    def test_charts_are_sized_after_every_card_exists_and_follow_their_card(self):
        # Built one by one, the first chart took its width from a grid holding one card --
        # the whole row -- and was then squeezed, text and all, to a third of it.
        js = self.code_of(os.path.join(HERE, "web", "live.js"))
        b = js[js.index("function build(series)"):js.index("function relayout()")]
        self.assertLess(b.index("root.appendChild(card);"), b.index("new Live.LiveChart("))
        self.assertIn("new ResizeObserver(relayout)", js)
        self.assertIn("observer.observe(c.host)", js)
        lc = self.code_of(os.path.join(HERE, "web", "livechart.js"))
        self.assertIn("stale() { return Math.abs(Math.max(320, this.host.clientWidth || 640) - this.width) > 2; }", lc)

    def test_one_gap_rule_for_both_renderers(self):
        with open(os.path.join(HERE, "web", "charts.js")) as fh:
            charts = fh.read()
        self.assertIn("Math.ceil(GAP_CADENCES * (cadenceS || bucketS) / bucketS)", charts)
        self.assertIn("return (GAP_CADENCES + 0.5) * (cadenceS || 2);", charts)
        with open(os.path.join(HERE, "web", "livechart.js")) as fh:
            js = fh.read()
        self.assertGreaterEqual(js.count("C.gapLimitS("), 2, "line breaks and hatches")


# ---------------------------------------------------------------- the device tracker

def load_tracker():
    spec = importlib.util.spec_from_file_location(
        "tracker_handler", os.path.join(HERE, "cloud", "lambda", "tracker", "handler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeTrackerStore:
    """The tracker table in memory: (pk, sk) -> item, with the real conditions."""

    def __init__(self):
        self.items = {}

    def config(self):
        return [dict(v) for (pk, _), v in sorted(self.items.items()) if pk == "CFG"]

    def get(self, pk, sk):
        v = self.items.get((pk, sk))
        return dict(v) if v else None

    def put_new(self, item):
        key = (item["pk"], item["sk"])
        if key in self.items:
            return False
        self.items[key] = dict(item)
        return True

    def update(self, pk, sk, fields, remove=()):
        cur = self.items.get((pk, sk))
        if cur is None:
            return None
        cur.update(fields)
        for k in remove:
            cur.pop(k, None)
        return dict(cur)

    def events(self, device, since=None, limit=None, newest_first=True):
        out = sorted((v for (pk, sk), v in self.items.items() if pk == "EV#" + device
                      and (since is None or sk >= f"{int(since):013d}")),
                     key=lambda v: v["sk"], reverse=newest_first)
        return [dict(v) for v in out[:limit] if True] if limit else [dict(v) for v in out]


class TrackerFixture(unittest.TestCase):
    def setUp(self):
        self.h = load_tracker()
        self.store = FakeTrackerStore()

    def call(self, method, route, body=None, params=None, who="abc123def456", t_gw=1_000_000):
        ev = {"rawPath": "/api/tracker/" + route,
              "requestContext": {"http": {"method": method}, "timeEpoch": t_gw}}
        if who:
            ev["requestContext"]["authorizer"] = {"lambda": {"email_hash": who}}
        if body is not None:
            ev["body"] = json.dumps(body)
        if params:
            ev["queryStringParameters"] = params
        r = self.h.lambda_handler(ev, None, store=self.store)
        return r["statusCode"], json.loads(r["body"])

    def device(self, name="Heat pump"):
        st, b = self.call("POST", "devices", {"name": name, "desc": "outdoor unit"})
        self.assertEqual(st, 200, b)
        return b["device"]


class TestTheTrackerFailsClosed(TrackerFixture):
    def test_no_authorizer_context_is_a_refusal(self):
        # A route wired up without the authorizer must not become a public write endpoint.
        for method, route in (("GET", "devices"), ("POST", "events"), ("GET", "export")):
            st, b = self.call(method, route, {}, who=None)
            self.assertEqual(st, 401, route)
        self.assertEqual(self.store.items, {})


class TestDevicesAndStates(TrackerFixture):
    def test_a_new_device_has_off_and_on_and_off_is_its_default(self):
        d = self.device()
        names = {s["name"]: s["id"] for s in d["states"]}
        self.assertEqual(set(names), {"off", "on"})
        self.assertEqual(d["off"], names["off"])

    def test_renaming_changes_a_name_and_never_an_id(self):
        d = self.device()
        sid = d["states"][1]["id"]
        st, b = self.call("PATCH", "states", {"device": d["id"], "state": sid, "name": "cooling"})
        self.assertEqual(st, 200)
        renamed = next(s for s in b["device"]["states"] if s["id"] == sid)
        self.assertEqual(renamed["name"], "cooling")
        st, b = self.call("PATCH", "devices", {"device": d["id"], "name": "Heat pump (lounge)"})
        self.assertEqual((b["device"]["id"], b["device"]["name"]), (d["id"], "Heat pump (lounge)"))

    def test_states_are_arbitrary_and_the_off_one_cannot_be_archived(self):
        d = self.device()
        st, b = self.call("POST", "states", {"device": d["id"], "name": "defrost cycle"})
        self.assertEqual(st, 200)
        self.assertIn("defrost cycle", [s["name"] for s in b["device"]["states"]])
        st, b = self.call("PATCH", "states", {"device": d["id"], "state": d["off"], "archived": True})
        self.assertEqual(st, 400)
        self.assertIn("off state", b["error"])
        on = next(s["id"] for s in d["states"] if s["name"] == "on")
        st, b = self.call("PATCH", "devices", {"device": d["id"], "off": on})
        self.assertEqual(b["device"]["off"], on)
        st, _ = self.call("PATCH", "states", {"device": d["id"], "state": d["off"], "archived": True})
        self.assertEqual(st, 200, "once it is not the off state, it may go")

    def test_nothing_is_ever_deleted(self):
        with open(os.path.join(HERE, "cloud", "lambda", "tracker", "handler.py")) as fh:
            src = fh.read()
        self.assertNotIn("delete_item", src)
        with open(os.path.join(HERE, "cloud", "infrastructure", "lib", "live-stack.ts")) as fh:
            ts = fh.read()
        grant = ts[ts.index("// No Delete: nothing in the tracker"):]
        grant = grant[:grant.index("}));")]
        self.assertNotIn("DeleteItem", grant)


class TestEvents(TrackerFixture):
    def tap(self, d, sid, eid="eabc123456", **extra):
        body = dict({"id": eid, "device": d["id"], "state": sid, "tPhone": 2_000_000_000_000,
                     "sent": 2_000_000_000_500, "off": -1200, "rtt": 80, "syncAge": 3000,
                     "piT": 1_999_999_999_000, "piSeenPhone": 2_000_000_000_100, "piLat": 70},
                    **extra)
        return self.call("POST", "events", body, t_gw=2_000_000_000_200)

    def test_every_clock_is_recorded_and_the_estimate_uses_the_measured_offset(self):
        d = self.device()
        st, b = self.tap(d, d["states"][1]["id"])
        self.assertEqual(st, 200, b)
        e = b["event"]
        self.assertEqual(e["tEst"], 2_000_000_000_000 - 1200)
        for k in ("tPhone", "sent", "off", "rtt", "syncAge", "piT", "piSeenPhone", "piLat",
                  "tGw", "tServer", "by"):
            self.assertIn(k, e, k)
        self.assertEqual(e["tGw"], 2_000_000_000_200)
        self.assertEqual(e["by"], "abc123def456")
        self.assertTrue(e["sk"].startswith(f"{e['tEst']:013d}#"))

    def test_without_a_sync_the_estimate_is_the_gateways_less_the_wait(self):
        d = self.device()
        body = {"id": "enosync001", "device": d["id"], "state": d["off"],
                "tPhone": 5000, "sent": 7000}
        st, b = self.call("POST", "events", body, t_gw=1_000_000)
        self.assertEqual(b["event"]["tEst"], 1_000_000 - 2000)

    def test_a_retried_tap_is_the_same_event(self):
        d = self.device()
        self.tap(d, d["off"])
        st, b = self.tap(d, d["off"])
        self.assertEqual(st, 200)
        self.assertTrue(b["event"]["duplicate"])
        st, b = self.call("GET", "events", params={"device": d["id"]})
        self.assertEqual(len(b["events"]), 1)

    def test_an_edit_sits_beside_the_evidence_and_undo_is_soft(self):
        d = self.device()
        e = self.tap(d, d["off"])[1]["event"]
        st, b = self.call("PATCH", "events", {"device": d["id"], "sk": e["sk"],
                                              "tEdit": e["tEst"] - 4000, "tEst": 1, "tPhone": 1})
        self.assertEqual(st, 200, b)
        out = b["event"]
        self.assertEqual(out["tEdit"], e["tEst"] - 4000)
        self.assertEqual((out["tEst"], out["tPhone"]), (e["tEst"], e["tPhone"]),
                         "what was recorded at the tap is never overwritten")
        st, b = self.call("PATCH", "events", {"device": d["id"], "sk": e["sk"], "deleted": True})
        self.assertTrue(b["event"]["deleted"])
        st, b = self.call("GET", "events", params={"device": d["id"]})
        self.assertEqual(len(b["events"]), 1, "an undone event is kept, marked")
        st, b = self.call("PATCH", "events", {"device": d["id"], "sk": e["sk"], "tEdit": None})
        self.assertNotIn("tEdit", b["event"])

    def test_a_state_from_another_device_is_refused(self):
        a, b_dev = self.device("Kettle"), self.device("Oven")
        st, b = self.tap(a, b_dev["off"])
        self.assertEqual(st, 400)

    def test_bad_input_is_400(self):
        d = self.device()
        for body in ({"id": "x", "device": d["id"], "state": d["off"], "tPhone": 1},
                     {"id": "eabcdefgh", "device": d["id"], "state": d["off"]},
                     {"id": "eabcdefgh", "device": d["id"], "state": d["off"], "tPhone": "soon"},
                     {"id": "eabcdefgh", "device": d["id"], "state": d["off"], "tPhone": True}):
            st, _ = self.call("POST", "events", body)
            self.assertEqual(st, 400, body)

    def test_export_has_everything(self):
        d = self.device()
        self.tap(d, d["off"])
        self.tap(d, d["off"], eid="esecond0001", tPhone=2_000_000_005_000)
        st, b = self.call("GET", "export")
        self.assertEqual(st, 200)
        self.assertEqual(len(b["devices"]), 1)
        self.assertEqual([e["id"] for e in b["events"]], ["eabc123456", "esecond0001"])

    def test_the_clock_sync_route_answers_both_timestamps(self):
        st, b = self.call("GET", "time", t_gw=4242)
        self.assertEqual((st, b["recv"]), (200, 4242))
        self.assertGreaterEqual(b["send"], 0)


class TestTheAuthorizer(unittest.TestCase):
    """cloud/lambda/authorizer/index.js: the edge gate's decision, made for the API's hostname."""

    def setUp(self):
        with open(os.path.join(HERE, "cloud", "lambda", "authorizer", "index.js")) as fh:
            self.js = re.sub(r"/\*.*?\*/|//[^\n]*", "", fh.read(), flags=re.S)
        with open(os.path.join(HERE, "cloud", "infrastructure", "lib", "live-stack.ts")) as fh:
            self.ts = fh.read()

    def test_it_reuses_the_gates_verifier_and_config(self):
        self.assertIn("require('./jwt')", self.js)
        self.assertIn("require('./config')", self.js)
        self.assertIn("path.join(LAMBDA_DIR, 'auth-edge', 'jwt.js')", self.ts)
        self.assertIn("const authConfig = gateConfig(cfg);", self.ts)

    def test_it_checks_what_the_gate_checks(self):
        self.assertIn("verifyIdToken(token, cfg)", self.js)
        self.assertIn("claims.email_verified === false", self.js)
        self.assertIn("cfg.allowedEmails.includes(email)", self.js)
        self.assertIn("readCookie(header, cfg.cookies.id)", self.js)
        self.assertNotIn("sigen_id", self.js, "cookie names come from COOKIES, never a literal")

    def test_it_records_who_without_recording_the_address(self):
        self.assertIn("email_hash", self.js)
        self.assertNotRegex(self.js, r"context:\s*\{\s*email\s*:", "the address itself")

    def test_the_tracker_routes_are_always_behind_it(self):
        for route in ("'POST /api/tracker/live'", "'ANY /api/tracker/{proxy+}'"):
            line = re.search(re.escape(route) + r"[^\]]*\]", self.ts).group(0)
            self.assertRegex(line, r", true, \d+, \d+\]$", route)
        self.assertIn("const liveGated = !cfg.livePublic;", self.ts)
        self.assertIn("...(gated ? { authorizationType: 'CUSTOM', authorizerId: authorizer.ref } : {}),",
                      self.ts)

    def test_the_tracker_table_is_kept(self):
        block = self.ts[self.ts.index("'TrackerTable'"):]
        block = block[:block.index("});")]
        self.assertIn("removalPolicy: cdk.RemovalPolicy.RETAIN", block)
        self.assertIn("pointInTimeRecoveryEnabled: true", block)


class TestTheTrackerPage(unittest.TestCase):
    SITE = os.path.join(HERE, "cloud", "infrastructure", "lib", "site-stack.ts")

    def setUp(self):
        with open(self.SITE) as fh:
            self.ts = fh.read()

    def test_the_page_and_its_api_are_gated_whatever_live_public_says(self):
        devices = self.ts[self.ts.index("liveBehaviors['/devices'] = {"):]
        devices = devices[:devices.index("};")]
        self.assertIn("edgeLambdas: gate,", devices)
        self.assertNotIn("livePublic", devices)
        api = self.ts[self.ts.index("liveBehaviors['/api/tracker/*'] = {"):]
        api = api[:api.index("};")]
        self.assertIn("edgeLambdas: gateSigningBody,", api)
        self.assertIn("CACHING_DISABLED", api)
        self.assertIn("ALLOW_ALL", api)

    def test_every_file_it_loads_is_published(self):
        with open(os.path.join(HERE, "web", "devices.html")) as fh:
            refs = re.findall(r'(?:src|href)="(/[^"]*)"', fh.read())
        lists = []
        for name in ("WEB_ASSETS", "LIVE_ASSETS", "HTML_ENTRY_POINTS"):
            m = re.search(rf"const {name} = \[(.*?)\] as const;", self.ts, re.S)
            lists += re.findall(r"'([^']+)'", m.group(1))
        published = {"/" + f for f in lists} | {"/"}
        api = {r for r in refs if r.startswith("/api/")}
        self.assertEqual(set(refs) - published - api, set())
        self.assertIn("devices: 'devices.html'", self.ts)


if __name__ == "__main__":
    unittest.main()
