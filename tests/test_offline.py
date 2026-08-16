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
import io
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config          # noqa: E402
import decode          # noqa: E402
import dump            # noqa: E402
import lib             # noqa: E402
import log             # noqa: E402

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

    def test_manifest_is_written_once_per_day(self):
        a1 = log.Archive(self.tmp, 0, self.manifest)
        first = os.path.getmtime(a1.manifest_path)
        a2 = log.Archive(self.tmp, 0, self.manifest)
        self.assertEqual(a1.manifest_path, a2.manifest_path)
        self.assertEqual(first, os.path.getmtime(a2.manifest_path))


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


if __name__ == "__main__":
    unittest.main()
