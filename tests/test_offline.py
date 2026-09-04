#!/usr/bin/env python3
"""
Offline test suite. NO HARDWARE, NO NETWORK.

    python3 -m unittest discover -s tests -v

Everything here runs against a FakeModbus serving a synthetic register image, so
the whole capture/decode path is exercisable with the inverter powered down --
which is the state it is in for a good part of any installation.

What this is for, beyond the usual: the archive format is binary and self-
describing via a plan fingerprint, so a mistake there silently produces
plausible-looking wrong numbers rather than an error. These tests assert the
things that would fail quietly.
"""
import gzip
import importlib.util
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alerts          # noqa: E402
import config          # noqa: E402
import decode          # noqa: E402
import discover        # noqa: E402
import dump            # noqa: E402
import lib             # noqa: E402
import log             # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_watchdog():
    """bin/watchdog.py as a module. It is a script, not an importable package path."""
    spec = importlib.util.spec_from_file_location(
        "sigen_watchdog", os.path.join(REPO, "bin", "watchdog.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# The plan hash of the shipped default block plan. Frozen deliberately: every
# archive file and manifest already written carries it in its filename, and
# decode.py refuses to decode across a mismatch. If a change moves this, existing
# archives become undecodable without hand-editing manifests -- so a change here
# has to be an intentional retune, with the old series segregated first.
DEFAULT_PLAN_HASH = "08c047b8"


def base_cfg(**over):
    cfg = dict(config.DEFAULTS)
    cfg.update({"host": "203.0.113.1", "manifest_identity": True})
    cfg.update(over)
    cfg["install_dir"] = cfg["install_dir"] or "/tmp/sigen-test"
    return cfg


class FakeModbus:
    """Serves deterministic register images. Records what was asked for.

    Registers not explicitly set read as their address, so every field decodes to
    something distinguishable and an off-by-one in a payload offset shows up as a
    wrong number rather than a plausible one.
    """

    def __init__(self, values=None, fail_units=()):
        self.regs = dict(values or {})
        self.fail_units = set(fail_units)
        self.reads = []
        self.connects = 1
        self.host = "fake"

    def set_string(self, addr, text, regs):
        raw = text.encode("ascii").ljust(regs * 2, b"\x00")
        for i in range(regs):
            self.regs[addr + i] = struct.unpack(">H", raw[i * 2:i * 2 + 2])[0]

    def read(self, unit, addr, count, fc, retries=2):
        self.reads.append((unit, addr, count, fc))
        if unit in self.fail_units:
            raise IOError("no route to host")
        return b"".join(struct.pack(">H", self.regs.get(addr + i, (addr + i) & 0xFFFF))
                        for i in range(count))

    def connection_age(self):
        return 0.0

    def close(self):
        pass

    # The real Modbus is a context manager and serial_probe() uses it as one.
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class DeadModbus(FakeModbus):
    """A device that is reachable but answers nothing -- port 502 refusing, not a slow link.

    `connection_age()` returning None is the whole point: the real Modbus drops its socket on
    a failed read, so during an outage there is no connection to report an age for. FakeModbus
    reports a constant 0.0 and so never exercised that path.
    """

    def __init__(self, host=None, port=502, timeout=6.0):
        super().__init__()          # stands in for Modbus(host, port, timeout_s)
        # The real Modbus keeps the address it was constructed with, and rediscovery
        # works by mutating it. Swallowing it here left mb.host reading "fake", which
        # hid the very distinction "did we adopt a different address?" depends on.
        if host is not None:
            self.host = host

    def read(self, unit, addr, count, fc, retries=2):
        raise IOError("connection refused")

    def connection_age(self):
        return None


class TestPlanHash(unittest.TestCase):
    def test_default_plan_hash_is_frozen(self):
        self.assertEqual(log.plan_hash(log.build_tiers(base_cfg())),
                         DEFAULT_PLAN_HASH,
                         "the default block plan's fingerprint changed: every "
                         "existing archive becomes undecodable")

    def test_retuning_cadence_changes_the_hash(self):
        a = log.plan_hash(log.build_tiers(base_cfg(fast_period_s=2)))
        b = log.plan_hash(log.build_tiers(base_cfg(fast_period_s=1)))
        self.assertNotEqual(a, b)

    def test_changing_unit_ids_changes_the_hash(self):
        a = log.plan_hash(log.build_tiers(base_cfg()))
        b = log.plan_hash(log.build_tiers(base_cfg(plant_unit=246)))
        self.assertNotEqual(a, b)

    def test_gates_are_relative_to_the_tick_window(self):
        self.assertEqual(log.gates(base_cfg(fast_period_s=1)), (500, 900))
        self.assertEqual(log.gates(base_cfg(fast_period_s=2)), (1000, 1800))


class TestLibPrimitives(unittest.TestCase):
    def test_gain_is_a_divisor(self):
        f = {"key": "x", "dtype": "U16", "gain": 10, "count": 1}
        self.assertEqual(lib.decode(struct.pack(">H", 1234), f), (1234, 123.4))

    def test_power_factor_override_reads_as_signed(self):
        f = {"key": "inverter_power_factor", "dtype": "U16", "gain": 1000, "count": 1}
        raw, val = lib.decode(struct.pack(">H", 64538), f)
        self.assertEqual(raw, -998)          # not 64538
        self.assertAlmostEqual(val, -0.998)  # a PF is bounded to [-1, 1]

    def test_sentinels_and_absent_markers_are_distinguished(self):
        self.assertTrue(lib.is_sentinel(0xFFFF, "U16"))
        self.assertTrue(lib.is_sentinel(0xFFFFFFFF, "U32"))
        self.assertFalse(lib.is_sentinel(-1, "S16"))    # -1 is a legal signed value
        self.assertTrue(lib.is_absent_marker(-1, "S16"))
        self.assertFalse(lib.is_absent_marker(-1, "U16"))

    def test_epoch_registers_decode_as_gmt(self):
        # The device stores LOCAL time as an epoch count, so localtime() would
        # apply the offset twice.
        self.assertEqual(lib.device_local(0), "1970-01-01 00:00:00")

    def test_pct_clamps_on_short_lists(self):
        self.assertEqual(lib.pct([5], 0.95), 5)
        self.assertEqual(lib.pct([1, 2, 3, 4], 0.95), 4)

    def test_plan_blocks_start_and_end_on_field_boundaries(self):
        fields = [{"addr": 100, "count": 2, "key": "a"},
                  {"addr": 102, "count": 1, "key": "b"},
                  {"addr": 400, "count": 1, "key": "c"}]  # beyond MAX_GAP
        blocks = lib.plan_blocks(fields)
        self.assertEqual([(s, n) for s, n, _ in blocks], [(100, 3), (400, 1)])
        for start, span, group in blocks:
            self.assertLessEqual(span, lib.MAX_SPAN)


class ArchiveFixture(unittest.TestCase):
    """Builds a synthetic archive on disk that every decode mode can run against."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sigen-test-")
        self.cfg = base_cfg()
        self.tiers = log.build_tiers(self.cfg)
        self.bundle = dump.load_regmap()
        self.mb = FakeModbus()
        self.mb.set_string(30500, "SigenStor TEST", 15)
        self.mb.set_string(30515, "TESTSERIAL", 10)
        self.mb.set_string(30525, "V0.0.0", 15)
        self.manifest = log.build_manifest(self.mb, self.bundle, self.cfg, self.tiers)

    def write_archive(self, n_data=8, n_empty=0, stamp="20260814T090000",
                      t0=None, gzip_it=False):
        """Write one archive file: n_data full records then n_empty mask-0 ones."""
        t0 = time.time() - 600 if t0 is None else t0
        path = os.path.join(self.tmp, f"sigen-{stamp}-{self.manifest['plan_hash']}.bin")
        blob = io.BytesIO()
        full_mask = (1 << len(self.tiers)) - 1
        payload = b"".join(
            self.mb.read(u, a, c, f) for _, u, a, c, f, _, _ in self.tiers)
        for i in range(n_data):
            blob.write(struct.pack(log.HEADER, t0 + i * 2, full_mask, 90 + i))
            blob.write(payload)
        for i in range(n_empty):
            # Outage probes: header only, and a latency that is socket timeout
            blob.write(struct.pack(log.HEADER, t0 + n_data * 2 + i * 30, 0, 6500))
        data = blob.getvalue()
        if gzip_it:
            path += ".gz"
            with gzip.open(path, "wb") as fh:
                fh.write(data)
        else:
            with open(path, "wb") as fh:
                fh.write(data)
        mpath = os.path.join(
            self.tmp, f"sigen-20260814-{self.manifest['plan_hash']}.manifest.json")
        with open(mpath, "w") as fh:
            json.dump(self.manifest, fh)
        return mpath, path

    @staticmethod
    def load_json(path):
        with open(path) as fh:
            return json.load(fh)

    @staticmethod
    def capture_stdout(fn, *a, **kw):
        """stdout is what we assert on; stderr is swallowed so notes and skip
        warnings do not litter the test run."""
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            fn(*a, **kw)
        return buf.getvalue()


class TestManifest(ArchiveFixture):
    def test_manifest_describes_every_block_and_its_payload_offset(self):
        blocks = self.manifest["blocks"]
        self.assertEqual(len(blocks), len(self.tiers))
        off = 0
        for b, t in zip(blocks, self.tiers):
            self.assertEqual(b["label"], t[0])
            self.assertEqual(b["bytes"], t[3] * 2)
            self.assertEqual(b["payload_offset_if_present"], off)
            off += t[3] * 2
        self.assertEqual(self.manifest["format"], "sigen-raw-1")
        self.assertEqual(self.manifest["fast_period_s"], self.cfg["fast_period_s"])

    def test_identity_is_read_and_recorded(self):
        self.assertEqual(self.manifest["device"]["model"], "SigenStor TEST")
        self.assertEqual(self.manifest["device"]["serial"], "TESTSERIAL")
        self.assertEqual(self.manifest["host"], "203.0.113.1")

    def test_manifest_identity_false_omits_serial_and_host(self):
        cfg = base_cfg(manifest_identity=False)
        m = log.build_manifest(self.mb, self.bundle, cfg, log.build_tiers(cfg))
        self.assertNotIn("serial", m["device"])
        self.assertIsNone(m["host"])
        # ...but the block plan, which is what decoding needs, is still complete
        self.assertEqual(len(m["blocks"]), len(self.tiers))
        self.assertEqual(m["plan_hash"], DEFAULT_PLAN_HASH)


class TestArchiveWriter(ArchiveFixture):
    def test_write_rotate_and_gzip(self):
        archive = log.Archive(self.tmp, rotate_minutes=0, manifest=self.manifest)
        payload = b"\x00\x02"
        archive.write(time.time(), 1, 100, payload)
        archive.write(time.time(), 1, 100, payload)
        archive.close()
        self.assertEqual(archive.records, 2)
        self.assertEqual(archive.bytes_written, 2 * (log.HEADER_LEN + 2))
        self.assertTrue(os.path.exists(archive.manifest_path))

    def test_close_gzips_the_final_file_so_it_is_not_stranded(self):
        """Only `.bin.gz` travels, so a `.bin` left by a clean shutdown never reaches S3.

        close() used to shut the handle and stop there, which stranded the closing file of
        every run -- see FINDINGS 28. sync.py reports those but deliberately never uploads
        one, so the fix has to happen here, while this process still owns the file.
        """
        archive = log.Archive(self.tmp, rotate_minutes=0, manifest=self.manifest)
        archive.write(time.time(), 1, 100, b"\x00\x02")
        archive.close()
        names = os.listdir(self.tmp)
        gz = [n for n in names if n.endswith(".bin.gz")]
        self.assertEqual(len(gz), 1, names)
        self.assertEqual([n for n in names if n.endswith(".bin")], [],
                         "a plain .bin survived close(); sync.py would never upload it")
        # Name alone is not enough: an empty or truncated .gz would pass the check above.
        with gzip.open(os.path.join(self.tmp, gz[0]), "rb") as fh:
            self.assertEqual(len(fh.read()), log.HEADER_LEN + 2)

    def test_close_on_an_archive_that_never_opened_a_file_is_a_no_op(self):
        """A capture that dies before its first record has no path to compress."""
        archive = log.Archive(self.tmp, 0, self.manifest)
        archive.close()
        self.assertEqual([n for n in os.listdir(self.tmp) if ".bin" in n], [])

    def test_manifest_is_written_once_per_day(self):
        a1 = log.Archive(self.tmp, 0, self.manifest)
        first = os.path.getmtime(a1.manifest_path)
        a2 = log.Archive(self.tmp, 0, self.manifest)
        self.assertEqual(a1.manifest_path, a2.manifest_path)
        self.assertEqual(first, os.path.getmtime(a2.manifest_path))


class TestCaptureDuringAnOutage(ArchiveFixture):
    """capture() against a device that is up but refusing Modbus.

    Nothing else drives capture() itself, which is how the crash below shipped: every test
    exercised the pieces, none ran the loop with the device down. These are deliberately
    short -- the loop paces itself at one tick per second, so duration is in seconds.
    """

    def setUp(self):
        super().setUp()
        real = log.Modbus
        log.Modbus = DeadModbus          # capture() builds its own transport
        self.addCleanup(lambda: setattr(log, "Modbus", real))

    def test_a_scheduled_recycle_with_no_connection_does_not_crash(self):
        """A recycle tick during an outage formatted None with {age:.0f} and died.

        KeepAlive restarted it every time, so the daemon looked alive for eleven hours
        while the archive took nothing -- and each restart stranded a `.bin`.
        """
        cfg = base_cfg(recycle_s=2, degrade_after=1, degrade_probe_s=1)
        out = self.capture_stdout(log.capture, cfg, self.tiers, self.tmp,
                                  3, 0, False, self.bundle)
        # Proof the recycle tick at i=2 really ran with the device down, so this cannot
        # pass by never reaching the path that broke.
        self.assertIn("[degraded]", out)
        # Nothing was open, so nothing should claim to have recycled anything.
        self.assertNotIn("[recycle]", out)

    def test_an_outage_still_reports_the_device_as_unreachable(self):
        """The fix must stay quiet about recycles without muting the actual diagnosis."""
        cfg = base_cfg(recycle_s=2, degrade_after=1, degrade_probe_s=1)
        out = self.capture_stdout(log.capture, cfg, self.tiers, self.tmp,
                                  3, 0, False, self.bundle)
        self.assertIn("retry-exhausted failures", out)
        self.assertNotIn("final connection age", out)


class TestDecodeRoundTrip(ArchiveFixture):
    def test_records_round_trip_with_expected_values(self):
        mpath, path = self.write_archive(n_data=5)
        manifest = self.load_json(mpath)
        recs = list(decode.records(manifest, [path]))
        self.assertEqual(len(recs), 5)
        ts, latency, blocks = recs[0]
        self.assertEqual(latency, 90)
        self.assertEqual(sorted(blocks), list(range(len(self.tiers))))
        # FakeModbus returns each register as its own address, so a field at 30005
        # with gain 1000 must decode to 30005/1000 -- an offset slip would give a
        # neighbouring address instead of an error.
        located, missing = decode.resolve(manifest, self.bundle,
                                          ["plant_grid_sensor_active_power"])
        self.assertEqual(missing, [])
        _, loc = located[0]
        idx, off, field = loc
        self.assertEqual(field["addr"], 30005)
        raw, _ = lib.decode(blocks[idx][off:off + field["count"] * 2], field)
        self.assertEqual(raw, (30005 << 16) + 30006)  # S32, big-endian pair

    def test_truncated_final_record_stops_cleanly(self):
        mpath, path = self.write_archive(n_data=4)
        with open(path, "ab") as fh:
            fh.write(b"\x01\x02\x03\x04\x05")   # a record torn by a hard kill
        manifest = self.load_json(mpath)
        self.assertEqual(len(list(decode.records(manifest, [path]))), 4)

    def test_empty_records_decode_as_absent_blocks(self):
        mpath, path = self.write_archive(n_data=2, n_empty=3)
        manifest = self.load_json(mpath)
        recs = list(decode.records(manifest, [path]))
        self.assertEqual(len(recs), 5)
        self.assertEqual([bool(r[2]) for r in recs], [True, True, False, False, False])

    def test_gzipped_and_plain_files_both_read(self):
        mpath, plain = self.write_archive(n_data=3, stamp="20260814T090000")
        _, gz = self.write_archive(n_data=2, stamp="20260814T100000", gzip_it=True)
        manifest = self.load_json(mpath)
        self.assertEqual(len(list(decode.records(manifest, [plain, gz]))), 5)

    def test_files_sort_chronologically_not_by_argv(self):
        # `*.bin *.bin.gz` expands plain-before-compressed, which is
        # reverse-chronological for a rotating archive and produces phantom
        # counter drops that look exactly like corruption.
        argv_order = ["sigen-20260814T100000-aaaaaaaa.bin",
                      "sigen-20260814T090000-aaaaaaaa.bin.gz"]
        self.assertEqual(decode.sort_chronologically(argv_order),
                         [argv_order[1], argv_order[0]])

    def test_plan_hash_mismatch_refuses_rather_than_guessing(self):
        mpath, path = self.write_archive(n_data=2)
        manifest = self.load_json(mpath)
        manifest["plan_hash"] = "deadbeef"
        with self.assertRaises(SystemExit) as cm:
            decode.check_plan_hash(manifest, [path])
        self.assertIn("plan-hash mismatch", str(cm.exception))

    def test_missing_plan_hash_is_also_a_mismatch(self):
        mpath, path = self.write_archive(n_data=2)
        manifest = self.load_json(mpath)
        del manifest["plan_hash"]
        with self.assertRaises(SystemExit):
            decode.check_plan_hash(manifest, [path])

    def test_unknown_field_keys_degrade_rather_than_crash(self):
        mpath, path = self.write_archive(n_data=2)
        manifest = self.load_json(mpath)
        located, missing = decode.resolve(manifest, self.bundle,
                                         ["plant_ess_soc", "not_a_real_field"])
        self.assertEqual([k for k, _ in located], ["plant_ess_soc"])
        self.assertEqual(missing, [("not_a_real_field", "not in regmap")])


class TestDecodeReports(ArchiveFixture):
    def test_check_reports_records_and_cadence(self):
        mpath, path = self.write_archive(n_data=10)
        manifest = self.load_json(mpath)
        out = self.capture_stdout(decode.emit_check, manifest, self.bundle, [path])
        self.assertIn("records            10", out)
        self.assertIn("device clock", out)

    def test_latency_runs_and_states_a_verdict(self):
        # The regression this guards: emit_latency was documented and called but
        # never defined, so --latency raised NameError on every invocation.
        mpath, path = self.write_archive(n_data=30, t0=time.time() - 3600)
        manifest = self.load_json(mpath)
        out = self.capture_stdout(decode.emit_latency, manifest, [path], 10)
        self.assertIn("median range", out)
        self.assertIn("verdict", out)

    def test_latency_excludes_outage_probes_from_the_medians(self):
        # 6500 ms probe latencies are socket timeout, not device latency. Averaged
        # in, they would swamp any bucket they landed in.
        mpath, path = self.write_archive(n_data=6, n_empty=6,
                                        t0=time.time() - 3600)
        manifest = self.load_json(mpath)
        out = self.capture_stdout(decode.emit_latency, manifest, [path], 60)
        self.assertIn("excluded       6 empty probe records", out)
        self.assertNotIn("6500", out.split("verdict")[0].split("excluded")[0])

    def test_last_reports_data_freshness_not_record_freshness(self):
        mpath, path = self.write_archive(n_data=3, n_empty=4, t0=time.time() - 300)
        manifest = self.load_json(mpath)
        out = self.capture_stdout(decode.emit_last, manifest, self.bundle, [path],
                                  decode.DEFAULT_FIELDS)
        self.assertIn("DEVICE NOT ANSWERING", out)
        self.assertIn("logger is HEALTHY", out)
        self.assertIn("LAST KNOWN GOOD", out)

    def test_last_on_a_healthy_tail_shows_current_values(self):
        mpath, path = self.write_archive(n_data=4, t0=time.time() - 8)
        manifest = self.load_json(mpath)
        out = self.capture_stdout(decode.emit_last, manifest, self.bundle, [path],
                                  ["plant_ess_soc"])
        self.assertIn("latest record", out)
        self.assertNotIn("DEVICE NOT ANSWERING", out)

    def test_balance_emits_the_export_series(self):
        mpath, path = self.write_archive(n_data=3)
        manifest = self.load_json(mpath)
        out = self.capture_stdout(decode.emit_balance, manifest, self.bundle,
                                  [path], 0, 0)
        self.assertIn("grid_kw,import_kw,export_kw", out)
        self.assertEqual(len(out.strip().splitlines()), 4)  # header + 3 rows

    def test_all_covered_fields_are_decodable(self):
        mpath, path = self.write_archive(n_data=1)
        manifest = self.load_json(mpath)
        keys = decode.covered_fields(manifest, self.bundle)
        self.assertGreater(len(keys), 200)
        out = self.capture_stdout(decode.emit_rows, manifest, self.bundle, [path],
                                  keys, "csv", 0, 0)
        self.assertEqual(len(out.strip().splitlines()), 2)


class TestScheduler(unittest.TestCase):
    def test_fast_blocks_share_a_tick_and_slow_ones_do_not(self):
        cfg = base_cfg(fast_period_s=2)
        tiers = log.build_tiers(cfg)
        # Peak requests per tick must stay at 3: the slow tiers sit on odd offsets
        # so they land on ticks the fast tier skips.
        peak = max(sum(1 for idx in range(len(tiers)) if log.fires(tiers, idx, t))
                   for t in range(600))
        self.assertEqual(peak, 3)

    def test_idle_ticks_exist_at_half_hertz(self):
        tiers = log.build_tiers(base_cfg(fast_period_s=2))
        idle = [t for t in range(60)
                if not any(log.fires(tiers, idx, t) for idx in range(len(tiers)))]
        self.assertTrue(idle, "at 0.5 Hz some ticks must have nothing scheduled")

    def test_bucket_labels_are_unique_per_bucket(self):
        # The heartbeat fires on label transition, so a repeated label means it
        # never fires. Whole-minute labelling collapsed every sub-minute bucket.
        for bucket_s in (300, 60, 8, 45, 7):
            labels = [log.bucket_label(i, bucket_s) for i in range(bucket_s * 4)]
            distinct = []
            for lab in labels:
                if not distinct or distinct[-1] != lab:
                    distinct.append(lab)
            self.assertEqual(len(distinct), 4, f"bucket_s={bucket_s} collapsed")
            self.assertEqual(len(set(distinct)), 4, f"bucket_s={bucket_s} repeats")

    def test_whole_minute_buckets_keep_the_minute_label(self):
        self.assertEqual(log.bucket_label(0, 300), "  0-5 min")
        self.assertEqual(log.bucket_label(299, 300), "  0-5 min")
        self.assertEqual(log.bucket_label(300, 300), "  5-10 min")

    def test_locate_finds_a_field_in_its_block(self):
        cfg = base_cfg()
        bundle = dump.load_regmap()
        idx, off, field = log.locate(bundle, log.build_tiers(cfg),
                                    "plant_system_time")
        self.assertEqual(field["addr"], 30000)
        self.assertEqual(off, 0)
        self.assertEqual(idx, 0)


class TestSoakReport(ArchiveFixture):
    """The soak table must not silently drop an outage."""

    def bucket(self, lat, empty=0, label="  0-5 min"):
        return {"label": label, "lat": list(lat), "empty": empty, "over": 0,
                "retry": 0, "fail": 0}

    def test_a_dataless_bucket_is_reported_not_dropped(self):
        archive = log.Archive(self.tmp, 0, self.manifest)
        buckets = [self.bucket([90, 95, 100], label="  0-5 min"),
                   self.bucket([], empty=10, label="  5-10 min")]
        out = self.capture_stdout(log.soak_report, buckets, {}, archive, self.mb,
                                  300, self.cfg, self.tiers)
        archive.close()
        self.assertIn("NO DATA", out)
        self.assertIn("every tick returned data", out)
        self.assertIn("10 empty ticks", out)
        self.assertIn("1 bucket(s) with none at all", out)

    def test_a_clean_soak_passes_the_empty_tick_gate(self):
        archive = log.Archive(self.tmp, 0, self.manifest)
        out = self.capture_stdout(log.soak_report,
                                  [self.bucket([90, 95, 100])], {}, archive,
                                  self.mb, 300, self.cfg, self.tiers)
        archive.close()
        self.assertIn("PASS  every tick returned data", out)

    def test_gates_are_quoted_from_the_tick_window(self):
        archive = log.Archive(self.tmp, 0, self.manifest)
        out = self.capture_stdout(log.soak_report,
                                  [self.bucket([90.0])], {}, archive, self.mb,
                                  300, base_cfg(fast_period_s=2), self.tiers)
        archive.close()
        self.assertIn("every bucket p95 < 1000 ms", out)
        self.assertIn("every bucket max < 1800 ms", out)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sigen-cfg-")
        self.path = os.path.join(self.tmp, "config.json")
        self._env = {k: v for k, v in os.environ.items() if k.startswith("SIGEN_")}
        for k in self._env:
            del os.environ[k]
        os.environ["SIGEN_CONFIG"] = self.path

    def tearDown(self):
        for k in [k for k in os.environ if k.startswith("SIGEN_")]:
            del os.environ[k]
        os.environ.update(self._env)

    def write(self, obj):
        with open(self.path, "w") as fh:
            json.dump(obj, fh)

    def test_defaults_apply_when_the_file_only_sets_host(self):
        self.write({"host": "203.0.113.9"})
        cfg = config.load()
        self.assertEqual(cfg["host"], "203.0.113.9")
        self.assertEqual(cfg["port"], 502)
        self.assertEqual(cfg["fast_period_s"], 2)

    def test_env_overrides_file_and_flags_override_env(self):
        self.write({"host": "from.file", "fast_period_s": 2})
        os.environ["SIGEN_HOST"] = "from.env"
        os.environ["SIGEN_FAST_PERIOD_S"] = "5"
        cfg = config.load()
        self.assertEqual(cfg["host"], "from.env")
        self.assertEqual(cfg["fast_period_s"], 5)      # coerced from the string
        cfg = config.load(overrides={"host": "from.flag"})
        self.assertEqual(cfg["host"], "from.flag")

    def test_missing_host_is_an_actionable_error(self):
        self.write({"port": 502})
        with self.assertRaises(SystemExit) as cm:
            config.load()
        self.assertIn("config.example.json", str(cm.exception))

    def test_unknown_keys_are_rejected_but_underscore_comments_are_not(self):
        self.write({"_": "a comment", "host": "h"})
        self.assertEqual(config.load()["host"], "h")
        self.write({"host": "h", "hsot": "typo"})
        with self.assertRaises(SystemExit) as cm:
            config.load()
        self.assertIn("hsot", str(cm.exception))

    def test_data_and_log_dirs_default_under_install_dir(self):
        self.write({"host": "h", "install_dir": "/opt/sigen"})
        cfg = config.load()
        self.assertEqual(cfg["data_dir"], "/opt/sigen/data")
        self.assertEqual(cfg["log_dir"], "/opt/sigen/logs")

    def test_shipped_example_config_is_valid(self):
        # A broken example is the first thing a new user hits.
        os.environ["SIGEN_CONFIG"] = config.EXAMPLE
        cfg = config.load()
        self.assertTrue(cfg["host"])
        self.assertEqual(cfg["launchd_label"], "local.sigen-logger")

    def test_sh_output_is_evaluable_assignments(self):
        self.write({"host": "h", "install_dir": "/opt/sigen with space"})
        out = ArchiveFixture.capture_stdout(config.emit_sh, config.load())
        self.assertIn("SIGEN_HOST=h", out)
        self.assertIn("'/opt/sigen with space'", out)   # quoted, so eval is safe

    def test_sh_exports_the_keys_install_sync_refuses_to_install_without(self):
        # deploy/install-sync.sh can only refuse what --sh tells it, and it used to be
        # blind to sync_enabled: the install then SUCCEEDED and the daemon declined to
        # upload every five minutes into a log nobody was watching. sync_enabled reaches sh
        # as the literal "True", because emit_sh prints booleans through str() -- so the
        # export and the installer's comparison have to move together or the guard is
        # silently unenforceable. Asserting both halves here is what keeps them together.
        self.write({"host": "h", "s3_bucket": "b", "sync_enabled": True})
        out = ArchiveFixture.capture_stdout(config.emit_sh, config.load())
        self.assertIn("SIGEN_SYNC_ENABLED=True", out,
                      "install-sync.sh cannot check a key --sh does not export")
        self.assertIn("SIGEN_S3_REGION=", out,
                      "a wrong region is a confusing runtime failure and a cheap check")
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "deploy", "install-sync.sh")) as fh:
            sh = fh.read()
        self.assertIn('"$SIGEN_SYNC_ENABLED" = "True"', sh,
                      "the installer must compare against the literal emit_sh prints")

    def test_no_two_launchd_labels_may_be_one(self):
        # Four daemons, four files under /Library/LaunchDaemons. A shared label means the
        # second install overwrites the first -- the logger quietly becoming the uploader,
        # or the watchdog replacing the thing it is meant to watch. Every pair, not just
        # the first: adding the watchdog is exactly the change that could leave a new
        # collision unchecked.
        keys = ("launchd_label", "web_launchd_label", "sync_launchd_label",
                "watchdog_launchd_label")
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                self.write({"host": "h", a: "same", b: "same"})
                with self.assertRaises(SystemExit) as cm:
                    config.load()
                self.assertIn("must all differ", str(cm.exception), f"{a} vs {b}")

    def test_the_watchdog_label_is_exported_to_sh(self):
        # bin/status.sh and deploy/install-watchdog.sh both read it from --sh.
        self.write({"host": "h"})
        out = ArchiveFixture.capture_stdout(config.emit_sh, config.load())
        self.assertIn("SIGEN_WATCHDOG_LAUNCHD_LABEL=local.sigen-watchdog", out)

    def test_render_substitutes_every_token(self):
        self.write({"host": "h", "install_dir": "/opt/sigen",
                    "run_as_user": "someone"})
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        text = config.render(config.load(),
                             os.path.join(here, "deploy/launchd.plist.template"))
        self.assertNotIn("@", text.replace("@LABEL@", ""))
        self.assertIn("<string>someone</string>", text)
        self.assertIn("/opt/sigen/log.py", text)

    def test_render_refuses_a_daemon_plist_with_no_user(self):
        # An empty <string></string> for UserName loads, and runs as root.
        self.write({"host": "h"})
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with self.assertRaises(SystemExit) as cm:
            config.render(config.load(),
                          os.path.join(here, "deploy/launchd.plist.template"))
        self.assertIn("run_as_user", str(cm.exception))


class TestReadOnly(unittest.TestCase):
    """The safety claim, asserted rather than merely documented."""

    def test_capture_only_ever_issues_read_function_codes(self):
        cfg = base_cfg()
        tiers = log.build_tiers(cfg)
        mb = FakeModbus()
        log.build_manifest(mb, dump.load_regmap(), cfg, tiers)
        for _, unit, addr, count, fc, _, _ in tiers:
            mb.read(unit, addr, count, fc)
        self.assertTrue(mb.reads)
        for unit, addr, count, fc in mb.reads:
            self.assertIn(fc, (3, 4), "FC3/FC4 are reads; anything else writes")

    def test_no_source_file_issues_a_write_function_code(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("lib.py", "log.py", "dump.py", "decode.py", "series.py",
                     "serve.py"):
            with open(os.path.join(here, name)) as fh:
                src = fh.read()
            for fc in (" 5,", " 6,", " 15,", " 16,"):
                self.assertNotIn(f"fc={fc.strip().rstrip(',')}", src,
                                 f"{name} may be issuing a Modbus write")


class TestSweepTargets(unittest.TestCase):
    """Which addresses a rediscovery sweep will try, and in what order."""

    def test_hints_are_tried_first_and_in_order(self):
        targets = discover.sweep_targets(["10.0.0.9", "10.0.0.4"])
        self.assertEqual(targets[:2], ["10.0.0.9", "10.0.0.4"])

    def test_both_the_old_subnet_and_the_local_one_are_covered(self):
        """If the router's LAN range is what changed, the old /24 is empty.

        Searching only the hint's subnet would find nothing and give no clue why.
        """
        real = discover.local_ipv4
        discover.local_ipv4 = lambda: "192.168.9.20"
        self.addCleanup(lambda: setattr(discover, "local_ipv4", real))
        targets = discover.sweep_targets(["10.0.0.4"])
        self.assertIn("10.0.0.77", targets)
        self.assertIn("192.168.9.77", targets)

    def test_broadcast_network_and_self_are_never_probed(self):
        real = discover.local_ipv4
        discover.local_ipv4 = lambda: "10.0.0.20"
        self.addCleanup(lambda: setattr(discover, "local_ipv4", real))
        targets = discover.sweep_targets(["10.0.0.4"])
        self.assertNotIn("10.0.0.0", targets)
        self.assertNotIn("10.0.0.255", targets)
        self.assertNotIn("10.0.0.20", targets, "we are not the inverter")

    def test_a_malformed_hint_does_not_produce_a_garbage_subnet(self):
        real = discover.local_ipv4
        discover.local_ipv4 = lambda: None
        self.addCleanup(lambda: setattr(discover, "local_ipv4", real))
        self.assertEqual(discover.sweep_targets(["not-an-ip"]), ["not-an-ip"])


class DiscoverFixture(unittest.TestCase):
    """discover.find() with the port sweep stubbed out. No sockets are opened."""

    def stub_listeners(self, hosts):
        real = discover.listeners
        discover.listeners = lambda targets, port, **kw: list(hosts)
        self.addCleanup(lambda: setattr(discover, "listeners", real))

    @staticmethod
    def probe_from(table):
        """table: {host: serial | (serial, latency_ms) | Exception}.

        A bare serial gets a latency derived from dict order, so "first in the table is
        fastest" -- which lets a test state an ordering without stating numbers.
        """
        def probe(host):
            v = table[host]
            if isinstance(v, Exception):
                raise v
            if isinstance(v, tuple):
                return v
            return v, 10.0 * (list(table).index(host) + 1)
        return probe


class TestFindRefusesWithoutAKnownSerial(DiscoverFixture):
    """The safety property: no serial, no adoption.

    "Something is listening on 502" is not "this is my inverter" -- a neighbour's unit
    answers identically, and adopting it would record someone else's plant into an
    archive claiming to be this one's, looking entirely healthy while doing it.
    """

    def test_no_serial_means_no_host_and_a_stated_reason(self):
        self.stub_listeners(["10.0.0.5"])
        said = []
        for empty in (None, ""):
            self.assertIsNone(discover.find(empty, self.probe_from({}),
                                            log=said.append))
        self.assertTrue(any("refusing to adopt" in s for s in said))

    def test_the_probe_is_never_even_called_without_a_serial(self):
        def explode(_host):
            raise AssertionError("probed the LAN with nothing to compare against")
        self.stub_listeners(["10.0.0.5"])
        self.assertIsNone(discover.find(None, explode))


class TestFindPicksTheFastestMatch(DiscoverFixture):
    def test_the_wired_interface_wins_over_the_wireless_one(self):
        """The unit answers on both its Ethernet port and its Wi-Fi module with the
        same serial. Picking by latency lands on the wired one without anything here
        having to know which address is which.

        The numbers are the ones measured on the real unit: 19 ms wired, 70 ms wireless.
        """
        self.stub_listeners(["10.0.0.13", "10.0.0.15"])
        probe = self.probe_from({"10.0.0.13": ("SN1", 70.0), "10.0.0.15": ("SN1", 19.0)})
        found = discover.find("SN1", probe, log=lambda _s: None)
        self.assertEqual(found["host"], "10.0.0.15")
        self.assertEqual(len(found["matches"]), 2)
        self.assertLess(found["matches"][0][1], found["matches"][1][1])

    def test_the_probes_latency_decides_not_the_order_it_was_found_in(self):
        """The regression this contract exists for. Timing one cold call inside find()
        ranked the real inverter's Wi-Fi module above its wired port, because the cold
        figure includes the TCP connect and the first host probed absorbs whatever the
        sweep just did to the device. So: slowest to answer, but reports the best read
        latency, must still win."""
        self.stub_listeners(["10.0.0.13", "10.0.0.15"])

        def probe(host):
            if host == "10.0.0.15":
                time.sleep(0.05)             # answers last
                return "SN1", 19.0           # but is genuinely the faster link
            return "SN1", 70.0
        found = discover.find("SN1", probe, log=lambda _s: None)
        self.assertEqual(found["host"], "10.0.0.15")

    def test_a_match_that_could_not_be_timed_does_not_win_on_a_missing_number(self):
        self.stub_listeners(["10.0.0.13", "10.0.0.15"])
        probe = self.probe_from({"10.0.0.13": ("SN1", None), "10.0.0.15": ("SN1", 70.0)})
        found = discover.find("SN1", probe, log=lambda _s: None)
        self.assertEqual(found["host"], "10.0.0.15")

    def test_a_different_serial_is_reported_but_never_adopted(self):
        self.stub_listeners(["10.0.0.7", "10.0.0.15"])
        probe = self.probe_from({"10.0.0.7": "SOMEONE-ELSE", "10.0.0.15": "SN1"})
        said = []
        found = discover.find("SN1", probe, log=said.append)
        self.assertEqual(found["host"], "10.0.0.15")
        self.assertEqual([h for h, _ in found["matches"]], ["10.0.0.15"])
        self.assertTrue(any("not ours" in s for s in said),
                        "a second, different inverter on the LAN must be logged loudly")

    def test_no_match_returns_none_and_says_how_many_were_listening(self):
        self.stub_listeners(["10.0.0.7"])
        said = []
        self.assertIsNone(discover.find("SN1", self.probe_from({"10.0.0.7": "OTHER"}),
                                        log=said.append))
        self.assertTrue(any("none reported serial" in s for s in said))

    def test_nothing_listening_returns_none(self):
        self.stub_listeners([])
        said = []
        self.assertIsNone(discover.find("SN1", self.probe_from({}), log=said.append))
        self.assertTrue(any("nothing listening" in s for s in said))

    def test_a_probe_that_raises_does_not_abort_the_search(self):
        """One unreachable host among several must not cost us the one that answers."""
        self.stub_listeners(["10.0.0.7", "10.0.0.15"])
        probe = self.probe_from({"10.0.0.7": IOError("refused"), "10.0.0.15": "SN1"})
        found = discover.find("SN1", probe, log=lambda _s: None)
        self.assertEqual(found["host"], "10.0.0.15")


class TestHostStateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sigen-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_round_trips_and_tolerates_absence(self):
        self.assertIsNone(discover.read_host_state(self.tmp))
        discover.write_host_state(self.tmp, "10.0.0.15")
        self.assertEqual(discover.read_host_state(self.tmp), "10.0.0.15")

    def test_writing_never_raises_on_a_bad_path(self):
        discover.write_host_state(None, "10.0.0.15")
        discover.write_host_state(os.path.join(self.tmp, "x\x00y"), "10.0.0.15")


class TestKnownSerial(ArchiveFixture):
    def test_a_live_read_wins(self):
        self.assertEqual(log.known_serial(self.tmp, self.manifest), "TESTSERIAL")

    def test_falls_back_to_the_newest_manifest_on_disk(self):
        """The case that matters: the device was already unreachable at startup, so the
        live identity read failed and the only source left is an earlier run's manifest."""
        self.write_archive(n_data=1)
        self.assertEqual(log.known_serial(self.tmp, {"device": {"error": "refused"}}),
                         "TESTSERIAL")

    def test_none_when_nothing_knows(self):
        self.assertIsNone(log.known_serial(self.tmp, None))

    def test_a_corrupt_manifest_is_skipped_rather_than_fatal(self):
        with open(os.path.join(self.tmp, "sigen-20260101-deadbeef.manifest.json"), "w") as fh:
            fh.write("{not json")
        self.write_archive(n_data=1)
        self.assertEqual(log.known_serial(self.tmp, None), "TESTSERIAL")


class TestParseIdentity(ArchiveFixture):
    def test_the_manifest_and_the_probe_read_the_same_offsets(self):
        """Two copies of these offsets could disagree about which host is ours."""
        unit, addr, count, fc = log.identity_block(self.cfg)
        ident = log.parse_identity(self.mb.read(unit, addr, count, fc))
        self.assertEqual(ident["serial"], self.manifest["device"]["serial"])
        self.assertEqual(ident["model"], self.manifest["device"]["model"])


class TestSerialProbe(ArchiveFixture):
    """log.serial_probe(), the half of rediscovery that talks to the device."""

    def setUp(self):
        super().setUp()
        self.built = []
        real = log.Modbus

        def fake(host, port, timeout):
            self.built.append((host, port, timeout))
            return self.mb
        log.Modbus = fake
        self.addCleanup(lambda: setattr(log, "Modbus", real))

    def test_it_returns_the_serial_and_a_latency(self):
        serial, ms = log.serial_probe(self.cfg)("10.0.0.15")
        self.assertEqual(serial, "TESTSERIAL")
        self.assertIsInstance(ms, float)

    def test_it_discards_the_first_read_and_times_the_rest(self):
        """The first read includes the TCP connect, and timing it inverted the ranking
        of the real unit's two interfaces. So it is read and thrown away."""
        self.mb.reads.clear()
        log.serial_probe(self.cfg)("10.0.0.15")
        self.assertEqual(len(self.mb.reads), 1 + log.PROBE_READS)

    def test_it_reads_only_the_identity_block_and_only_with_fc4(self):
        self.mb.reads.clear()
        log.serial_probe(self.cfg)("10.0.0.15")
        unit, addr, count, fc = log.identity_block(self.cfg)
        for got in self.mb.reads:
            self.assertEqual(got, (unit, addr, count, fc))
            self.assertEqual(got[3], 4, "FC4 is a read of input registers")

    def test_it_honours_the_port_from_config_and_caps_the_timeout(self):
        log.serial_probe(base_cfg(port=5020, timeout_s=30.0))("10.0.0.15")
        host, port, timeout = self.built[-1]
        self.assertEqual((host, port), ("10.0.0.15", 5020))
        self.assertLessEqual(timeout, 3.0, "a scan must not sit on one host for 30 s")

    def test_no_serial_means_no_latency_either(self):
        self.mb.set_string(30515, "", 10)
        self.assertEqual(log.serial_probe(self.cfg)("10.0.0.15"), (None, None))


class TestRediscoveryDuringAnOutage(ArchiveFixture):
    """capture() adopting a new address while the configured one is refusing.

    The 2026-09-04 failure end to end: the device is up somewhere else, the old address
    refuses, and the logger has to notice and move.
    """

    def setUp(self):
        super().setUp()
        real = log.Modbus
        log.Modbus = DeadModbus
        self.addCleanup(lambda: setattr(log, "Modbus", real))
        # The thresholds are wall-clock seconds; a 3-second test cannot wait 120.
        for name in ("REDISCOVER_AFTER_S", "REDISCOVER_EVERY_S"):
            old = getattr(log, name)
            setattr(log, name, 0)
            self.addCleanup(lambda n=name, o=old: setattr(log, n, o))
        # Never let a test reach the network or a notification service.
        real_find = discover.find
        self.addCleanup(lambda: setattr(discover, "find", real_find))
        real_notify = alerts.notify
        self.notifications = []
        alerts.notify = lambda *a, **kw: self.notifications.append(a) or True
        self.addCleanup(lambda: setattr(alerts, "notify", real_notify))
        self.write_archive(n_data=1)     # so a serial is discoverable on disk

    def run_capture(self, cfg, seconds=3):
        return self.capture_stdout(log.capture, cfg, self.tiers, self.tmp, seconds, 0,
                                   False, self.bundle, None, 0, True)

    def test_a_matching_host_is_adopted_and_announced(self):
        discover.find = lambda serial, probe, **kw: {
            "host": "10.0.0.15", "latency_ms": 16.0,
            "matches": [("10.0.0.15", 16.0), ("10.0.0.13", 112.0)],
            "listeners": ["10.0.0.13", "10.0.0.15"], "elapsed_s": 1.0}
        cfg = base_cfg(degrade_after=1, degrade_probe_s=1,
                       log_dir=self.tmp, install_dir=self.tmp)
        out = self.run_capture(cfg)
        self.assertIn("[rediscovered]", out)
        self.assertIn("10.0.0.15", out)
        self.assertIn("also on 10.0.0.13", out, "the slower interface should be named")
        self.assertEqual(discover.read_host_state(self.tmp), "10.0.0.15")
        self.assertTrue(self.notifications, "an address change must be pushed")

    def test_the_configured_host_is_left_alone_when_nothing_is_found(self):
        discover.find = lambda serial, probe, **kw: None
        cfg = base_cfg(degrade_after=1, degrade_probe_s=1,
                       log_dir=self.tmp, install_dir=self.tmp)
        out = self.run_capture(cfg)
        self.assertIn("[discover]", out)
        self.assertNotIn("[rediscovered]", out)
        self.assertEqual(discover.read_host_state(self.tmp), cfg["host"],
                         "a failed search must not rewrite the host in use")
        self.assertFalse(self.notifications)

    def test_finding_the_address_we_are_already_on_is_not_an_adoption(self):
        """Same address back means the device is refusing our reads, not that it moved.
        Reporting it as a move would send you looking for a network fault."""
        cfg = base_cfg(degrade_after=1, degrade_probe_s=1,
                       log_dir=self.tmp, install_dir=self.tmp)
        discover.find = lambda serial, probe, **kw: {
            "host": cfg["host"], "latency_ms": 20.0,
            "matches": [(cfg["host"], 20.0)], "listeners": [cfg["host"]],
            "elapsed_s": 1.0}
        out = self.run_capture(cfg)
        self.assertNotIn("[rediscovered]", out)
        self.assertIn("already the address being polled", out)
        self.assertFalse(self.notifications)

    def test_rediscovery_off_by_default_never_scans(self):
        def explode(*_a, **_kw):
            raise AssertionError("scanned the LAN without --discover")
        discover.find = explode
        cfg = base_cfg(degrade_after=1, degrade_probe_s=1, log_dir=self.tmp)
        out = self.capture_stdout(log.capture, cfg, self.tiers, self.tmp, 3, 0,
                                  False, self.bundle)
        self.assertIn("[degraded]", out)
        self.assertNotIn("[discover]", out)

    def test_an_adopted_address_is_not_counted_as_an_unexpected_reconnect(self):
        """Adopting closes the socket deliberately. Letting that inflate the counter
        whose job is to mean "something dropped this and we do not know what" would
        make every rediscovery look like a second, unexplained fault."""
        discover.find = lambda serial, probe, **kw: {
            "host": "10.0.0.15", "latency_ms": 16.0,
            "matches": [("10.0.0.15", 16.0)], "listeners": ["10.0.0.15"],
            "elapsed_s": 1.0}
        cfg = base_cfg(degrade_after=1, degrade_probe_s=1, bucket_s=2,
                       log_dir=self.tmp, install_dir=self.tmp)
        out = self.run_capture(cfg, seconds=6)
        self.assertIn("rediscoveries", out)
        self.assertNotIn("UNEXPECTED reconnects", out)


class TestWatchdogPlist(unittest.TestCase):
    """The properties of the watchdog daemon that no other test would notice losing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sigen-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._env = {k: v for k, v in os.environ.items() if k.startswith("SIGEN_")}
        for k in self._env:
            del os.environ[k]
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as fh:
            json.dump({"host": "h", "install_dir": "/opt/sigen",
                       "run_as_user": "someone"}, fh)
        os.environ["SIGEN_CONFIG"] = path
        self.addCleanup(self._restore_env)
        self.text = config.render(
            config.load(), os.path.join(REPO, "deploy/watchdog.plist.template"))

    def _restore_env(self):
        for k in [k for k in os.environ if k.startswith("SIGEN_")]:
            del os.environ[k]
        os.environ.update(self._env)

    def value_after(self, key):
        """The <string>/<integer> immediately following a <key>. plistlib would be
        tidier, but this asserts on the TEMPLATE's shape, which is what gets reviewed."""
        lines = [ln.strip() for ln in self.text.splitlines()]
        i = lines.index(f"<key>{key}</key>")
        for ln in lines[i + 1:]:
            if ln.startswith("<string>") or ln.startswith("<integer>"):
                return ln.split(">", 1)[1].rsplit("<", 1)[0]
        return None

    def test_every_token_is_substituted(self):
        # One leftover @TOKEN@ is a daemon that fails with a message about a file called
        # "@DATA_DIR@". render() refuses tokens it knows; this catches ones it does not.
        self.assertNotIn("@", self.text)

    def test_it_runs_as_a_user_and_not_as_root(self):
        # An empty <string></string> for UserName loads fine and runs as root.
        self.assertEqual(self.value_after("UserName"), "someone")

    def test_it_is_a_job_that_finishes_not_a_daemon(self):
        # KeepAlive on a check that exits is a crash loop that looks like health, which is
        # the shape of the bug this whole feature exists because of.
        self.assertNotIn("<key>KeepAlive</key>", self.text)
        self.assertEqual(self.value_after("StartInterval"), "900")

    def test_the_cadence_is_finer_than_the_staleness_threshold(self):
        """Otherwise the alert can arrive up to a whole threshold late."""
        args = self.text.split("<key>ProgramArguments</key>", 1)[1]
        threshold = int(args.split("--stale-after", 1)[1]
                        .split("<string>", 1)[1].split("<", 1)[0])
        self.assertLess(int(self.value_after("StartInterval")), threshold)

    def test_it_yields_to_the_logger(self):
        # The logger is the latency-sensitive process; Interactive here would let a disk
        # read compete with a two-second tick.
        self.assertEqual(self.value_after("ProcessType"), "Background")

    def test_it_logs_where_status_sh_looks(self):
        self.assertIn("/opt/sigen/logs/watchdog.log", self.text)
        self.assertIn("/opt/sigen/logs/watchdog.err", self.text)

    def test_it_runs_the_watchdog_and_not_something_else(self):
        self.assertIn("/opt/sigen/bin/watchdog.py", self.text)


class TestAlertConf(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sigen-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        alerts._warned.clear()

    def write(self, text):
        with open(os.path.join(self.tmp, alerts.CONF_NAME), "w") as fh:
            fh.write(text)

    def test_a_full_url_is_used_as_given(self):
        self.write("https://ntfy.example/topic\n")
        self.assertEqual(alerts.load_target(self.tmp), "https://ntfy.example/topic")

    def test_a_bare_topic_becomes_an_ntfy_url(self):
        self.write("my-topic\n")
        self.assertEqual(alerts.load_target(self.tmp), "https://ntfy.sh/my-topic")

    def test_comments_and_blank_lines_are_skipped(self):
        self.write("# the topic is a capability, keep it out of git\n\nmy-topic\n")
        self.assertEqual(alerts.load_target(self.tmp), "https://ntfy.sh/my-topic")

    def test_an_absent_file_disables_alerting_without_raising(self):
        said = []
        self.assertIsNone(alerts.load_target(self.tmp, log=said.append))
        self.assertTrue(any("notifications disabled" in s for s in said))

    def test_the_absence_is_said_once_not_every_run(self):
        said = []
        for _ in range(3):
            alerts.load_target(self.tmp, log=said.append)
        self.assertEqual(len(said), 1)

    def test_notify_without_a_conf_reports_failure_and_does_not_raise(self):
        self.assertFalse(alerts.notify("t", "b", install_dir=self.tmp))

    def test_notify_never_raises_when_the_service_is_unreachable(self):
        self.write("http://127.0.0.1:1/never-listening\n")
        said = []
        self.assertFalse(alerts.notify("t", "b", install_dir=self.tmp,
                                       log=said.append))
        self.assertTrue(any("failed" in s for s in said))


class TestWatchdog(ArchiveFixture):
    """The check that would have caught the 2026-09-04 outage."""

    def setUp(self):
        super().setUp()
        self.watchdog = load_watchdog()
        self.cfg = base_cfg(data_dir=self.tmp, log_dir=self.tmp, install_dir=self.tmp)

    def assess(self, stale_after_s=1800):
        return self.watchdog.assess(self.cfg, stale_after_s)

    def write_outage(self, data_age_s, probes=6):
        """The shape of a real outage: old data, but probe records arriving right now.

        Two files, because it cannot be done in one. write_archive() derives the empty
        records' timestamps from the same t0 as the data records, so a single old file
        is an archive that stopped being written to -- a stalled *logger*, which is a
        different diagnosis. The device case needs the logger to be demonstrably alive.
        """
        self.write_archive(n_data=3, stamp="20260814T090000",
                           t0=time.time() - data_age_s)
        # 30 s apart and ending now, which is under the 90 s stall threshold, so the
        # logger reads as healthy -- exactly as it did on 2026-09-04.
        self.write_archive(n_data=0, n_empty=probes, stamp="20260814T100000",
                           t0=time.time() - 30 * (probes - 1))

    def test_a_fresh_archive_is_not_an_alert(self):
        self.write_archive(n_data=5, t0=time.time() - 10)
        kind, summary, _ = self.assess()
        self.assertIsNone(kind, summary)

    def test_old_data_with_a_healthy_logger_is_a_device_problem(self):
        """The distinction the whole incident turned on: the logger was fine."""
        self.write_outage(data_age_s=4000)
        kind, summary, detail = self.assess(stale_after_s=1800)
        self.assertEqual(kind, "DEVICE")
        self.assertIn("no data for", summary)
        self.assertIn("the inverter is", detail)
        self.assertIn("not answering", detail)

    def test_data_inside_the_threshold_is_not_an_alert(self):
        self.write_outage(data_age_s=600)
        self.assertIsNone(self.assess(stale_after_s=1800)[0])

    def test_an_archive_of_nothing_but_probes_is_maximally_stale(self):
        """series.latest() scans a bounded number of files, so a long enough outage
        leaves no last-good sample to age at all. That is the 14-hour case."""
        self.write_archive(n_data=0, n_empty=6, t0=time.time() - 150)
        kind, _, detail = self.assess()
        self.assertEqual(kind, "DEVICE")
        self.assertIn("only empty probe records", detail)

    def test_no_archive_at_all_is_reported_distinctly(self):
        kind, _, _ = self.assess()
        self.assertEqual(kind, "ARCHIVE")

    def test_the_adopted_address_is_named_not_the_configured_one(self):
        """After rediscovery these differ, and naming the configured host would send
        you to the wrong machine."""
        self.write_outage(data_age_s=4000)
        discover.write_host_state(self.tmp, "10.0.0.15")
        self.assertIn("10.0.0.15", self.assess()[2])
        self.assertNotIn(self.cfg["host"], self.assess()[2])

    def test_a_stalled_logger_is_not_reported_as_a_device_fault(self):
        # No record of any kind for far longer than the stall threshold: the capture
        # process, not the inverter. Different cause, different fix, so different alert.
        self.write_archive(n_data=2, t0=time.time() - 90000)
        kind, _, detail = self.assess()
        self.assertEqual(kind, "LOGGER")
        self.assertIn("logger process", detail)


class TestWatchdogNotifiesOnTransitions(ArchiveFixture):
    def setUp(self):
        super().setUp()
        self.watchdog = load_watchdog()
        self.sent = []
        real = alerts.notify
        alerts.notify = lambda title, body, **kw: (self.sent.append(title) or True)
        self.addCleanup(lambda: setattr(alerts, "notify", real))
        self.cfg = base_cfg(data_dir=self.tmp, log_dir=self.tmp, install_dir=self.tmp)
        real_load = config.load
        config.load = lambda **kw: self.cfg
        self.addCleanup(lambda: setattr(config, "load", real_load))

    def write_outage(self, data_age_s=4000, probes=6):
        """Old data with probes still arriving. See TestWatchdog.write_outage."""
        self.write_archive(n_data=3, stamp="20260814T090000",
                           t0=time.time() - data_age_s)
        self.write_archive(n_data=0, n_empty=probes, stamp="20260814T100000",
                           t0=time.time() - 30 * (probes - 1))

    def run_once(self, argv=()):
        old = sys.argv
        sys.argv = ["watchdog.py"] + list(argv)
        self.rc = None
        try:
            buf, err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                self.rc = self.watchdog.main()
            return buf.getvalue()
        finally:
            sys.argv = old

    def test_a_healthy_first_run_says_nothing(self):
        """A watchdog that pushes on every run trains you to ignore it."""
        self.write_archive(n_data=5, t0=time.time() - 10)
        out = self.run_once()
        self.assertIn("OK", out)
        self.assertEqual(self.sent, [])

    def test_going_stale_notifies_once_not_every_run(self):
        self.write_outage()
        self.run_once()
        self.assertEqual(len(self.sent), 1)
        self.run_once()
        self.assertEqual(len(self.sent), 1, "a sustained outage must not re-nag hourly")

    def test_recovery_notifies_too(self):
        self.write_outage()
        self.run_once()
        self.assertEqual(len(self.sent), 1)
        for name in os.listdir(self.tmp):
            if name.endswith(".bin") or name.endswith(".bin.gz"):
                os.unlink(os.path.join(self.tmp, name))
        self.write_archive(n_data=5, stamp="20260814T120000", t0=time.time() - 10)
        self.run_once()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("recovered", self.sent[1])

    def test_dry_run_never_sends(self):
        self.write_outage()
        out = self.run_once(["--dry-run"])
        self.assertIn("would have notified", out)
        self.assertEqual(self.sent, [])

    def test_a_failed_notification_is_retried_next_run(self):
        """Treating an undelivered alert as delivered is how an outage stays silent."""
        alerts.notify = lambda title, body, **kw: False
        self.write_outage()
        out = self.run_once()
        self.assertIn("NOT notified", out)
        state = self.load_json(os.path.join(self.tmp, self.watchdog.STATE_NAME))
        self.assertFalse(state["last_notified"])

    def test_it_exits_zero_even_when_stale(self):
        """A non-zero exit would make launchd log a failed job on top of the alert."""
        self.write_outage()
        self.run_once()
        self.assertEqual(self.rc, 0)


if __name__ == "__main__":
    unittest.main()
