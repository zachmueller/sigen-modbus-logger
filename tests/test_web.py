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
import calendar
import contextlib
import gzip
import io
import json
import os
import re
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
import ingest          # noqa: E402
import serve           # noqa: E402
import sync            # noqa: E402
import tiles           # noqa: E402
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

    # Every module on the read side. One list, so a new one is covered by all three
    # assertions below rather than by whichever the author remembered. tiles.py is here
    # because it also runs in the ingest Lambda, where a Modbus client would be pointed
    # at a LAN address it cannot reach -- but the guarantee is the same one either way.
    MODULES = ("series.py", "serve.py", "tiles.py")

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
        for name in self.MODULES:
            used = self.used_names(name)
            self.assertNotIn("Modbus", used,
                             f"{name} would become a second client of the inverter")
            self.assertNotIn("create_connection", used)
            self.assertNotIn("recv_exact", used)

    def test_viewer_modules_do_not_import_the_transport(self):
        # lib is imported for decode primitives; importing socket in serve.py is
        # only gethostname. What must never appear is a read of the device.
        for name in self.MODULES:
            used = self.used_names(name)
            self.assertNotIn("sweep", used)        # dump.sweep polls every register
            self.assertNotIn("identity_block", used)

    def test_no_write_function_codes(self):
        for name in self.MODULES:
            with open(os.path.join(HERE, name)) as fh:
                src = fh.read()
            for fc in ("5", "6", "15", "16"):
                self.assertNotIn(f"fc={fc}", src)


class TestDependenciesStayContained(unittest.TestCase):
    """boto3 is the one dependency, and it lives in exactly one module.

    The repository's headline claim is stdlib-only with nothing to install. sync.py breaks
    that deliberately -- signing S3 requests by hand is not a thing to hand-roll -- but the
    breach has to stay contained, or the capture host stops being able to capture without a
    pip install and the Pi Zero migration gets harder for no reason.
    """

    # Everything that must run on a bare Python: capture, decode, and the local viewer.
    STDLIB_ONLY = ("config.py", "lib.py", "log.py", "decode.py", "dump.py", "series.py",
                   "serve.py", "tiles.py", "ingest.py", "regmap_gen.py")

    def tree_of(self, name):
        with open(os.path.join(HERE, name)) as fh:
            return ast.parse(fh.read(), filename=name)

    def imported_by(self, name, top_level_only=False):
        """Modules imported by `name`. `top_level_only` ignores imports inside functions,
        which is the distinction between a hard dependency and a lazy one."""
        tree = self.tree_of(name)
        nodes = tree.body if top_level_only else list(ast.walk(tree))
        out = set()
        for n in nodes:
            if isinstance(n, ast.Import):
                out |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                out.add(n.module.split(".")[0])
        return out

    def test_only_sync_py_imports_boto3(self):
        for name in self.STDLIB_ONLY:
            self.assertNotIn("boto3", self.imported_by(name),
                             f"{name} must run on a bare Python; boto3 belongs in sync.py")

    def test_sync_imports_boto3_lazily(self):
        # At module scope it would break `sync.py --status` and `--dry-run` on a machine
        # that has never installed it, and would pull boto3 into any process that imports
        # this module for its ledger logic.
        self.assertIn("boto3", self.imported_by("sync.py"),
                      "sync.py does need boto3 somewhere")
        self.assertNotIn("boto3", self.imported_by("sync.py", top_level_only=True),
                         "boto3 must be imported inside the function that needs it")

    def test_sync_never_constructs_a_modbus_client(self):
        # Parsed with ast, not grepped: sync.py's own docstring says it never constructs
        # one, and a substring search cannot tell the promise from the breach. Same reason
        # TestViewerIsReadOnly does it this way.
        tree = self.tree_of("sync.py")
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.assertNotIn("Modbus", used)
        self.assertNotIn("lib", self.imported_by("sync.py"),
                         "the uploader has no business with the transport")


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

    def test_identity_survives_a_manifest_written_during_an_outage(self):
        # The logger reads model and serial ONCE, at startup, so a restart while the
        # inverter is not answering writes a manifest whose device block is just an error.
        # The newest manifest is the one used for decoding, so that error became the
        # answer to "what hardware is this?" -- reading as "the device is down", which is
        # a claim about NOW made from a file written days ago.
        #
        # Observed in the real archive: day one recorded the model, the two days after it
        # recorded "[Errno 64] Host is down".
        t0 = 1786600000.0
        self.write_records("20260814T090000", self.data_records(t0, 4),
                           manifest_day="20260814")
        good = os.path.join(self.tmp, "sigen-20260814-08c047b8.manifest.json")
        with open(good) as fh:
            man = json.load(fh)
        self.assertTrue(man["device"].get("model"), "fixture manifest has no model")
        blind = dict(man, device={"error": "[Errno 64] Host is down"})
        for day in ("20260815", "20260816"):
            with open(os.path.join(self.tmp,
                                   f"sigen-{day}-08c047b8.manifest.json"), "w") as fh:
                json.dump(blind, fh)

        s = self.series_of()
        self.assertEqual(s.manifest["device"], blind["device"],
                         "decoding still uses the newest manifest")
        self.assertEqual(s.device()["model"], man["device"]["model"],
                         "identity must come from whichever manifest actually has it")

    def test_identity_reports_the_failure_when_no_manifest_ever_read_it(self):
        # Falling back is not the same as inventing: if the model was never read, the
        # error is the honest answer.
        t0 = 1786600000.0
        self.write_records("20260814T090000", self.data_records(t0, 4))
        path = os.path.join(self.tmp, "sigen-20260814-08c047b8.manifest.json")
        with open(path) as fh:
            man = json.load(fh)
        with open(path, "w") as fh:
            json.dump(dict(man, device={"error": "no identity"}), fh)
        self.assertEqual(self.series_of().device(), {"error": "no identity"})

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


# ---------------------------------------------------------------------- tiles

class TestTiles(ViewerFixture):
    """Precomputed tiles must say exactly what a live window says.

    The hosted viewer reads static tiles; serve.py decodes on demand. Both feed the
    SAME web/app.js, so if the two paths disagree about one archive, both pages render,
    both look plausible, and only a careful side-by-side would ever show it. That is
    what these assert, and it is the invariant the whole hosted design rests on.

    Tile spans here are 300 s at 30 s buckets rather than the real hour/day/month, so
    the arithmetic is exercised without generating an hour of records. The arithmetic
    is the part that goes wrong.
    """

    SOC = "plant_ess_soc"
    KWH = "plant_accumulated_grid_import_energy"

    def setUp(self):
        super().setUp()
        # 300 s aligned, so tile boundaries land on bucket boundaries as UTC ones do.
        self.base = float((1786600000 // 300) * 300)
        self.bucket = 30
        self.span = 300

    def seed_walking(self, n=300, step=2):
        """`n` records with SOC and a lifetime counter both moving, so min/mean/max
        differ per bucket and the counter has a real first/last per tile."""
        recs = []
        for i in range(n):
            self.set_scaled(self.SOC, 20.0 + i * 0.1)
            self.set_scaled(self.KWH, 100.0 + i * 0.01)
            recs.append((self.base + i * step, self.full_mask(), 100 + (i % 7),
                         self.block_payload()))
        self.write_records("20260814T090000", recs)
        return self.series_of()

    def catalog_of(self, s):
        return {c["key"]: c for c in series.catalog(s)}

    def tile(self, s, cat, i):
        start = self.base + i * self.span
        return tiles.build_from_series(
            s, cat, start, start + self.span, self.bucket,
            field_keys=[self.SOC], counter_keys=[self.KWH], cache=self.cache)

    # -- the boundary rule -------------------------------------------------

    def test_adjacent_tiles_do_not_share_a_bucket(self):
        # _grid() is END-INCLUSIVE, which is right for a viewer and wrong for tiling.
        # Without end_exclusive the two tiles below would both carry the bucket at
        # base+300, so a concatenated series repeats a point at every tile boundary.
        s = self.seed_walking()
        cat = self.catalog_of(s)
        a, b = self.tile(s, cat, 0), self.tile(s, cat, 1)
        self.assertEqual(a["n"], self.span // self.bucket)
        self.assertEqual(b["n"], self.span // self.bucket)
        a_t = [a["start"] + i * a["bucket_s"] for i in range(a["n"])]
        b_t = [b["start"] + i * b["bucket_s"] for i in range(b["n"])]
        self.assertEqual(set(a_t) & set(b_t), set(), "tiles overlap by a bucket")
        self.assertEqual(a_t[-1] + self.bucket, b_t[0], "tiles must tile, with no gap")

    def test_end_exclusive_leaves_an_unaligned_window_alone(self):
        # The final bucket of a window that does not end on a boundary is genuinely
        # part of the span. Only an exactly-aligned end is the next tile's business.
        s = self.seed_walking(n=60)
        for kw in ({}, {"end_exclusive": True}):
            w = series.window(s, self.base, self.base + 305, [self.SOC],
                              bucket_s=self.bucket, cache=series.SummaryCache(), **kw)
            self.assertEqual(len(w["t"]), 11, kw)

    def test_a_misaligned_span_is_refused_not_silently_shifted(self):
        # A tile whose arrays do not line up with `n` draws every chart one bucket out,
        # per tile, cumulatively. Better to refuse at build time.
        s = self.seed_walking(n=60)
        cat = self.catalog_of(s)
        win = series.window(s, self.base, self.base + self.span, [self.SOC],
                            bucket_s=self.bucket, cache=self.cache)   # no end_exclusive
        with self.assertRaises(ValueError) as caught:
            tiles.build(win, cat, s.plan_hash, self.base, self.base + self.span,
                        self.bucket, [self.SOC], [])
        self.assertIn("end_exclusive", str(caught.exception))

    # -- the differential -------------------------------------------------

    def test_concatenated_tiles_equal_one_live_window(self):
        s = self.seed_walking()
        cat = self.catalog_of(s)
        a, b = self.tile(s, cat, 0), self.tile(s, cat, 1)

        # What serve.py would answer for the same span, through the same shaper.
        whole = series.window(s, self.base, self.base + 2 * self.span, [self.SOC, self.KWH],
                              bucket_s=self.bucket, cache=series.SummaryCache(),
                              warm_budget_s=0, end_exclusive=True)
        live = tiles.columns(whole, cat, [self.SOC])

        for field in ("mean", "min", "max"):
            joined = a["series"][self.SOC][field] + b["series"][self.SOC][field]
            self.assertEqual(joined, live[self.SOC][field],
                             f"{field}: tiles disagree with a live window")
        self.assertEqual(a["series"][self.SOC]["unit"], live[self.SOC]["unit"])
        self.assertEqual(a["series"][self.SOC]["cadence_s"], live[self.SOC]["cadence_s"])

        # Health too: the outage hatching and every tooltip's record count come from it.
        for field in ("records", "empty"):
            self.assertEqual(a["health"][field] + b["health"][field],
                             whole["health"][field], field)
        self.assertEqual(a["records"] + b["records"], whole["records"])

    def compose_energy(self, tiles_, key, start, end):
        """What web/tiles.js does: lay each tile's per-bucket counter endpoints onto the
        window grid, then take the first and last surviving values.

        Reimplemented here rather than mocked, because this arithmetic is the thing being
        tested and the JavaScript has no test harness (see the project notes)."""
        n = (end - start) // self.bucket
        first = [None] * int(n)
        last = [None] * int(n)
        for t in tiles_:
            c = t["counters"].get(key)
            if not c:
                continue
            for j in range(t["n"]):
                i = int((t["start"] + j * t["bucket_s"] - start) // self.bucket)
                if not 0 <= i < n:
                    continue                  # this bucket is outside the window
                if c["first"][j] is not None:
                    first[i] = c["first"][j]
                if c["last"][j] is not None:
                    last[i] = c["last"][j]
        lo = next((v for v in first if v is not None), None)
        hi = next((v for v in reversed(last) if v is not None), None)
        return lo, hi

    def test_counters_compose_across_tiles(self):
        s = self.seed_walking()
        cat = self.catalog_of(s)
        a, b = self.tile(s, cat, 0), self.tile(s, cat, 1)
        start, end = self.base, self.base + 2 * self.span
        whole = series.window(s, start, end, [self.KWH], bucket_s=self.bucket,
                              cache=series.SummaryCache(), warm_budget_s=0,
                              end_exclusive=True)
        want = series.energy(whole, [self.KWH])[self.KWH]

        lo, hi = self.compose_energy([a, b], self.KWH, start, end)
        self.assertAlmostEqual(lo, want["first"], places=6)
        self.assertAlmostEqual(hi, want["last"], places=6)
        self.assertAlmostEqual(hi - lo, want["kwh"], places=3)
        # And the counter travels as endpoints, never as a line: 20 points of a lifetime
        # counter is payload nothing draws.
        self.assertNotIn(self.KWH, a["series"])

    def test_a_counter_is_clipped_to_the_window_not_the_tile(self):
        # THE bug this format exists to prevent, and it was live before this test.
        #
        # A tile spans a whole UTC hour; a window does not. Reading a tile's own endpoints
        # measures the counter from the top of the hour, so "the last six hours" starting
        # at :05 picked up five extra minutes of energy -- measured at 5.35 kWh against a
        # true 5.23, a 2% overstatement that looks entirely plausible on screen.
        s = self.seed_walking()
        cat = self.catalog_of(s)
        tiles_ = [self.tile(s, cat, 0), self.tile(s, cat, 1)]

        # A window starting a third of the way into the first tile, deliberately unaligned.
        start = self.base + self.span // 3
        end = self.base + 2 * self.span
        start = float(int(start) // self.bucket * self.bucket)   # on a bucket boundary

        live = series.window(s, start, end, [self.KWH], bucket_s=self.bucket,
                             cache=series.SummaryCache(), warm_budget_s=0,
                             end_exclusive=True)
        want = series.energy(live, [self.KWH])[self.KWH]
        lo, hi = self.compose_energy(tiles_, self.KWH, start, end)
        self.assertAlmostEqual(hi - lo, want["kwh"], places=3,
                               msg="the total must measure from the WINDOW's edge")

        # And show the mistake it replaces would have been wrong: the tile's own first
        # value is earlier, and therefore gives a larger total.
        tile_first = next(v for v in tiles_[0]["counters"][self.KWH]["first"]
                          if v is not None)
        self.assertLess(tile_first, lo,
                        "the fixture must have the window starting inside the tile, or "
                        "this test proves nothing")
        self.assertGreater(hi - tile_first, want["kwh"])

    def test_a_reset_across_a_tile_boundary_is_visible_to_the_reader(self):
        # Neither tile can see it from inside: A ends high, B starts low, and each is
        # internally monotonic. So `first`/`last` have to be raw endpoints, which is what
        # lets whoever concatenates compare B.first against A.last (FINDINGS 11).
        recs = []
        for i in range(300):
            kwh = 100.0 + i * 0.01 if i < 150 else 50.0 + (i - 150) * 0.01
            self.set_scaled(self.KWH, kwh)
            self.set_scaled(self.SOC, 20.0)
            recs.append((self.base + i * 2, self.full_mask(), 100, self.block_payload()))
        self.write_records("20260814T090000", recs)
        s = self.series_of()
        cat = self.catalog_of(s)
        a, b = self.tile(s, cat, 0), self.tile(s, cat, 1)
        self.assertFalse(a["counters"][self.KWH]["reset"], "A is monotonic internally")
        self.assertFalse(b["counters"][self.KWH]["reset"], "B is monotonic internally")
        self.assertLess(b["counters"][self.KWH]["first"], a["counters"][self.KWH]["last"],
                        "the step back has to be detectable at the seam")

    # -- shape -------------------------------------------------------------

    def test_a_tile_omits_the_grid_and_says_how_to_rebuild_it(self):
        s = self.seed_walking()
        a = self.tile(s, self.catalog_of(s), 0)
        self.assertNotIn("t", a, "t is start + i * bucket_s; sending it is ~10 KB wasted")
        for key in ("v", "plan", "bucket_s", "start", "n", "series", "counters",
                    "health", "covered", "records"):
            self.assertIn(key, a)
        self.assertEqual(a["v"], tiles.TILE_VERSION)
        self.assertEqual(a["plan"], s.plan_hash)
        self.assertEqual(len(a["series"][self.SOC]["mean"]), a["n"])

    def test_covered_is_clamped_to_the_tile(self):
        # A tile is self-contained: `covered` tells the reader which buckets are inside
        # the archive at all, and a span reaching past the tile would make that call
        # about data this tile does not hold.
        s = self.seed_walking()
        a = self.tile(s, self.catalog_of(s), 0)
        for lo, hi in a["covered"]:
            self.assertGreaterEqual(lo, a["start"])
            self.assertLessEqual(hi, a["start"] + a["n"] * a["bucket_s"])

    def test_an_absent_field_ships_as_empty_not_as_nulls(self):
        # `empty` means captured-but-nothing-there: the field's BLOCK was absent from
        # every record in the span. That happens for real -- a slow-tier block that did
        # not fire in this window, or one that failed -- and over a day tile carrying the
        # whole catalogue it is the difference between three 288-long null arrays per
        # field and one boolean.
        #
        # Note it is NOT what the unit's 32 unpopulated PV channels do: those return the
        # -1 absent marker, which decodes to a legal -0.01 reading, so they ship as a flat
        # line. serve.PANELS excludes them by name rather than relying on this.
        absent = "inverter_rated_active_power"      # in inv_battery, block index 3
        keep = [i for i in range(len(self.tiers)) if i != 3]
        mask = sum(1 << i for i in keep)
        recs = []
        for i in range(60):
            self.set_scaled(self.SOC, 20.0 + i * 0.1)
            recs.append((self.base + i * 2, mask, 100, self.block_payload(keep)))
        self.write_records("20260814T090000", recs)
        s = self.series_of()
        cat = self.catalog_of(s)
        self.assertIn(absent, cat, "fixture no longer covers the inv_battery block")
        a = tiles.build_from_series(s, cat, self.base, self.base + self.span,
                                    self.bucket, field_keys=[self.SOC, absent],
                                    counter_keys=[], cache=self.cache)
        self.assertTrue(a["series"][absent]["empty"])
        self.assertNotIn("mean", a["series"][absent])
        # And the field that WAS present is unaffected -- "empty" is per field.
        self.assertEqual(len(a["series"][self.SOC]["mean"]), a["n"])

    def test_a_partial_tile_is_refused_rather_than_written_as_whole(self):
        # The warm budget exists to keep an interactive request responsive. An ingest run
        # has nothing to be responsive to, and a tile missing a file would be cached
        # immutably with a hole in it.
        s = self.seed_walking(n=60)
        cat = self.catalog_of(s)
        real = series.window

        def stingy(*a, **kw):
            out = real(*a, **kw)
            out["pending_files"] = 1
            return out
        series.window = stingy
        self.addCleanup(lambda: setattr(series, "window", real))
        with self.assertRaises(ValueError) as caught:
            tiles.build_from_series(s, cat, self.base, self.base + self.span,
                                    self.bucket, [self.SOC], [])
        self.assertIn("partial tile", str(caught.exception))

    def test_bucket_width_maps_to_a_tile_span(self):
        # Hour tiles carry the panel fields at fine widths; from 120 s up a tile spans a
        # UTC day and carries the whole catalogue, where the per-field cost has collapsed.
        self.assertEqual(tiles.granularity_for(1), tiles.HOUR)
        self.assertEqual(tiles.granularity_for(30), tiles.HOUR)
        self.assertEqual(tiles.granularity_for(60), tiles.HOUR)
        self.assertEqual(tiles.granularity_for(120), tiles.DAY)
        self.assertEqual(tiles.granularity_for(1800), tiles.DAY)
        self.assertEqual(tiles.granularity_for(3600), tiles.MONTH)
        self.assertEqual(tiles.granularity_for(86400), tiles.MONTH)

    def test_every_ladder_width_divides_a_utc_day(self):
        # This is what makes UTC-aligned tiles concatenate: a width that did not divide
        # 86400 would straddle the day boundary, and the seam would be a duplicated or
        # missing bucket once a day rather than something anyone would notice.
        for b in series.BUCKET_LADDER:
            self.assertEqual(86400 % b, 0, f"{b}s does not divide a UTC day")
            if tiles.granularity_for(b) == tiles.HOUR:
                self.assertEqual(3600 % b, 0, f"{b}s does not divide a UTC hour")


# ----------------------------------------------------------------- uploading

class TestSync(ViewerFixture):
    """The offsite uploader's bookkeeping. No network: boto3 is never reached.

    What can go wrong here is quiet in a specific way -- a file that is never uploaded, or
    one uploaded to a key nothing can decode -- and the symptom appears weeks later as a
    hole in the hosted archive.
    """

    def ledger(self):
        return sync.Ledger(os.path.join(self.tmp, sync.LEDGER))

    def test_the_open_bin_is_never_uploaded(self):
        # It grows every couple of seconds. Uploading it would mean re-uploading a partial
        # file forever; log.py gzips it on rotation and the .gz is what travels.
        t0 = 1786600000.0
        self.write_records("20260814T090000", self.data_records(t0, 4))            # .bin
        self.write_records("20260814T100000", self.data_records(t0 + 3600, 4),
                           gzip_it=True)                                          # .bin.gz
        names = {n for n, _, _ in sync.pending(self.tmp, self.ledger())}
        self.assertTrue(any(n.endswith(".bin.gz") for n in names))
        self.assertFalse(any(n.endswith(".bin") and not n.endswith(".bin.gz")
                             for n in names), "the open file must not be uploaded")
        self.assertTrue(any(n.endswith(".manifest.json") for n in names),
                        "without a manifest nothing downstream can decode the records")

    def test_an_un_rotated_bin_is_reported_rather_than_silently_dropped(self):
        # A logger killed mid-hour leaves its .bin un-gzipped, and nothing gzips it later,
        # so that hour never reaches S3 by this route. wanted() is right to skip it -- the
        # open file is still growing -- but skipping it in silence is how an hour goes
        # missing for weeks. What proves a .bin is finished is a NEWER .bin.gz beside it.
        t0 = 1786600000.0
        self.write_records("20260814T090000", self.data_records(t0, 4))            # died
        self.write_records("20260814T100000", self.data_records(t0 + 3600, 4),
                           gzip_it=True)                                          # rotated
        self.write_records("20260814T110000", self.data_records(t0 + 7200, 4))     # open
        self.assertEqual(sync.stranded(os.listdir(self.tmp)),
                         ["sigen-20260814T090000-08c047b8.bin"],
                         "only a .bin a later rotation has moved past is stranded")
        # Reporting it must not turn into uploading it: it is skipped for a reason.
        pend = {n for n, _, _ in sync.pending(self.tmp, self.ledger())}
        self.assertNotIn("sigen-20260814T090000-08c047b8.bin", pend)

    def test_the_open_bin_is_not_reported_as_stranded(self):
        # Otherwise the current hour is reported every five minutes, for the whole hour,
        # every hour -- and a warning that is always on is a warning nobody reads.
        t0 = 1786600000.0
        self.write_records("20260814T090000", self.data_records(t0, 4), gzip_it=True)
        self.write_records("20260814T100000", self.data_records(t0 + 3600, 4))
        self.assertEqual(sync.stranded(os.listdir(self.tmp)), [])
        # Nor before anything has rotated at all, when the only .bin there is is the open one.
        self.assertEqual(sync.stranded(["sigen-20260814T090000-08c047b8.bin"]), [])

    def test_a_bin_beside_its_own_gz_is_not_stranded(self):
        # The observed case: cloud/backfill.py uploaded the then-open .bin, and rotation
        # later produced the .bin.gz for the same stem. The .gz supersedes it, so calling
        # the .bin stranded would send somebody hunting for an hour that is already offsite.
        t0 = 1786600000.0
        recs = self.data_records(t0, 4)
        self.write_records("20260814T090000", recs)
        self.write_records("20260814T090000", recs, gzip_it=True)
        self.write_records("20260814T100000", self.data_records(t0 + 3600, 4), gzip_it=True)
        self.assertEqual(sync.stranded(os.listdir(self.tmp)), [])

    def test_the_ledger_stops_a_file_being_uploaded_twice(self):
        t0 = 1786600000.0
        self.write_records("20260814T090000", self.data_records(t0, 4), gzip_it=True)
        led = self.ledger()
        todo = sync.pending(self.tmp, led)
        self.assertTrue(todo)
        for name, sz, mtime in todo:
            led.record(name, sz, mtime)
        led.save()
        self.assertEqual(sync.pending(self.tmp, self.ledger()), [],
                         "a recorded file must not come back as pending")

    def test_a_rewritten_manifest_is_uploaded_again(self):
        # log.py re-emits the manifest when the date rolls over. Keyed on size and mtime,
        # so a rewrite is noticed without hashing the whole archive every run.
        t0 = 1786600000.0
        self.write_records("20260814T090000", self.data_records(t0, 4), gzip_it=True)
        led = self.ledger()
        for name, sz, mtime in sync.pending(self.tmp, led):
            led.record(name, sz, mtime)
        led.save()
        man = os.path.join(self.tmp, "sigen-20260814-08c047b8.manifest.json")
        with open(man) as fh:
            d = json.load(fh)
        time.sleep(1.1)                          # mtime resolution
        with open(man, "w") as fh:
            json.dump(dict(d, started_at="later"), fh)
        names = {n for n, _, _ in sync.pending(self.tmp, self.ledger())}
        self.assertIn(os.path.basename(man), names)

    def test_a_corrupt_ledger_re_uploads_rather_than_crashing(self):
        # Losing the ledger is harmless -- uploading is idempotent -- and crashing on it
        # would stop the archive going offsite over a cache file.
        t0 = 1786600000.0
        self.write_records("20260814T090000", self.data_records(t0, 4), gzip_it=True)
        with open(os.path.join(self.tmp, sync.LEDGER), "w") as fh:
            fh.write("{not json")
        self.assertTrue(sync.pending(self.tmp, self.ledger()))

    def test_the_key_carries_the_plan_hash(self):
        # The ingest Lambda finds the plan from the key, and decode.check_plan_hash treats
        # a missing hash as a mismatch. A file without one is skipped and reported, never
        # filed under a guess.
        self.assertEqual(sync.plan_of("sigen-20260814T090000-08c047b8.bin.gz"), "08c047b8")
        self.assertEqual(sync.plan_of("sigen-20260814-08c047b8.manifest.json"), "08c047b8")
        self.assertIsNone(sync.plan_of("sigen-20260814T074107.bin"))
        self.assertIsNone(sync.plan_of("sigen-20260814.manifest.json"))

    def test_the_ledger_write_is_atomic(self):
        # A kill mid-write must not leave a truncated ledger, which would re-upload the
        # whole archive on the next run.
        with open(os.path.join(HERE, "sync.py")) as fh:
            src = fh.read()
        self.assertIn("os.replace(tmp, self.path)", src)


# ------------------------------------------------------- the Lambda package

class TestIngestPackage(unittest.TestCase):
    """What ships to the Python Lambdas, derived rather than trusted.

    This exists because the hand-written list was wrong: it omitted config.py, which
    serve.py imports, and the only symptom was `No module named 'config'` in a CloudWatch
    log after a successful deploy. A packaging mistake should fail here, in a second,
    rather than in a cloud log minutes later.

    TWO handlers ship this list now -- ingest and share -- because both import the tile
    geometry rather than restating it, so the closure is computed over both.
    """

    PACKAGE = os.path.join(HERE, "cloud", "lambda", "ingest", "PACKAGE.txt")
    HANDLER = os.path.join(HERE, "cloud", "lambda", "ingest", "handler.py")
    SHARE_HANDLER = os.path.join(HERE, "cloud", "lambda", "share", "handler.py")

    def handlers(self):
        return [self.HANDLER, self.SHARE_HANDLER]

    def listed(self):
        with open(self.PACKAGE) as fh:
            lines = [x.strip() for x in fh]
        return [x for x in lines if x and not x.startswith("#")]

    def local_modules(self):
        return {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}

    def imports_of(self, path):
        with open(path) as fh:
            tree = ast.parse(fh.read(), filename=path)
        local, out = self.local_modules(), set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                out.add(n.module.split(".")[0])
        return out & local

    def closure(self, roots=None):
        """Every repository module the given handlers reach, transitively."""
        seen, stack = set(), list(roots or self.handlers())
        while stack:
            for dep in self.imports_of(stack.pop()):
                if dep not in seen:
                    seen.add(dep)
                    stack.append(os.path.join(HERE, dep + ".py"))
        return seen

    def test_the_package_list_is_exactly_the_import_closure(self):
        listed = {x for x in self.listed() if x.endswith(".py")}
        self.assertEqual(listed, {m + ".py" for m in self.closure()},
                         "PACKAGE.txt has drifted from what the Lambda handlers import")

    def test_the_share_handler_needs_nothing_the_list_does_not_carry(self):
        # The two Lambdas share one PACKAGE.txt, which is only safe while the share
        # handler's closure is a SUBSET of what the list already carries. If it ever grows
        # an import the ingest handler does not reach, the union above keeps the list
        # correct -- but this states the assumption so a surprise is visible.
        share_only = self.closure([self.SHARE_HANDLER]) - self.closure([self.HANDLER])
        self.assertEqual(share_only, set(),
                         f"the share handler reaches {sorted(share_only)}, which the ingest "
                         f"handler does not -- one list still covers both, but say so here")

    def test_the_share_handler_imports_the_tile_geometry_rather_than_restating_it(self):
        # A share that computed its own bucket width or key layout could write tiles under
        # keys web/tiles.js never asks for, and the symptom would be a share page that
        # renders as a flat line -- indistinguishable from an outage. So it must reach these
        # three, and the assertion is on the IMPORT, not on a substring of the arithmetic.
        reached = self.imports_of(self.SHARE_HANDLER)
        for module in ("series", "tiles", "ingest"):
            self.assertIn(module, reached,
                          f"cloud/lambda/share/handler.py must import {module} rather than "
                          f"restating tile geometry")

    def test_the_register_map_travels_with_the_modules(self):
        # dump.load_regmap() locates it relative to __file__, so a package without it
        # imports cleanly and then fails on the first decode.
        self.assertIn("regmap.json", self.listed())

    def test_the_module_that_can_talk_to_the_inverter_is_not_in_the_package(self):
        # log.py is the only module that constructs lib.Modbus. The ingest path is
        # read-only by construction because that code is not shipped at all -- a stronger
        # statement than "it is never called".
        self.assertNotIn("log.py", self.listed())
        with open(os.path.join(HERE, "log.py")) as fh:
            self.assertIn("Modbus(", fh.read(), "log.py no longer opens the connection; "
                                                "this test is pinning the wrong module")

    def test_every_packaged_file_exists(self):
        for rel in self.listed():
            self.assertTrue(os.path.exists(os.path.join(HERE, rel)), rel)


class TestSharePostIsSigned(unittest.TestCase):
    """POST /api/share, and the signature without which it is a 403 that logs nothing.

    `/api/share` is a Lambda function URL with `AWS_IAM` auth behind CloudFront Origin
    Access Control. OAC signs every origin request with SigV4, and per the CloudFront
    documentation a function URL **does not support an unsigned payload**: a POST body's
    SHA-256 has to arrive in `x-amz-content-sha256` or Lambda rejects the request.

    It rejects it at the authorizer, so the handler is never invoked and its log group stays
    empty -- and the refusal body is `{"Message": "Forbidden"}`, which has no `error` for
    web/app.js to read, so the page said only `403 `. Every browser click of "Create link"
    failed that way while `curl` against the Lambda and the rendered `/p/<uid>` page both
    looked perfect. See docs/FINDINGS.md 27.

    Source-level assertions because there is no JS harness here, and because the bug lives in
    the AGREEMENT between three files: site-stack.ts must expose the body, index.js must hash
    it, and app.js must be able to report it when either is missing. Any one of them alone is
    the same silent 403.
    """

    EDGE = os.path.join(HERE, "cloud", "lambda", "auth-edge", "index.js")
    STACK = os.path.join(HERE, "cloud", "infrastructure", "lib", "site-stack.ts")
    APP = os.path.join(HERE, "web", "app.js")

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def test_the_gate_hashes_the_body_cloudfront_will_not_do_it(self):
        js = self.read(self.EDGE)
        fn = re.search(r"function signPayload\(request\)\s*\{(.*?)\n\}", js, re.S)
        self.assertIsNotNone(fn, "the gate must sign the payload in one function")
        body = re.sub(r"\s+", " ", fn.group(1))
        self.assertIn("x-amz-content-sha256", body,
                      "the header name IS the requirement -- OAC signs the request and "
                      "Lambda refuses a POST whose payload hash is absent")
        self.assertIn("createHash('sha256')", body,
                      "it must be a SHA-256 of the body, not a placeholder")
        self.assertIn("base64", body,
                      "CloudFront always base64-encodes a body before exposing it to "
                      "Lambda@Edge, so the bytes have to be decoded before hashing")
        self.assertRegex(js, r"if \(isApi\(request\.uri\)\)[\s\S]{0,400}signPayload\(request\)",
                         "sign only the API paths, and only after the allowlist above -- an "
                         "anonymous caller must not reach it")

    def test_a_truncated_body_is_refused_rather_than_signed(self):
        # CloudFront truncates a viewer-request body at 40 KB before exposing it, but sends
        # the FULL body to the origin when the function leaves it read-only. So hashing a
        # truncated body signs bytes the origin never receives: the same 403, with the header
        # present and looking right. It must refuse instead, and say so.
        js = self.read(self.EDGE)
        self.assertIn("inputTruncated", js,
                      "the gate must notice a truncated body before hashing it")
        self.assertRegex(js, r"inputTruncated\) return null",
                         "signPayload() reports 'cannot sign this' rather than signing the "
                         "part it happens to have")
        self.assertRegex(js, r"413", "a body too large to sign is a 413 the page can print, "
                                     "not a 403 nobody can explain")

    def test_only_the_share_behaviour_is_handed_a_body(self):
        # includeBody and the hash are two halves of one mechanism: either alone is a 403.
        # And /view and /agg/* have no body, so exposing one there buys nothing.
        ts = self.read(self.STACK)
        self.assertRegex(ts, r"includeBody: true",
                         "the gate cannot hash a body CloudFront does not expose")
        gate_with_body = re.search(r"const gateSigningBody[^;]*;", ts, re.S)
        self.assertIsNotNone(gate_with_body,
                            "keep the body-bearing association separate from `gate`")
        self.assertIn("includeBody", gate_with_body.group(0))
        plain = re.search(r"const gate:[^;]*;", ts, re.S)
        self.assertIsNotNone(plain)
        self.assertNotIn("includeBody", plain.group(0),
                         "/view and /agg/* are GETs; only /api/share needs its body")
        share = re.search(r"'/api/share': \{(.*?)\n\t\t\t\t\},", ts, re.S)
        self.assertIsNotNone(share, "the /api/share behaviour should still be here")
        self.assertIn("edgeLambdas: gateSigningBody", share.group(1),
                      "the share behaviour must use the association that carries the body")

    def test_cloudfront_is_granted_both_invoke_actions_not_just_the_url_one(self):
        # A signed request still has to be AUTHORISED, and since October 2025 a function URL
        # requires lambda:InvokeFunction as well as lambda:InvokeFunctionUrl. CDK's
        # withOriginAccessControl() grants only the latter -- it grants both for authType
        # NONE, so the omission is specific to the OAC path -- and the symptom of the gap is
        # indistinguishable from the payload-hash bug above: 403, no invocation, no log, and
        # a gate that has already said `allow`. This is the second cause of one symptom.
        #
        # Pinned on the stack rather than the deployed policy because a test cannot reach AWS.
        # A future CDK that grants it too makes this redundant, not wrong: the statements are
        # identical. Deleting it as "surely CDK does that" is the regression.
        ts = self.read(self.STACK)
        grant = re.search(r"shareFn\.addPermission\([^;]*;", ts, re.S)
        self.assertIsNotNone(grant,
                             "site-stack.ts must grant CloudFront lambda:InvokeFunction "
                             "itself; withOriginAccessControl() stops at InvokeFunctionUrl")
        self.assertIn("'lambda:InvokeFunction'", grant.group(0))
        self.assertIn("cloudfront.amazonaws.com", grant.group(0))
        self.assertIn("sourceArn", grant.group(0),
                      "scope it to this distribution -- otherwise any CloudFront distribution "
                      "in any account could invoke the endpoint that mints public shares")
        self.assertIn("distribution/", grant.group(0))

    def test_the_page_names_a_refusal_that_carries_no_error(self):
        # The whole cost of this bug was diagnostic: three layers can refuse a share, and the
        # page could only read one of their shapes. `{"Message": "Forbidden"}` reduced to
        # "403 " -- no message, and no log anywhere, because the handler never ran.
        js = self.read(self.APP)
        fn = re.search(r"function refusalText\(r, body, raw\)\s*\{(.*?)\n\}", js, re.S)
        self.assertIsNotNone(fn, "web/app.js should name a refusal in one function")
        body = re.sub(r"\s+", " ", fn.group(1))
        self.assertIn("body.error", body, "the gate and the handler both send `error`")
        # Both casings, measured against the real endpoint: an unsigned request is refused
        # with {"Message": "Forbidden"}, a signature over the wrong payload hash with
        # {"message": "The request signature we calculated does not match…"}, and a crashed
        # handler with {"message": "Internal Server Error"}. Reading only `error` -- or only
        # one of the two casings -- is what reduced all of them to a bare status code.
        self.assertIn("body.Message", body, "the unsigned-request refusal uses a capital M")
        self.assertIn("body.message", body,
                      "the signature-mismatch refusal and the handler's own 502 use a "
                      "lowercase m -- both are 403/502 with no `error` at all")
        self.assertIn("raw", body,
                      "a CloudFront error page is not JSON at all, and still has to be "
                      "quotable rather than discarded")
        # getJSON() may still use statusText: it talks to serve.py over HTTP/1.1, where the
        # reason phrase exists. Over HTTP/2, which the distribution serves, it is always ''
        # -- so on this path it can only pad a message that already says nothing.
        code = "\n".join(re.sub(r"//.*$", "", line) for line in js.split("\n"))
        share = re.search(r"async function createShare\(\)[\s\S]*?\nfunction showShareLink",
                          code)
        self.assertIsNotNone(share)
        self.assertNotIn("statusText", share.group(0),
                         "statusText is empty over HTTP/2; '403 ' is what that produced")
        self.assertRegex(js, r"const raw = await r\.text\(\);",
                         "read the body once as text, then parse: a Response can only be "
                         "consumed once, and r.json() throws away a non-JSON refusal")


class TestShareHandlerErrors(unittest.TestCase):
    """The share handler's own refusals, which must not arrive as an unhandled exception.

    An exception that escapes a Lambda behind a function URL is a 502 carrying
    `{"message": "Internal Server Error"}` -- no `error`, so the page renders `502 ` and
    names nothing, which is the same dead end as the 403 above. S3 is the thing most likely
    to refuse (FINDINGS 11: without `s3:ListBucket` a MISSING key answers 403), so that path
    has to end in a reply the page can print.

    boto3 is stubbed rather than required: every other module in this repo runs on a bare
    Python, and a test that needs boto3 installed would be the first exception.
    """

    HANDLER = os.path.join(HERE, "cloud", "lambda", "share", "handler.py")

    class ClientError(Exception):
        """botocore's shape, which is all the handler touches."""

        def __init__(self, response, operation_name):
            super().__init__(f"An error occurred ({response['Error']['Code']}) when calling "
                             f"the {operation_name} operation")
            self.response = response
            self.operation_name = operation_name

    def load(self, raises):
        """The handler module, with an S3 client that refuses everything with `raises`."""
        import importlib.util
        import types

        boto3_stub = types.ModuleType("boto3")
        client = types.SimpleNamespace(
            exceptions=types.SimpleNamespace(ClientError=self.ClientError))

        def refuse(*a, **kw):
            raise raises
        for op in ("get_object", "put_object", "head_object", "copy_object", "get_paginator"):
            setattr(client, op, refuse)
        boto3_stub.client = lambda *a, **kw: client

        keep_tz = os.environ.get("TZ")
        env = {"BUCKET": "test-bucket", "SITE_DOMAIN": "example.test", "CAPTURE_TZ": "UTC"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        sys.modules["boto3"] = boto3_stub
        try:
            spec = importlib.util.spec_from_file_location("share_handler", self.HANDLER)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        finally:
            del sys.modules["boto3"]
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            # The handler sets TZ and calls tzset() at import, by design -- see its header.
            # Undo it, or every test after this one reads timestamps in UTC.
            if keep_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = keep_tz
            time.tzset()

    def test_a_refused_s3_read_is_a_named_error_not_an_unhandled_raise(self):
        refusal = self.ClientError({"Error": {"Code": "403", "Message": "Forbidden"}},
                                   "HeadObject")
        mod = self.load(refusal)
        # Captured, not silenced: the handler is SUPPOSED to log the code, the operation and a
        # traceback -- that is how the key it was refused becomes recoverable -- but a test
        # that prints one looks like a test that failed.
        logged = io.StringIO()
        with contextlib.redirect_stdout(logged), contextlib.redirect_stderr(logged):
            reply = mod.lambda_handler({"body": json.dumps({"hours": 24})}, None)
        self.assertIn('"operation": "HeadObject"', logged.getvalue(),
                      "the log line is the other half: the reply names what refused, the log "
                      "names which key")
        self.assertIn("Traceback", logged.getvalue(),
                      "and the traceback, because FINDINGS 11 was found by reading one")
        self.assertEqual(reply["statusCode"], 500,
                         "an S3 refusal is ours, not the caller's -- but it must be a reply, "
                         "because an escaping exception is a 502 the page cannot read")
        body = json.loads(reply["body"])
        self.assertFalse(body["ok"])
        self.assertIn("HeadObject", body["error"],
                      "name the operation: FINDINGS 11 took a traceback to find, and the "
                      "operation is what identified it")
        self.assertIn("403", body["error"], "and the code, since 403-on-absent is the "
                                            "failure this endpoint has already had once")


# --------------------------------------------------------------------- ingest

class TestIngest(ViewerFixture):
    """Turning an archive into the objects the hosted viewer reads.

    No S3 here: ingest.py reads a directory and writes a directory, which is the whole
    reason it is testable at all. The Lambda is the thin part that syncs one to the other.
    """

    def setUp(self):
        super().setUp()
        # 09:00 UTC on a UTC day boundary + 9 h, so hour, day and month spans are all
        # exercised and none of them coincides with the local date -- the capture host is
        # UTC+12, and a tile named by the local date would be the bug.
        self.base = 1786600800.0        # 2026-08-14 21:20 NZST = 09:20 UTC
        self.base = float(int(self.base) // 3600 * 3600)

    def seed(self, hours=2, step=2):
        recs = []
        n = int(hours * 3600 / step)
        for i in range(n):
            self.set_scaled("plant_ess_soc", 20.0 + (i % 500) * 0.1)
            self.set_scaled("plant_accumulated_grid_import_energy", 100.0 + i * 0.001)
            recs.append((self.base + i * step, self.full_mask(), 100, self.block_payload()))
        self.write_records("20260814T090000", recs)
        return series.newest_series(self.tmp)

    def out(self):
        d = os.path.join(self.tmp, "agg")
        os.makedirs(d, exist_ok=True)
        return d

    # -- UTC partitioning --------------------------------------------------

    def test_spans_are_utc_never_the_capture_hosts_local_date(self):
        # A tile named by the local date would straddle a bucket boundary twice a year at
        # a DST transition, leaving a one-hour seam. The page still LABELS local, from
        # meta's tz change points -- partitioning and labelling are separate concerns.
        #
        # Pick an instant whose UTC and local dates DIFFER, or the test proves nothing.
        # Anywhere east of UTC, a local morning is the previous UTC day.
        ts = calendar.timegm((2026, 8, 13, 21, 0, 0, 0, 0, 0))   # 09:00 next day at +12
        utc_date = time.strftime("%Y/%m/%d", time.gmtime(ts))
        local_date = time.strftime("%Y/%m/%d", time.localtime(ts))
        if utc_date == local_date:
            self.skipTest("this machine's zone makes the UTC and local dates equal here")
        self.assertEqual(ingest.hour_span(ts)[0] % 3600, 0)
        self.assertEqual(ingest.day_span(ts)[0] % 86400, 0)
        rel = ingest.path_for("abc12345", 300, ingest.day_span(ts)[0], tiles.DAY)
        self.assertIn(utc_date, rel, "a tile is named by its UTC span")
        self.assertNotIn(local_date, rel, "never by the capture host's local date")
        self.assertTrue(rel.endswith(".json.gz"))

    def test_month_spans_follow_the_calendar_not_a_fixed_width(self):
        feb = calendar.timegm((2028, 2, 10, 0, 0, 0, 0, 0, 0))   # a leap February
        lo, hi = ingest.month_span(feb)
        self.assertEqual((hi - lo) // 86400, 29)
        dec = calendar.timegm((2026, 12, 31, 23, 0, 0, 0, 0, 0))
        lo, hi = ingest.month_span(dec)
        self.assertEqual(time.gmtime(hi).tm_year, 2027, "December must roll the year")

    # -- what gets written -------------------------------------------------

    def test_widths_below_the_cadence_are_never_generated(self):
        # choose_bucket() floors at the cadence, so at a 2 s tick nothing resolves to a
        # 1 s bucket. Generating them cost 28% of the whole aggregate for tiles no reader
        # could ask for.
        self.assertIn(1, series.BUCKET_LADDER)
        self.assertNotIn(1, ingest.widths_for(tiles.HOUR, floor_s=2))
        self.assertIn(2, ingest.widths_for(tiles.HOUR, floor_s=2))
        self.assertIn(1, ingest.widths_for(tiles.HOUR, floor_s=1))

    def test_tiles_are_gzipped_and_parse_back(self):
        # Without gzip the derived data is 20x the raw archive it came from. With it, less
        # than 1x. Served as Content-Encoding: gzip so no page code has to inflate it.
        s = self.seed()
        out = self.out()
        written = ingest.run(self.tmp, out, now=self.base + 4 * 3600)
        tile_rels = [r for r, _ in written if r.endswith(".json.gz")]
        self.assertTrue(tile_rels)
        with gzip.open(os.path.join(out, sorted(tile_rels)[0]), "rb") as fh:
            tile = json.load(fh)
        self.assertEqual(tile["v"], tiles.TILE_VERSION)
        self.assertEqual(tile["plan"], s.plan_hash)
        self.assertEqual(len(tile["series"]["plant_ess_soc"]["mean"]), tile["n"])

    def test_gzip_bytes_are_a_function_of_the_contents_only(self):
        # A gzip header carrying the build time would make every re-run of an unchanged
        # tile a new object, defeating S3's ETag and any CDN validator.
        self.seed(hours=1)
        a, b = self.out(), os.path.join(self.tmp, "agg2")
        os.makedirs(b, exist_ok=True)
        ingest.run(self.tmp, a, now=self.base + 4 * 3600)
        time.sleep(1.1)
        ingest.run(self.tmp, b, now=self.base + 4 * 3600)
        checked = 0
        for root, _, files in os.walk(a):
            for f in files:
                if not f.endswith(".json.gz"):
                    continue
                rel = os.path.relpath(os.path.join(root, f), a)
                with open(os.path.join(a, rel), "rb") as fa, \
                        open(os.path.join(b, rel), "rb") as fb:
                    self.assertEqual(fa.read(), fb.read(), rel)
                checked += 1
        self.assertGreater(checked, 0, "nothing was compared")

    def test_a_finished_span_is_immutable_and_a_running_one_is_not(self):
        s = self.seed()
        out = self.out()
        # "now" inside the same UTC day: the hours are done, the day is not.
        written = dict(ingest.run(self.tmp, out, now=self.base + 4 * 3600))
        hours = [r for r in written if "/hour/" in r]
        days = [r for r in written if "/day/" in r]
        self.assertTrue(hours and days)
        for r in hours:
            self.assertEqual(written[r], ingest.IMMUTABLE, r)
        for r in days:
            self.assertEqual(written[r], ingest.FRESH, r)
        # And once the day has closed, its tile is immutable too.
        later = dict(ingest.run(self.tmp, out, now=self.base + 3 * 86400))
        for r in [x for x in later if "/day/" in x]:
            self.assertEqual(later[r], ingest.IMMUTABLE, r)

    def test_an_incomplete_month_gets_no_tile(self):
        # By design: complete months are written once and never re-read, which is what
        # keeps ingest from re-decoding a month of raw every hour. The reader falls back
        # to that month's day tiles.
        self.seed()
        out = self.out()
        written = [r for r, _ in ingest.run(self.tmp, out, now=self.base + 4 * 3600)]
        self.assertEqual([r for r in written if "/month/" in r], [])
        # Two months later the month has closed, so now it exists.
        written = [r for r, _ in ingest.run(self.tmp, out, now=self.base + 70 * 86400)]
        self.assertTrue([r for r in written if "/month/" in r])

    def test_a_span_with_no_records_writes_nothing(self):
        # An absent tile means "no data here", which is the truth. An empty one would cost
        # a request to say the same thing.
        self.seed(hours=1)
        out = self.out()
        before = ingest.write_span(series.newest_series(self.tmp), out, tiles.HOUR,
                                   self.base + 50 * 3600, now=self.base + 100 * 3600)
        self.assertEqual(before, [])

    # -- incremental -------------------------------------------------------

    def test_the_incremental_run_touches_only_what_can_have_changed(self):
        # run() re-reads the whole archive, which is right for a backfill and wrong once
        # an hour -- it would grow to re-decoding a year on every rotation.
        s = self.seed()
        out = self.out()
        full = {r for r, _ in ingest.run(self.tmp, out, now=self.base + 4 * 3600)}
        inc = {r for r, _ in ingest.run_for(self.tmp, out, touched=[self.base],
                                           now=self.base + 4 * 3600)}
        self.assertTrue(inc < full, "the incremental run must be a strict subset")
        hours = {r for r in inc if "/hour/" in r}
        self.assertTrue(hours)
        for r in hours:
            self.assertIn(time.strftime("%Y/%m/%d/%H", time.gmtime(self.base)), r)
        # The documents always move: extent and latest change every rotation.
        self.assertIn(f"plan={s.plan_hash}/{ingest.META}", inc)
        self.assertIn(ingest.INDEX, inc)

    def test_a_rotation_straddling_a_utc_hour_rebuilds_both(self):
        self.seed()
        out = self.out()
        inc = {r for r, _ in ingest.run_for(
            self.tmp, out, touched=[self.base, self.base + 3600],
            now=self.base + 4 * 3600)}
        for ts in (self.base, self.base + 3600):
            stem = time.strftime("%Y/%m/%d/%H", time.gmtime(ts))
            self.assertTrue([r for r in inc if stem in r], stem)

    # -- the documents -----------------------------------------------------

    def test_meta_withholds_identity(self):
        # This file is served over the internet. The serial and the inverter's LAN
        # address are not part of reading a chart.
        s = self.seed(hours=1)
        m = ingest.build_meta(s)
        blob = json.dumps(m)
        self.assertNotIn("TESTSERIAL", blob)
        self.assertNotIn(str(self.manifest.get("host")), blob)
        self.assertIn("withheld", m["device"])
        self.assertEqual(m["device"]["model"], s.manifest["device"]["model"])

    def test_meta_states_the_field_picker_limit_rather_than_implying_none(self):
        # Only day tiles carry the whole catalogue, so the 6 h default cannot chart an
        # arbitrary register. The page has to say so rather than offer a field that comes
        # back absent.
        s = self.seed(hours=1)
        m = ingest.build_meta(s)
        self.assertEqual(m["picker_min_bucket_s"],
                         min(ingest.widths_for(tiles.DAY, s.fast_period_s)))
        self.assertLess(len(m["fine_fields"]), len(m["coarse_fields"]))
        # The page needs both of these to reproduce choose_bucket()'s arithmetic; without
        # target_buckets it hardcoded 900 and, worse, the wrong formula.
        self.assertEqual(m["target_buckets"], series.TARGET_BUCKETS)
        self.assertIn(m["picker_min_bucket_s"], m["bucket_ladder"])

    def test_the_picker_threshold_is_the_width_below_it_not_the_width_itself(self):
        # choose_bucket() rounds UP to the next ladder width, so picker_min_bucket_s is
        # reached once span/TARGET_BUCKETS passes the width BELOW it -- 60 * 900 = 54,000 s
        # = 15 h on this ladder, not 120 * 900 = 108,000 s = 30 h.
        #
        # web/app.js computed it the second way and told people to widen to 30 h when 15
        # would do; 30 h is in fact where the ladder has already moved on to 300 s. This
        # asserts the boundary from both sides so the arithmetic cannot drift back.
        s = self.seed(hours=1)
        m = ingest.build_meta(s)
        minimum = m["picker_min_bucket_s"]
        ladder = m["bucket_ladder"]
        below = ladder[ladder.index(minimum) - 1]
        threshold = below * m["target_buckets"]

        self.assertLess(series.choose_bucket(threshold, s.fast_period_s), minimum,
                        "at exactly the threshold the picker is still shut")
        self.assertGreaterEqual(
            series.choose_bucket(threshold + below, s.fast_period_s), minimum,
            "one bucket past it, the picker opens")
        # And this is precisely what the old formula got wrong. min * target_buckets is the
        # LAST span that still resolves to `min`, not the first: the band is
        # (below * target, min * target]. Quoting its far end as the entry point overstated
        # the requirement by exactly the ladder's step, which here is 2x.
        wrong = minimum * m["target_buckets"]
        self.assertEqual(series.choose_bucket(wrong, s.fast_period_s), minimum,
                         "min * target_buckets is the top of the band, still `min`")
        self.assertGreater(series.choose_bucket(wrong + 1, s.fast_period_s), minimum,
                           "one second past it, the ladder has moved on")
        self.assertEqual(wrong, 2 * threshold,
                         "on this ladder 60 -> 120 is a doubling, so the old message asked "
                         "for twice the window the picker actually needs")

    def test_latest_leaves_the_stall_verdict_to_the_reader(self):
        # series.latest() decides it against a few cadences, which in a document written
        # once an hour is always true -- the page would show a permanent red "the logger
        # may have stopped". An age is only meaningful relative to when it is read.
        s = self.seed(hours=1)
        lt = ingest.latest(s)
        for gone in ("logger_stalled", "record_age_s", "data_age_s", "now"):
            self.assertNotIn(gone, lt)
        self.assertEqual(lt["stall_after_s"], ingest.STALL_AFTER_S)
        self.assertIn("record_ts", lt)
        self.assertIn("data_ts", lt)

    def test_index_names_every_plan_and_which_one_is_current(self):
        # A superseded plan stays listed rather than silently dropped; `current` is the
        # one holding the newest record, the same rule series.newest_series() applies.
        t0 = self.base
        self.write_records("20260814T090000", self.data_records(t0, 10), plan_hash="aaaaaaaa")
        self.write_records("20260814T110000", self.data_records(t0 + 7200, 10),
                           plan_hash="bbbbbbbb")
        idx = ingest.index(series.discover(self.tmp))
        self.assertEqual({p["hash"] for p in idx["plans"]}, {"aaaaaaaa", "bbbbbbbb"})
        self.assertEqual(idx["current"], "bbbbbbbb")

    def test_the_panels_come_from_serve_not_a_second_copy(self):
        # A restated PANELS list is exactly the drift this arrangement exists to avoid: it
        # would show up as a hosted page quietly missing a panel the local one has.
        s = self.seed(hours=1)
        self.assertIs(ingest.build_meta(s)["panels"], serve.PANELS)
        self.assertIs(ingest.build_meta(s)["energy_tiles"], serve.ENERGY_TILES)


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

class HTTPFixture(ViewerFixture):
    """A viewer serving the fixture archive on a loopback port. No test methods:
    subclassing a TestCase that has them runs them again per subclass."""

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

    def fetch(self, path):
        """(status, headers, body)."""
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            # putrequest, not request(): the raw path must reach the server
            # unnormalised so a traversal attempt is actually tested.
            conn.putrequest("GET", path, skip_accept_encoding=True)
            conn.endheaders()
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def get(self, path):
        status, headers, body = self.fetch(path)
        return status, headers.get("Content-Type"), body

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


class TestHTTP(HTTPFixture):
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


class TestPageServerContract(HTTPFixture):
    """What the page asks for, and whether this server answers it.

    web/app.js reaches the outside world through exactly one seam -- getJSON() -- so
    the set of routes it names IS the contract, and a hosted deployment has to compose
    the same answers out of static tiles. Both halves of that are quiet when wrong: a
    renamed route gives a page that boots and then shows nothing, and a route this
    server drops is only noticed by whoever opens it next.

    Derived from the files rather than restated, so adding a fetch to app.js without a
    route in serve.py fails here rather than in a browser.
    """

    def page_routes(self):
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        return set(re.findall(r"'(/api/\w+)'", js))

    def test_every_route_the_page_names_is_one_this_server_answers(self):
        self.seed()
        self.start()
        for route in sorted(self.page_routes()):
            status, _, body = self.get(route)
            self.assertNotEqual(status, 404,
                                f"web/app.js fetches {route}, which serve.py does not "
                                f"route -- the page would boot and then show nothing")
            self.assertEqual(status, 200, f"{route}: {body[:200]}")

    def test_the_three_composable_answers_are_the_ones_a_tile_source_must_supply(self):
        # A hosted deployment has no server: web/tiles.js builds these three out of
        # precomputed objects. Pinning the set here means adding a fourth to app.js
        # fails until the tile source can answer it too.
        self.assertEqual(self.page_routes() & {"/api/meta", "/api/window", "/api/latest"},
                         {"/api/meta", "/api/window", "/api/latest"})

    def test_the_page_fetches_only_through_the_one_seam(self):
        # Two renderers is the failure this whole arrangement exists to avoid, and a
        # stray fetch() is how it would start: it would work on this server and quietly
        # 404 against tiles. Comments are stripped first, since the prose above getJSON()
        # names fetch() to say that nothing else may call it.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            code = "\n".join(re.sub(r"//.*$", "", line)
                             for line in fh.read().split("\n"))
        sites = [m.start() for m in re.finditer(r"\bfetch\(", code)]
        # And in these functions, not others of the same count: the number alone would be
        # satisfied by moving a call rather than removing it.
        #
        # createShare() is the one WRITE the page makes, and it is deliberately not routed
        # through getJSON(). That seam exists so one renderer can read from serve.py or from
        # static tiles, and it is shaped for that: it returns parsed JSON and throws on any
        # failure. A share POST has a body, and it has to tell 401 (sign in again) from 403
        # (signing in cannot help) from 400 (the window has no data), which a thrown Error
        # flattens. A tile source cannot answer it at all -- there is no server -- which is
        # why the card only appears when SIGEN_SOURCE sets `share`.
        enclosing = [re.findall(r"function (\w+)\s*\(", code[:at])[-1] for at in sites]
        self.assertEqual(enclosing, ["getJSON", "createShare", "verifyClosed"],
                         "fetch() belongs in getJSON(), createShare() and the tunnel probe "
                         "only; every READ goes through getJSON()")

    def test_every_asset_the_page_links_is_servable(self):
        with open(os.path.join(HERE, "web", "index.html")) as fh:
            html = fh.read()
        for ref in sorted(set(re.findall(r'(?:src|href)="(/[^"]*)"', html))):
            self.assertIn(ref, serve.STATIC,
                          f"index.html links {ref}, which is not in serve.STATIC")

    def test_the_retired_export_leaves_nothing_behind(self):
        # Deleted deliberately, not left as a dead button: a control that 404s reads as
        # a broken viewer rather than as a feature that moved.
        #
        # `id="share"` came BACK, for the hosted share link -- but the self-contained HTML
        # export it used to drive is still gone, so the snapshot ids and its wording must
        # stay absent. The distinction is the point: one writes a file, the other writes an
        # S3 prefix, and reviving the markup without reviving the export is correct.
        self.seed()
        self.start()
        status, _, _ = self.get("/api/snapshot?hours=1")
        self.assertEqual(status, 404)
        with open(os.path.join(HERE, "web", "index.html")) as fh:
            html = fh.read()
        for gone in ('id="snapshot-note"', 'id="snapshot-dl"', 'id="snapshot-summary"',
                     'id="snapshot-banner"', "Save snapshot", "Save this view"):
            self.assertNotIn(gone, html)

    def test_the_page_derives_the_picker_threshold_from_the_width_below_the_minimum(self):
        # The bug this pins was USER-VISIBLE: app.js computed `min * target_buckets`, so with
        # a 120 s minimum it told people to widen the window to 30 h when 15 h is enough --
        # 30 h is the top of the b120 band, not its start. See ingest.py's header.
        #
        # A source-level assertion because there is no JS harness: the arithmetic lives in
        # JavaScript, and the Python test above pins choose_bucket()'s behaviour without
        # touching the page's copy of the reasoning. Mutating the formula back left that test
        # passing, which is how this gap was found.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            code = "\n".join(re.sub(r"//.*$", "", line) for line in fh.read().split("\n"))
        fn = re.search(r"function pickerNeedsSpanS\(\)\s*\{(.*?)\n\}", code, re.S)
        self.assertIsNotNone(fn, "web/app.js should derive the threshold in one function")
        body = re.sub(r"\s+", " ", fn.group(1))
        self.assertIn("ladder[i - 1] * target", body,
                      "the threshold is the ladder width BELOW picker_min_bucket_s times "
                      "target_buckets, because choose_bucket() rounds up")
        self.assertNotRegex(body, r"\bmin\s*\*\s*target\b",
                            "min * target_buckets is the TOP of the band -- that was the bug")
        self.assertNotRegex(body, r"\*\s*900\b",
                            "target_buckets comes from meta, not a hardcoded 900")

    def test_the_stall_warning_does_not_blame_the_logger_on_a_tiles_source(self):
        # Observed live: a share rendered "no record for 4.5 h when shared -- the logger may
        # have stopped" over an archive whose logger had not missed a tick. What had stopped
        # was the uploader, which was never installed. On a tiles source `record_ts` is the
        # newest record that reached the bucket, and sync.py only advances that on rotation,
        # so the two failures are the SAME symptom and the page cannot tell them apart. It
        # must therefore name both. serve.py reads the archive directly, so there it may
        # still name the logger alone. See FINDINGS 29.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            code = "\n".join(re.sub(r"//.*$", "", line) for line in fh.read().split("\n"))
        fn = re.search(r"function renderFreshness\(\)\s*\{(.*?)\n\}", code, re.S)
        self.assertIsNotNone(fn, "web/app.js should render the freshness pill in one function")
        body = re.sub(r"\s+", " ", fn.group(1))
        stall = re.search(r"logger_stalled\)\s*\{(.*?)\}\s*else", body)
        self.assertIsNotNone(stall, "the stalled branch should be findable")
        self.assertIn("the logger or the uploader may have stopped", stall.group(1),
                      "a tiles source cannot distinguish a stopped logger from a stopped "
                      "uploader, so it must not pick one")
        self.assertIn("SRC.kind === 'server'", stall.group(1),
                      "the wording must branch on the source, not apply everywhere: "
                      "serve.py reads the archive itself and there the logger IS the answer")

    def test_the_share_card_is_hidden_until_a_source_offers_a_share_endpoint(self):
        # serve.py serves this page with no SIGEN_SOURCE, so SRC.share is undefined and the
        # card must stay hidden -- there is no local share endpoint, and a button that
        # cannot work is worse than no button. Only site-stack.ts's /view entry point sets
        # it. The `hidden` attribute is what makes the default safe: app.js reveals the card,
        # it never has to remember to hide it.
        with open(os.path.join(HERE, "web", "index.html")) as fh:
            html = fh.read()
        card = re.search(r'<section[^>]*id="share"[^>]*>', html)
        self.assertIsNotNone(card, "the share card should exist in the one page")
        self.assertIn("hidden", card.group(0),
                      "the share card must default to hidden: serve.py has no share endpoint")
        self.assertIn("data-live-only", card.group(0),
                      "a frozen share cannot re-share itself, so applyFrozenMode() must "
                      "hide this card too")
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            app = fh.read()
        self.assertIn("SRC.share", app,
                      "app.js must gate the card on the source offering an endpoint")


class TestWireFormat(unittest.TestCase):
    """How a value is written, which is not the same question as what it is."""

    def test_an_integral_value_is_written_without_a_decimal_point(self):
        # "241.0" spends two bytes on a point and a zero the page never shows, and a
        # window is tens of thousands of values. The number does not change.
        self.assertEqual(tiles.round_list([241.3, None, 3.5, 0.0, -0.0], 0),
                         [241, None, 4, 0, 0])
        self.assertEqual(tiles.round_list([1.2345, 2.0], 3), [1.234, 2])
        self.assertEqual(json.dumps(tiles.round_list([241.3], 0)), "[241]")

    def test_rounding_still_follows_the_registers_own_resolution(self):
        self.assertEqual(tiles.decimals_for(1000), 3)
        self.assertEqual(tiles.decimals_for(1), 0)
        self.assertEqual(tiles.round_list([3.14159], 2), [3.14])


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
