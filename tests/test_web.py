#!/usr/bin/env python3
"""
Offline tests for the viewer: series.py and serve.py. NO HARDWARE, NO NETWORK.

    python3 -m unittest discover -s tests -v

Built on tests/test_offline.py's ArchiveFixture, so the archive under test is the
same synthetic one the capture/decode tests use.

What these are for. The viewer's job is to answer "what was the house doing?" from
bytes on disk, and every way it can be wrong is quiet:

  - an aggregate that drops the records straddling a file boundary reads as a
    dip in the data rather than as a bug;
  - a gap drawn as a joined line reads as a smooth transition;
  - "device not answering" reported as latency reads as a slow inverter
    (FINDINGS 7);
  - and a viewer that grew a Modbus client would become a second poller of a
    device whose concurrent-client behaviour is unmeasured.

So each of those has a test rather than a comment.
"""
import ast
import gzip
import io
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest
from http.client import HTTPConnection

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config          # noqa: E402
import decode          # noqa: E402
import lib             # noqa: E402
import log             # noqa: E402
import series          # noqa: E402
import serve           # noqa: E402
from test_offline import ArchiveFixture, base_cfg   # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ViewerFixture(ArchiveFixture):
    """An archive with per-record control of values, masks and timestamps."""

    def setUp(self):
        super().setUp()
        series._CATALOG.clear()        # memoised per plan hash; tests reuse hashes
        self.cache = series.SummaryCache()

    # -- register image ----------------------------------------------------

    def set_scaled(self, key, value):
        """Set a field's registers so it decodes to `value`."""
        f = series._fields_by_key()[key]
        raw = int(round(value * (f["gain"] or 1)))
        self.set_raw(key, raw)

    def set_raw(self, key, raw):
        f = series._fields_by_key()[key]
        width = lib.WIDTH[lib.dtype_of(f)]
        packed = struct.pack(lib.FMT[lib.dtype_of(f)].replace(">", ">"), raw)
        packed = packed.rjust(width * 2, b"\x00")
        for i in range(width):
            self.mb.regs[f["addr"] + i] = struct.unpack(
                ">H", packed[i * 2:i * 2 + 2])[0]

    def block_payload(self, indices=None):
        out = []
        for i, (_, u, a, c, fc, _, _) in enumerate(self.tiers):
            if indices is not None and i not in indices:
                continue
            out.append(self.mb.read(u, a, c, fc))
        return b"".join(out)

    def full_mask(self):
        return (1 << len(self.tiers)) - 1

    # -- files -------------------------------------------------------------

    def write_records(self, stamp, recs, gzip_it=False, plan_hash=None,
                      manifest_day="20260814"):
        """recs: [(ts, mask, latency_ms, payload_bytes)]. Returns the path."""
        ph = plan_hash or self.manifest["plan_hash"]
        blob = io.BytesIO()
        for ts, mask, latency, payload in recs:
            blob.write(struct.pack(log.HEADER, ts, mask, latency))
            blob.write(payload)
        path = os.path.join(self.tmp, f"sigen-{stamp}-{ph}.bin")
        if gzip_it:
            path += ".gz"
            with gzip.open(path, "wb") as fh:
                fh.write(blob.getvalue())
        else:
            with open(path, "wb") as fh:
                fh.write(blob.getvalue())
        man = dict(self.manifest, plan_hash=ph)
        with open(os.path.join(self.tmp,
                              f"sigen-{manifest_day}-{ph}.manifest.json"), "w") as fh:
            json.dump(man, fh)
        return path

    def data_records(self, t0, n, step=2, soc_from=1.0, soc_step=1.0):
        """n full-mask records, with plant_ess_soc walking so min/max/mean differ."""
        out = []
        for i in range(n):
            self.set_scaled("plant_ess_soc", soc_from + i * soc_step)
            out.append((t0 + i * step, self.full_mask(), 100 + i, self.block_payload()))
        return out

    def series_of(self, data_dir=None):
        s = series.newest_series(data_dir or self.tmp)
        self.assertIsNotNone(s, "no series discovered")
        return s


# ------------------------------------------------------------------- read-only

class TestViewerIsReadOnly(unittest.TestCase):
    """The safety claim for the new code, asserted rather than documented.

    Parsed with ast rather than grepped, so the docstrings that *describe* the
    guarantee ("lib.Modbus is never constructed") cannot satisfy or break it.
    """

    def used_names(self, name):
        with open(os.path.join(HERE, name)) as fh:
            tree = ast.parse(fh.read(), filename=name)
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                out.add(node.attr)
            elif isinstance(node, ast.Name):
                out.add(node.id)
        return out

    def test_viewer_never_constructs_a_modbus_client(self):
        for name in ("series.py", "serve.py"):
            used = self.used_names(name)
            self.assertNotIn("Modbus", used,
                             f"{name} would become a second client of the inverter")
            self.assertNotIn("create_connection", used)
            self.assertNotIn("recv_exact", used)

    def test_viewer_modules_do_not_import_the_transport(self):
        # lib is imported for decode primitives; importing socket in serve.py is
        # only gethostname. What must never appear is a read of the device.
        for name in ("series.py", "serve.py"):
            used = self.used_names(name)
            self.assertNotIn("sweep", used)        # dump.sweep polls every register
            self.assertNotIn("identity_block", used)

    def test_no_write_function_codes(self):
        for name in ("series.py", "serve.py"):
            with open(os.path.join(HERE, name)) as fh:
                src = fh.read()
            for fc in ("5", "6", "15", "16"):
                self.assertNotIn(f"fc={fc}", src)


# ---------------------------------------------------------------------- index

class TestIndex(ViewerFixture):
    def test_first_ts_comes_from_the_record_not_the_filename(self):
        # The stamp in the name is host-local wall clock; parsing it back would
        # guess at a DST fold. The header is authoritative.
        t0 = 1786600000.0
        self.write_records("19990101T000000", self.data_records(t0, 3))
        s = self.series_of()
        self.assertAlmostEqual(s.spans()[0].first_ts, t0, places=3)

    def test_spans_tile_the_archive_and_the_newest_is_open(self):
        a, b = 1786600000.0, 1786603600.0
        self.write_records("20260814T090000", self.data_records(a, 4))
        self.write_records("20260814T100000", self.data_records(b, 4))
        spans = self.series_of().spans()
        self.assertEqual(len(spans), 2)
        self.assertAlmostEqual(spans[0].last_ts, b, places=3)
        self.assertIsNone(spans[1].last_ts, "the newest file is still being written")

    def test_two_plans_are_two_series_and_never_mixed(self):
        t0 = 1786600000.0
        self.write_records("20260814T090000", self.data_records(t0, 4))
        self.write_records("20260814T100000", self.data_records(t0 + 3600, 4),
                           plan_hash="deadbeef")
        found = series.discover(self.tmp)
        self.assertEqual(sorted(found), ["08c047b8", "deadbeef"])
        for ph, s in found.items():
            for span in s.spans():
                self.assertIn(ph, span.name)
        # newest_series picks the one holding the newest record, and only that one.
        self.assertEqual(self.series_of().plan_hash, "deadbeef")

    def test_files_without_a_manifest_for_their_plan_are_not_offered(self):
        t0 = 1786600000.0
        self.write_records("20260814T090000", self.data_records(t0, 3))
        # A file whose manifest is missing: the format of record is unknown, so it
        # cannot be decoded at all -- better absent than guessed at.
        orphan = os.path.join(self.tmp, "sigen-20260814T110000-facefeed.bin")
        with open(orphan, "wb") as fh:
            fh.write(struct.pack(log.HEADER, t0 + 7200, 0, 10))
        self.assertEqual(sorted(series.discover(self.tmp)), ["08c047b8"])

    def test_an_empty_open_file_is_skipped_not_fatal(self):
        # Exactly what exists for a moment after rotation.
        t0 = 1786600000.0
        self.write_records("20260814T090000", self.data_records(t0, 3))
        open(os.path.join(self.tmp, "sigen-20260814T100000-08c047b8.bin"), "wb").close()
        self.assertEqual(len(self.series_of().spans()), 1)

    def test_index_refreshes_when_the_open_file_grows(self):
        t0 = 1786600000.0
        path = self.write_records("20260814T090000", self.data_records(t0, 2))
        s = self.series_of()
        self.assertEqual(len(s.spans()), 1)
        with open(path, "ab") as fh:
            for ts, mask, lat, payload in self.data_records(t0 + 100, 2):
                fh.write(struct.pack(log.HEADER, ts, mask, lat) + payload)
        w = series.window(s, t0 - 10, t0 + 200, ["plant_ess_soc"], cache=self.cache)
        self.assertEqual(w["records"], 4)


# -------------------------------------------------------------- bucket maths

class TestBuckets(ViewerFixture):
    def test_common_windows_land_on_round_bucket_widths(self):
        self.assertEqual(series.choose_bucket(6 * 3600, 2), 30)
        self.assertEqual(series.choose_bucket(24 * 3600, 2), 120)
        self.assertEqual(series.choose_bucket(7 * 86400, 2), 900)
        self.assertIn(series.choose_bucket(900, 2), series.BUCKET_LADDER)

    def test_bucket_width_never_goes_below_the_cadence(self):
        self.assertGreaterEqual(series.choose_bucket(60, 2), 2)

    def test_buckets_are_aligned_to_absolute_epoch(self):
        t0 = 1786600000.0
        self.write_records("20260814T090000", self.data_records(t0, 30))
        w = series.window(self.series_of(), t0 + 7, t0 + 55, ["plant_ess_soc"],
                          bucket_s=30, cache=self.cache)
        for t in w["t"]:
            self.assertEqual(t % 30, 0, "a bucket must not start at the window edge")

    def test_a_bucket_split_across_two_files_aggregates_exactly(self):
        # The reason bucket keys are absolute. Eight records inside ONE bucket,
        # four in each of two files, must give the same numbers as all eight in one.
        base = (1786600000 // 300) * 300 + 10.0
        split_recs = self.data_records(base, 8, step=2)
        self.write_records("20260814T090000", split_recs[:4])
        self.write_records("20260814T090010", split_recs[4:])
        split = series.window(self.series_of(), base - 300, base + 300,
                              ["plant_ess_soc"], bucket_s=300,
                              cache=series.SummaryCache())["series"]["plant_ess_soc"]

        self.tearDown(); self.setUp()
        whole = self.data_records(base, 8, step=2)
        self.write_records("20260814T090000", whole)
        one = series.window(self.series_of(), base - 300, base + 300,
                            ["plant_ess_soc"], bucket_s=300,
                            cache=series.SummaryCache())["series"]["plant_ess_soc"]
        self.assertEqual(split["n"], one["n"])
        self.assertEqual(split["min"], one["min"])
        self.assertEqual(split["max"], one["max"])
        self.assertEqual(split["mean"], one["mean"])
        self.assertEqual(sum(split["n"]), 8)

    def test_min_max_and_mean_are_the_bucket_extremes(self):
        base = (1786600000 // 60) * 60 + 0.0
        self.write_records("20260814T090000",
                           self.data_records(base, 10, step=2, soc_from=1.0,
                                             soc_step=1.0))
        w = series.window(self.series_of(), base, base + 60, ["plant_ess_soc"],
                          bucket_s=60, cache=self.cache)
        col = w["series"]["plant_ess_soc"]
        self.assertEqual(col["min"][0], 1.0)
        self.assertEqual(col["max"][0], 10.0)
        self.assertAlmostEqual(col["mean"][0], 5.5)
        self.assertEqual(col["n"][0], 10)

    def test_an_empty_bucket_is_null_not_zero(self):
        # Zero is a legal reading. A hole must not become one.
        base = (1786600000 // 60) * 60 + 0.0
        self.write_records("20260814T090000", self.data_records(base, 3, step=2))
        self.write_records("20260814T091000",
                           self.data_records(base + 600, 3, step=2))
        w = series.window(self.series_of(), base, base + 660, ["plant_ess_soc"],
                          bucket_s=60, cache=self.cache)
        col = w["series"]["plant_ess_soc"]
        self.assertIsNotNone(col["mean"][0])
        self.assertIsNone(col["mean"][5], "no records in that bucket")
        self.assertEqual(col["n"][5], 0)

    def test_a_sentinel_never_becomes_a_value(self):
        base = 1786600000.0
        self.set_raw("plant_ess_soc", 0xFFFF)          # all-ones "not available"
        recs = [(base, self.full_mask(), 90, self.block_payload())]
        self.write_records("20260814T090000", recs)
        w = series.window(self.series_of(), base - 10, base + 10, ["plant_ess_soc"],
                          bucket_s=10, cache=self.cache)
        self.assertTrue(all(v is None for v in w["series"]["plant_ess_soc"]["mean"]),
                        "0xFFFF is a sentinel, not 6553.5%")

    def test_a_signed_minus_one_is_kept_because_it_is_a_legal_reading(self):
        # The two "nothing here" encodings are different (FINDINGS 6): unsigned
        # all-ones is suppressed, signed -1 is kept, because -1 is legal for plenty
        # of signed fields. A viewer that silently dropped it would hide real data.
        base = 1786600000.0
        self.set_raw("inverter_pv9_current", -1)
        self.write_records("20260814T090000",
                           [(base, self.full_mask(), 90, self.block_payload())])
        w = series.window(self.series_of(), base - 10, base + 10,
                          ["inverter_pv9_current"], bucket_s=10, cache=self.cache)
        got = [v for v in w["series"]["inverter_pv9_current"]["min"] if v is not None]
        self.assertEqual(got, [-0.01], "raw -1 at gain 100")

    def test_stride_is_bounded_by_bucket_width_and_always_reported(self):
        self.assertEqual(series.stride_for(30, 2), 1)      # the 6 h default: nothing skipped
        self.assertEqual(series.stride_for(120, 2), 1)     # 24 h: still nothing
        self.assertEqual(series.stride_for(900, 2), 8)     # 7 d
        self.assertEqual(series.stride_for(3600, 2), 29)
        # Never more than SAMPLES_PER_BUCKET decoded per bucket, whatever the span.
        for bucket in series.BUCKET_LADDER:
            per = (bucket // 2) // series.stride_for(bucket, 2)
            self.assertLessEqual(per, series.SAMPLES_PER_BUCKET,
                                 f"{bucket}s buckets decode {per} records each")
        base = 1786600000.0
        self.write_records("20260814T090000", self.data_records(base, 4))
        w = series.window(self.series_of(), base - 10, base + 3600,
                          ["plant_ess_soc"], bucket_s=3600, cache=self.cache)
        self.assertEqual(w["stride"], 29)

    def test_slow_tier_records_are_never_strided_away(self):
        # inv_battery fires once a minute. Index-based striding could alias it out
        # entirely, and the field would read as absent rather than as sparse.
        base = 1786600000.0
        recs, ts = [], base
        for i in range(120):
            recs.append((ts, 0b111, 100, self.block_payload({0, 1, 2})))
            ts += 2
            if i % 30 == 29:                          # a slow-tier record
                recs.append((ts, 1 << 3, 100, self.block_payload({3})))
                ts += 2
        self.write_records("20260814T090000", recs)
        w = series.window(self.series_of(), base, base + 3600,
                          ["inverter_ess_average_cell_voltage"], bucket_s=3600,
                          cache=self.cache)
        self.assertGreater(w["stride"], 1)
        self.assertEqual(w["series"]["inverter_ess_average_cell_voltage"]["n"][0], 4,
                         "every slow-tier record must survive striding")


# --------------------------------------------------------------------- health

class TestHealth(ViewerFixture):
    def test_device_not_answering_is_not_the_same_as_no_records(self):
        base = (1786600000 // 60) * 60 + 0.0
        recs = self.data_records(base, 10, step=2)
        # A bucket of nothing but outage probes: records written, no blocks in them.
        recs += [(base + 60 + i * 30, 0, 6500, b"") for i in range(2)]
        recs += self.data_records(base + 180, 10, step=2)
        self.write_records("20260814T090000", recs)
        w = series.window(self.series_of(), base, base + 240, ["plant_ess_soc"],
                          bucket_s=60, cache=self.cache)
        h = w["health"]
        self.assertEqual(len(h["no_data"]), 1, "the probe-only bucket")
        self.assertEqual(h["no_data"][0][0], base + 60)
        self.assertEqual(len(h["no_records"]), 1, "the bucket with nothing at all")
        self.assertEqual(h["no_records"][0][0], base + 120)

    def test_outage_probe_latency_never_enters_the_medians(self):
        # 6500 ms is a socket timeout. Averaged in, a dead device reads as a
        # catastrophically slow one (FINDINGS 7).
        base = (1786600000 // 60) * 60 + 0.0
        recs = [(base + i * 2, self.full_mask(), 100 + i, self.block_payload())
                for i in range(5)]
        recs += [(base + 20 + i * 2, 0, 6500, b"") for i in range(5)]
        self.write_records("20260814T090000", recs)
        w = series.window(self.series_of(), base, base + 60, ["plant_ess_soc"],
                          bucket_s=60, cache=self.cache)
        h = w["health"]
        self.assertLess(h["latency_max"][0], 1000)
        self.assertLess(h["latency_median"][0], 200)
        self.assertEqual(h["empty"][0], 5)
        self.assertEqual(h["records"][0], 10)

    def test_nothing_is_flagged_outside_the_archive(self):
        # Before the first file there is no archive, which is not an outage.
        base = 1786600000.0
        self.write_records("20260814T090000", self.data_records(base, 10))
        w = series.window(self.series_of(), base - 7200, base + 20,
                          ["plant_ess_soc"], bucket_s=60, cache=self.cache)
        for a, b in w["health"]["no_records"]:
            self.assertGreaterEqual(a, base, "silence before the archive is not a gap")

    def test_latency_max_is_exact_while_the_quantiles_are_sampled(self):
        base = (1786600000 // 3600) * 3600 + 0.0
        recs = []
        for i in range(200):
            recs.append((base + i * 2, self.full_mask(), 100, self.block_payload()))
        recs[150] = (base + 300.0, self.full_mask(), 1147, self.block_payload())
        self.write_records("20260814T090000", recs)
        w = series.window(self.series_of(), base, base + 3600, ["plant_ess_soc"],
                          bucket_s=3600, cache=self.cache)
        self.assertEqual(w["health"]["latency_max"][0], 1147,
                         "the outlier tick is the one worth seeing")


# --------------------------------------------------------------------- energy

class TestEnergy(ViewerFixture):
    KEY = "plant_accumulated_grid_import_energy"

    def test_window_total_is_the_counter_difference(self):
        base = 1786600000.0
        recs = []
        for i, kwh in enumerate([100.0, 100.5, 101.25, 102.0]):
            self.set_scaled(self.KEY, kwh)
            recs.append((base + i * 2, self.full_mask(), 100, self.block_payload()))
        self.write_records("20260814T090000", recs)
        w = series.window(self.series_of(), base - 5, base + 10, [self.KEY],
                          bucket_s=10, cache=self.cache)
        e = series.energy(w, [self.KEY])
        self.assertAlmostEqual(e[self.KEY]["kwh"], 2.0, places=3)
        self.assertFalse(e[self.KEY]["reset"])

    def test_a_counter_that_steps_back_is_flagged_not_negative(self):
        # The device loses an unflushed increment across a power cut (FINDINGS 11).
        base = 1786600000.0
        recs = []
        for i, kwh in enumerate([100.0, 100.5, 99.8, 99.9]):
            self.set_scaled(self.KEY, kwh)
            recs.append((base + i * 20, self.full_mask(), 100, self.block_payload()))
        self.write_records("20260814T090000", recs)
        w = series.window(self.series_of(), base - 5, base + 80, [self.KEY],
                          bucket_s=10, cache=self.cache)
        e = series.energy(w, [self.KEY])
        self.assertTrue(e[self.KEY]["reset"])
        self.assertGreaterEqual(e[self.KEY]["kwh"], 0.0)


# --------------------------------------------------------------------- latest

class TestLatest(ViewerFixture):
    def test_data_freshness_is_reported_separately_from_record_freshness(self):
        now = time.time()
        recs = self.data_records(now - 300, 5, step=2)
        recs += [(now - 60 + i * 20, 0, 6500, b"") for i in range(3)]
        self.write_records("20260814T090000", recs)
        got = series.latest(self.series_of(), ["plant_ess_soc"])
        self.assertTrue(got["ok"], "there is a last known good sample")
        self.assertFalse(got["device_answering"])
        self.assertGreater(got["data_age_s"], 200)
        self.assertLess(got["record_age_s"], 100)
        self.assertEqual(got["empties_since"], 3)
        self.assertIsNotNone(got["values"]["plant_ess_soc"])

    def test_last_known_good_is_found_in_an_older_file(self):
        # The current file can hold nothing but probes during an outage; that is
        # exactly why bin/latest.sh passes several files too.
        now = time.time()
        self.write_records("20260814T080000", self.data_records(now - 4000, 5))
        self.write_records("20260814T090000",
                           [(now - 300 + i * 30, 0, 6500, b"") for i in range(4)])
        got = series.latest(self.series_of(), ["plant_ess_soc"])
        self.assertTrue(got["ok"])
        self.assertFalse(got["device_answering"])
        self.assertEqual(got["empties_since"], 4)
        self.assertGreater(got["data_age_s"], 3000)

    def test_a_healthy_tail_reads_as_current(self):
        now = time.time()
        self.write_records("20260814T090000", self.data_records(now - 6, 3))
        got = series.latest(self.series_of(), ["plant_ess_soc"])
        self.assertTrue(got["device_answering"])
        self.assertFalse(got["logger_stalled"])
        self.assertLess(got["data_age_s"], 30)

    def test_a_stalled_logger_is_distinguished_from_a_silent_device(self):
        now = time.time()
        self.write_records("20260814T090000", self.data_records(now - 3600, 3))
        got = series.latest(self.series_of(), ["plant_ess_soc"])
        self.assertTrue(got["device_answering"], "its last record did carry data")
        self.assertTrue(got["logger_stalled"], "but nothing has been written since")


# -------------------------------------------------------------------- catalog

class TestCatalog(ViewerFixture):
    def test_catalog_covers_the_captured_blocks_and_marks_duplicates(self):
        self.write_records("20260814T090000", self.data_records(1786600000.0, 3))
        cat = series.catalog(self.series_of())
        keys = {c["key"] for c in cat}
        self.assertIn("plant_ess_soc", keys)
        self.assertIn("inverter_pv1_voltage", keys)
        self.assertNotIn("inverter_model_type", keys, "not covered by any tier block")
        dupes = {c["key"]: c["duplicate_of"] for c in cat if "duplicate_of" in c}
        self.assertEqual(dupes.get("inverter_ess_battery_soc"), "plant_ess_soc")
        # A field is only marked as a copy when the register it copies is itself
        # captured -- otherwise the picker would hide the only copy there is.
        for key, prefer in dupes.items():
            self.assertIn(prefer, keys,
                          f"{key} points at a field that is not offered")
        self.assertNotIn("duplicate_of",
                         next(c for c in cat
                              if c["key"] ==
                              "plant_total_generation_of_third_party_inverter_2"),
                         "30196 is outside every block, so the _2 twin is the "
                         "only copy this archive has")

    def test_state_registers_carry_their_enum_labels(self):
        self.write_records("20260814T090000", self.data_records(1786600000.0, 3))
        cat = {c["key"]: c for c in series.catalog(self.series_of())}
        self.assertEqual(cat["plant_running_state"]["enum"]["1"], "RUNNING")
        self.assertEqual(cat["plant_on_off_grid_status"]["enum"]["0"], "ongrid")
        self.assertEqual(cat["plant_general_alarm1"]["alarm_table"], "PCS_ALARM_CODES")

    def test_alarm_bits_are_named(self):
        self.assertEqual(series.alarm_bits("plant_general_alarm1", 0), [])
        self.assertTrue(series.alarm_bits("plant_general_alarm1", 1))


class TestPanelContract(unittest.TestCase):
    """Every field a panel names must exist and be captured.

    A typo here would render an empty chart with no error anywhere -- the panel
    would simply have nothing to draw and look like a quiet night.
    """

    def setUp(self):
        cfg = base_cfg()
        self.tiers = log.build_tiers(cfg)
        import dump
        self.bundle = dump.load_regmap()
        self.manifest = {"blocks": [
            {"index": i, "label": t[0], "addr": t[2], "count": t[3], "period_s": t[5]}
            for i, t in enumerate(self.tiers)]}
        self.covered = set(decode.covered_fields(self.manifest, self.bundle))

    def test_every_panel_field_is_covered_by_the_default_block_plan(self):
        for key in serve.panel_keys():
            self.assertIn(key, self.covered, f"panel field {key} is never captured")

    def test_every_live_field_is_covered(self):
        for key in serve.LIVE_FIELDS:
            self.assertIn(key, self.covered, f"live tile field {key} is never captured")

    def test_panels_do_not_plot_a_register_twice_under_two_names(self):
        for key in serve.panel_keys():
            self.assertNotIn(key, series.DUPLICATE_OF,
                             f"{key} is the same register as "
                             f"{series.DUPLICATE_OF.get(key)}")

    def test_panel_slots_stay_inside_the_categorical_theme(self):
        # A ninth series would need a generated hue, which is indistinguishable
        # under colour-vision deficiency. Cap it here instead.
        for p in serve.PANELS:
            for s in p.get("series", []):
                self.assertLessEqual(s.get("slot", 1), 4)
            self.assertLessEqual(len(p.get("series", [])), 4, p["id"])

    def test_energy_tiles_are_lifetime_counters(self):
        for t in serve.ENERGY_TILES:
            self.assertIn("accumulated", t["key"])


# ----------------------------------------------------------------------- HTTP

class TestHTTP(ViewerFixture):
    def start(self, **over):
        cfg = dict(config.DEFAULTS)
        cfg.update({"host": None, "data_dir": self.tmp, "web_default_hours": 6})
        cfg.update(over)
        viewer = serve.Viewer(cfg, self.tmp)
        # The handler logs requests to stdout, which is the viewer's log file in
        # production and noise in a test run.
        real_stdout = sys.stdout
        sys.stdout = io.StringIO()
        self.addCleanup(lambda: setattr(sys, "stdout", real_stdout))
        self.httpd = serve.Server(("127.0.0.1", 0), viewer)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        return viewer

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def get(self, path):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            # putrequest, not request(): the raw path must reach the server
            # unnormalised so a traversal attempt is actually tested.
            conn.putrequest("GET", path, skip_accept_encoding=True)
            conn.endheaders()
            r = conn.getresponse()
            return r.status, r.getheader("Content-Type"), r.read()
        finally:
            conn.close()

    def json(self, path):
        status, ctype, body = self.get(path)
        self.assertEqual(status, 200, body[:400])
        self.assertIn("application/json", ctype)
        return json.loads(body)

    def seed(self, n=200):
        now = time.time()
        # FakeModbus returns each register as its own address, so a state register
        # would decode to a nonsense code. Set the ones the page names.
        self.set_raw("plant_running_state", 1)          # RUNNING
        self.set_raw("plant_on_off_grid_status", 0)     # ongrid
        self.set_raw("plant_ems_work_mode", 0)
        for key in ("plant_general_alarm1", "plant_general_alarm2",
                    "plant_general_alarm3", "plant_general_alarm4",
                    "plant_general_alarm6", "plant_general_alarm7"):
            self.set_raw(key, 0)
        self.write_records("20260814T090000",
                           self.data_records(now - n * 2, n, step=2))
        return now

    def test_the_page_and_its_assets_are_served(self):
        self.seed()
        self.start()
        for path, ctype in [("/", "text/html"), ("/app.js", "text/javascript"),
                            ("/charts.js", "text/javascript"),
                            ("/style.css", "text/css"), ("/favicon.svg", "image/svg")]:
            status, got, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(ctype, got, path)
            self.assertTrue(body)

    def test_meta_describes_the_series_panels_and_catalog(self):
        self.seed()
        self.start()
        m = self.json("/api/meta")
        self.assertTrue(m["ok"])
        self.assertEqual(m["plan_hash"], "08c047b8")
        self.assertGreater(len(m["catalog"]), 200)
        self.assertTrue(any(p["id"] == "power" for p in m["panels"]))
        self.assertEqual(m["default_hours"], 6)
        self.assertIn("first_ts", m["extent"])

    def test_identity_is_withheld_unless_asked_for(self):
        # A manifest identifies an installation, and this page is on the LAN.
        self.seed()
        self.start()
        m = self.json("/api/meta")
        self.assertNotIn("serial", m["device"])
        self.assertNotIn("host", m)
        self.assertIn("withheld", m["device"])
        self.stop()
        self.start(web_show_identity=True)
        m = self.json("/api/meta")
        self.assertEqual(m["device"]["serial"], "TESTSERIAL")
        self.assertEqual(m["host"], "203.0.113.1")

    def test_window_returns_a_grid_series_and_health(self):
        self.seed()
        self.start()
        w = self.json("/api/window?hours=1")
        self.assertTrue(w["ok"])
        self.assertIn("plant_ess_soc", w["series"])
        self.assertEqual(len(w["t"]), len(w["series"]["plant_ess_soc"]["mean"]))
        self.assertEqual(len(w["t"]), len(w["health"]["records"]))
        self.assertGreater(w["records"], 0)
        self.assertIn(w["bucket_s"], series.BUCKET_LADDER)
        self.assertEqual(w["stride"], 1)

    def test_window_scopes_to_the_panels_asked_for(self):
        self.seed()
        self.start()
        few = self.json("/api/window?hours=1&panels=soc")
        self.assertIn("plant_ess_soc", few["series"])
        self.assertNotIn("inverter_pv1_current", few["series"])

    def test_a_lifetime_counter_ships_as_a_total_not_a_line(self):
        self.seed()
        self.start()
        w = self.json("/api/window?hours=1&panels=energy")
        col = w["series"]["plant_accumulated_grid_import_energy"]
        self.assertTrue(col.get("tile_only"))
        self.assertNotIn("mean", col)
        self.assertIn("plant_accumulated_grid_import_energy", w["energy"])

    def test_an_unknown_field_is_reported_not_fatal(self):
        self.seed()
        self.start()
        w = self.json("/api/window?hours=1&panels=soc&fields=not_a_field,plant_ess_soh")
        self.assertEqual(w["unknown_fields"], ["not_a_field"])
        self.assertIn("plant_ess_soh", w["series"])

    def test_bad_parameters_are_a_400_with_a_readable_reason(self):
        self.seed()
        self.start()
        status, _, body = self.get("/api/window?hours=abc")
        self.assertEqual(status, 400)
        self.assertIn("not a number", json.loads(body)["error"])

    def test_latest_reports_values_labels_and_freshness(self):
        self.seed()
        self.start()
        l = self.json("/api/latest")
        self.assertTrue(l["ok"])
        self.assertIn("plant_ess_soc", l["values"])
        self.assertEqual(l["labels"]["plant_running_state"], "RUNNING")
        self.assertTrue(l["device_answering"])

    def test_csv_matches_the_window_and_raw_matches_decode_py(self):
        self.seed()
        self.start()
        status, ctype, body = self.get("/api/csv?hours=1&panels=soc")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", ctype)
        lines = body.decode().strip().splitlines()
        self.assertTrue(lines[0].startswith("# sigen viewer"))
        self.assertIn("plant_ess_soc_mean", lines[1])
        status, _, raw = self.get("/api/csv?hours=1&raw=1&fields=plant_ess_soc")
        self.assertEqual(status, 200)
        self.assertEqual(raw.decode().splitlines()[0],
                         "host_time,latency_ms,plant_ess_soc")

    def test_path_traversal_and_unknown_paths_are_refused(self):
        self.seed()
        self.start()
        for path in ("/../config.json", "/../../etc/passwd", "/config.json",
                     "/web/app.js", "/api/nope", "/%2e%2e/config.json"):
            status, _, _ = self.get(path)
            self.assertEqual(status, 404, path)

    def test_an_empty_data_dir_explains_itself_rather_than_erroring(self):
        self.start()
        m = self.json("/api/meta")
        self.assertFalse(m["ok"])
        self.assertIn("manifest", m["reason"])
        status, _, body = self.get("/api/window?hours=6")
        self.assertEqual(status, 503)

    def test_a_torn_final_record_still_serves_a_window(self):
        now = self.seed()
        path = os.path.join(self.tmp, "sigen-20260814T090000-08c047b8.bin")
        with open(path, "ab") as fh:
            fh.write(b"\x01\x02\x03")        # a record torn by a hard kill
        self.start()
        w = self.json("/api/window?hours=1")
        self.assertGreater(w["records"], 0)

    def test_check_mode_reports_without_serving(self):
        self.seed()
        cfg = dict(config.DEFAULTS)
        cfg.update({"host": None, "data_dir": self.tmp})
        viewer = serve.Viewer(cfg, self.tmp)
        out = self.capture_stdout(viewer.report)
        self.assertIn("plan       08c047b8", out)
        self.assertIn("plottable", out)


class TestIncrementalOpenFile(ViewerFixture):
    """The open file is read once, then extended.

    It grows every couple of seconds, so re-reading it whole on every 10 s poll
    cost 0.70 s of CPU on the reference host to learn what the last five records
    said. Records are append-only, so a summary can carry the offset it reached --
    but only if extending gives exactly what a full read would.
    """

    def append(self, path, recs):
        with open(path, "ab") as fh:
            for ts, mask, latency, payload in recs:
                fh.write(struct.pack(log.HEADER, ts, mask, latency) + payload)

    def cols(self, win, key):
        c = win["series"][key]
        return (c["n"], c["min"], c["max"], c["mean"])

    def test_extending_gives_exactly_what_a_full_read_gives(self):
        base = (1786600000 // 60) * 60 + 0.0
        path = self.write_records("20260814T090000",
                                  self.data_records(base, 20, step=2))
        s = self.series_of()
        key = "plant_ess_soc"
        series.window(s, base, base + 300, [key], bucket_s=60, cache=self.cache)
        self.append(path, self.data_records(base + 40, 20, step=2, soc_from=21.0))
        grown = series.window(s, base, base + 300, [key], bucket_s=60,
                              cache=self.cache)
        full = series.window(self.series_of(), base, base + 300, [key], bucket_s=60,
                             cache=series.SummaryCache())
        self.assertEqual(self.cols(grown, key), self.cols(full, key))
        self.assertEqual(grown["health"]["records"], full["health"]["records"])
        self.assertEqual(grown["health"]["latency_max"], full["health"]["latency_max"])
        self.assertEqual(grown["records"], 40)

    def test_the_head_of_the_file_is_not_read_twice(self):
        base = (1786600000 // 60) * 60 + 0.0
        path = self.write_records("20260814T090000",
                                  self.data_records(base, 30, step=2))
        s = self.series_of()
        series.window(s, base, base + 300, ["plant_ess_soc"], bucket_s=60,
                      cache=self.cache)
        offsets = []
        real = decode.records_from

        def watched(manifest, p, offset=0):
            offsets.append(offset)
            return real(manifest, p, offset)

        decode.records_from = watched
        self.addCleanup(lambda: setattr(decode, "records_from", real))
        self.append(path, self.data_records(base + 60, 5, step=2, soc_from=31.0))
        series.window(s, base, base + 300, ["plant_ess_soc"], bucket_s=60,
                      cache=self.cache)
        self.assertTrue(offsets and offsets[0] > 0,
                        f"resumed from {offsets}, so the head was re-read")

    def test_a_new_field_rebuilds_every_field_from_the_same_offset(self):
        # Otherwise the fields already cached stay at the old offset while the
        # resume marker moves on, and they silently skip the records in between.
        base = (1786600000 // 60) * 60 + 0.0
        path = self.write_records("20260814T090000",
                                  self.data_records(base, 20, step=2))
        s = self.series_of()
        series.window(s, base, base + 300, ["plant_ess_soc"], bucket_s=60,
                      cache=self.cache)
        self.append(path, self.data_records(base + 40, 20, step=2, soc_from=21.0))
        both = series.window(s, base, base + 300, ["plant_ess_soc", "plant_ess_soh"],
                             bucket_s=60, cache=self.cache)
        full = series.window(self.series_of(), base, base + 300,
                             ["plant_ess_soc", "plant_ess_soh"], bucket_s=60,
                             cache=series.SummaryCache())
        for key in ("plant_ess_soc", "plant_ess_soh"):
            self.assertEqual(self.cols(both, key), self.cols(full, key), key)

    def test_a_rotated_file_is_never_re_read(self):
        base = 1786600000.0
        self.write_records("20260814T090000", self.data_records(base, 10),
                           gzip_it=True)
        self.write_records("20260814T100000", self.data_records(base + 3600, 10))
        s = self.series_of()
        first = series.window(s, base, base + 3700, ["plant_ess_soc"], bucket_s=60,
                              cache=self.cache)
        self.assertEqual(first["files_decoded"], 2)
        again = series.window(s, base, base + 3700, ["plant_ess_soc"], bucket_s=60,
                              cache=self.cache)
        # The .gz is final; the open .bin is up to date, so neither needs work.
        self.assertEqual(again["files_decoded"], 0)


class TestDecodeIsBoundedToOneCore(ViewerFixture):
    """Concurrent requests must not multiply CPU use.

    The logger has to hit a 2 s tick on the same host. Three simultaneous window
    decodes during a deploy took its 5-minute median from ~90 ms to 164 ms and one
    tick to 1943 ms -- past its own 1800 ms soak gate. So decoding is serialised,
    and this asserts the serialisation rather than trusting the comment.
    """

    def test_two_windows_never_decode_at_the_same_time(self):
        base = 1786600000.0
        self.write_records("20260814T090000", self.data_records(base, 40))
        cfg = dict(config.DEFAULTS)
        cfg.update({"host": None, "data_dir": self.tmp})
        viewer = serve.Viewer(cfg, self.tmp)

        overlap = {"max": 0, "now": 0}
        guard = threading.Lock()
        real = series.window

        def watched(*a, **kw):
            with guard:
                overlap["now"] += 1
                overlap["max"] = max(overlap["max"], overlap["now"])
            try:
                time.sleep(0.05)          # widen the window a race would need
                return real(*a, **kw)
            finally:
                with guard:
                    overlap["now"] -= 1

        series.window = watched
        self.addCleanup(lambda: setattr(series, "window", real))
        threads = [threading.Thread(target=viewer.window, args=({"hours": ["1"]},))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(overlap["max"], 1,
                         "decodes ran concurrently; the logger pays for that")
        self.assertGreater(viewer.waited_s, 0, "contention should be recorded")

    def test_the_lock_is_reentrant_so_csv_can_build_a_window(self):
        base = 1786600000.0
        self.write_records("20260814T090000", self.data_records(base, 20))
        cfg = dict(config.DEFAULTS)
        cfg.update({"host": None, "data_dir": self.tmp})
        viewer = serve.Viewer(cfg, self.tmp)
        # csv() -> window(), both taking the same lock. A plain Semaphore deadlocks.
        out = viewer.csv({"hours": ["1"]})
        self.assertIn("# sigen viewer", out)


class TestBinding(ViewerFixture):
    """A wildcard bind must answer on both families.

    The bug this pins: an IPv4-only listener refuses the IPv6 address that mDNS
    advertises for a .local name, and macOS tries IPv6 first. curl hides it by
    falling back to IPv4, so the API tests fine from a shell while the browser
    shows connection refused.
    """

    def serve_on(self, bind):
        cfg = dict(config.DEFAULTS)
        cfg.update({"host": None, "data_dir": self.tmp})
        viewer = serve.Viewer(cfg, self.tmp)
        real_stdout = sys.stdout
        sys.stdout = io.StringIO()
        self.addCleanup(lambda: setattr(sys, "stdout", real_stdout))
        httpd = serve.Server((bind, 0), viewer)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        def stop():
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)
        self.addCleanup(stop)
        return httpd, port

    def reachable(self, family, host, port):
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect((host, port))
            s.sendall(b"GET /api/stats HTTP/1.0\r\n\r\n")
            return b"200" in s.recv(64)
        except OSError:
            return False
        finally:
            s.close()

    def test_a_wildcard_bind_answers_on_ipv4_and_ipv6(self):
        httpd, port = self.serve_on("0.0.0.0")
        self.assertEqual(httpd.families(), "IPv4 and IPv6")
        self.assertTrue(self.reachable(socket.AF_INET, "127.0.0.1", port),
                        "IPv4 client refused by the wildcard listener")
        self.assertTrue(self.reachable(socket.AF_INET6, "::1", port),
                        "IPv6 client refused -- this is the browser's first attempt")

    def test_an_explicit_v4_address_stays_v4_only(self):
        httpd, port = self.serve_on("127.0.0.1")
        self.assertEqual(httpd.families(), "IPv4")
        self.assertTrue(self.reachable(socket.AF_INET, "127.0.0.1", port))
        self.assertFalse(self.reachable(socket.AF_INET6, "::1", port),
                         "asking for 127.0.0.1 means only that")


class TestCache(ViewerFixture):
    def test_a_warm_window_does_no_decoding_at_all(self):
        base = 1786600000.0
        self.write_records("20260814T090000", self.data_records(base, 100))
        s = self.series_of()
        first = series.window(s, base, base + 200, ["plant_ess_soc"], bucket_s=30,
                             cache=self.cache)
        again = series.window(s, base, base + 200, ["plant_ess_soc"], bucket_s=30,
                              cache=self.cache)
        self.assertEqual(first["files_decoded"], 1)
        self.assertEqual(again["files_decoded"], 0, "the file cannot have changed")
        self.assertEqual(first["series"]["plant_ess_soc"]["mean"],
                         again["series"]["plant_ess_soc"]["mean"])

    def test_adding_a_field_only_decodes_that_field(self):
        base = 1786600000.0
        self.write_records("20260814T090000", self.data_records(base, 50))
        s = self.series_of()
        series.window(s, base, base + 100, ["plant_ess_soc"], bucket_s=30,
                      cache=self.cache)
        before = self.cache.stats()["entries"]
        series.window(s, base, base + 100, ["plant_ess_soc", "plant_ess_soh"],
                      bucket_s=30, cache=self.cache)
        self.assertEqual(self.cache.stats()["entries"], before + 1)

    def test_a_pruned_file_drops_out_of_the_index(self):
        # keep_days deletes .bin.gz on rotation, so a file really can vanish under
        # a running viewer. The index is re-derived per request, so it self-heals.
        base = 1786600000.0
        self.write_records("20260814T090000", self.data_records(base, 10))
        gone = self.write_records("20260814T100000",
                                  self.data_records(base + 3600, 10))
        s = self.series_of()
        self.assertEqual(len(s.spans()), 2)
        os.remove(gone)
        self.assertEqual(len(s.spans()), 1)
        w = series.window(s, base, base + 3700, ["plant_ess_soc"], bucket_s=60,
                          cache=self.cache)
        self.assertEqual(w["unreadable"], [])
        self.assertGreater(w["records"], 0, "the surviving file still serves")

    def test_a_file_that_fails_mid_read_is_reported_not_fatal(self):
        # The narrow race the index cannot absorb: readable when listed, unreadable
        # when read. Injected, because the real window is microseconds wide.
        base = 1786600000.0
        self.write_records("20260814T090000", self.data_records(base, 10))
        self.write_records("20260814T100000", self.data_records(base + 3600, 10))
        s = self.series_of()
        real = series.summarise_file

        def boom(ser, span, keys, bucket_s, cache=None):
            if "T100000" in span.name:
                raise OSError("file vanished under the reader")
            return real(ser, span, keys, bucket_s, cache)

        series.summarise_file = boom
        self.addCleanup(lambda: setattr(series, "summarise_file", real))
        w = series.window(s, base, base + 3700, ["plant_ess_soc"], bucket_s=60,
                          cache=self.cache)
        self.assertEqual(len(w["unreadable"]), 1)
        self.assertIn("T100000", w["unreadable"][0]["file"])
        self.assertGreater(w["records"], 0, "the readable file still serves")

    def test_the_cache_is_bounded(self):
        c = series.SummaryCache(max_entries=4)
        for i in range(10):
            c.put(("f", i), {"x": i})
        self.assertEqual(c.stats()["entries"], 4)


if __name__ == "__main__":
    unittest.main()
