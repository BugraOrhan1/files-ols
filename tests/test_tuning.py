import hashlib
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tuning_fixtures import (  # noqa: E402
    MAPS, SIZE, SWITCH_OFFSET, FakeReader, Variant, make_original, make_pair, make_stage1,
    metadata, revision_tag_bytes, write_fake_project,
)

from ols_platform.tuning import export as export_module  # noqa: E402
from ols_platform.tuning.evaluate import compare_to_reference, donor_diff_baseline, evaluate_holdout  # noqa: E402
from ols_platform.tuning.export import ExportBlocked, export_result  # noqa: E402
from ols_platform.tuning.generate import GenerationRejected, generate  # noqa: E402
from ols_platform.tuning.identity import classify_input  # noqa: E402
from ols_platform.tuning.library import Library, holdout_closure  # noqa: E402
from ols_platform.tuning.ols_versions import classify_profile, parse_revision_tag  # noqa: E402
from ols_platform.tuning.package import (  # noqa: E402
    PackageStore, build_package, read_package, write_package,
)

READY = {"SYNECU-77|65536": {"contract": "FLASH_TOOL_CORRECTS_ON_WRITE", "evidence": "test"}}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RevisionTagTests(unittest.TestCase):
    def test_parses_real_tag_layout_with_commas_in_notes(self):
        raw = b"\x00" * 10 + revision_tag_bytes(
            [("EGR OFF", "CS-, OLS280 2.50"), ("Stage 2", "CS+, OLS280 2.50")]
        ) + b"\x02\x00"
        tag = parse_revision_tag(raw)
        self.assertEqual([v.name for v in tag.versions], ["Original", "EGR OFF", "Stage 2"])
        self.assertEqual(tag.versions[1].checksum_note, "CS-, OLS280 2.50")
        self.assertEqual(tag.declared_count, 2)

    def test_profile_classification_covers_all_tuning_types(self):
        cases = {
            "Stage 1": "stage1", "Stage1": "stage1", "STAGE 1": "stage1", "Stufe 1": "stage1", "St1": "stage1",
            "Stage 1 (450PS)": "stage1", "Stage 1 450hp": "stage1", "Stage 2": "stage2",
            "Stage 1 + P&B": "stage1+pop_bang", "stage 1 = Vmax 280Km/h": "stage1+vmax",
            "Stage 1 EGR OFF": "stage1+egr_off", "EGR OFF": "egr_off", "P&B": "pop_bang",
            "DPF OFF + Adblue OFF": "dpf_off+adblue_off", "Swiftec Vmax Off": "vmax",
            "Stage 3 DPF EGR OFF": "stage3+egr_off+dpf_off", "Original": "original", "Daten": "other",
        }
        for label, expected in cases.items():
            self.assertEqual(classify_profile(label), expected, label)


class LearnAndGenerateTests(unittest.TestCase):
    def setUp(self):
        self.pairs = [make_pair(s, scale=sc) for s, sc in [(1, 1.0), (2, 0.95), (3, 1.06)]]
        self.package = build_package(self.pairs)

    def test_known_pair_reproduces_byte_for_byte(self):
        pair = make_pair(5)
        package = build_package([pair])
        result = generate(pair.original, "stage1", package)
        self.assertEqual(result.output, pair.tuned)
        self.assertEqual({c["method"] for c in result.changes}, {"bekend paar"})

    def test_blind_project_beats_baselines_and_stays_inside_regions(self):
        original = make_original(42, calibration_scale=1.02)
        reference = make_stage1(original)
        result = generate(original, "stage1", self.package)
        learned = compare_to_reference(original, result.output, reference, result.allowed_ranges)
        donor = compare_to_reference(
            original, donor_diff_baseline(original, self.pairs[0].original, self.pairs[0].tuned), reference
        )
        self.assertEqual(learned["changes_outside_allowed"], 0)
        # Rounding may leave a single low byte unchanged; word deviation covers it.
        self.assertLessEqual(learned["missed_changes"], 2)
        self.assertLessEqual(learned["unexpected_changes"], 2)
        self.assertLessEqual(learned["word_deviation_raw"]["little"]["max_abs"], 1)
        self.assertGreater(donor["word_deviation_raw"]["little"]["max_abs"], 10)
        report = evaluate_holdout(self.package, original, reference,
                                  (self.pairs[0].original, self.pairs[0].tuned))
        self.assertEqual(report["generation"]["status"], "GENERATED")

    def test_package_is_reproducible_and_tamper_evident(self):
        again = build_package(list(reversed(self.pairs)))
        self.assertEqual(self.package.package_id, again.package_id)
        original = make_original(42, calibration_scale=1.02)
        self.assertEqual(generate(original, "stage1", self.package).output,
                         generate(original, "stage1", again).output)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_package(self.package, Path(tmp) / "p.olstune")
            self.assertEqual(read_package(path).package_id, self.package.package_id)
            self.assertEqual(generate(original, "stage1", read_package(path)).output,
                             generate(original, "stage1", self.package).output)
            import zipfile

            bad = Path(tmp) / "bad.olstune"
            with zipfile.ZipFile(path) as source, zipfile.ZipFile(bad, "w") as target:
                for item in source.namelist():
                    data = source.read(item)
                    if item == "manifest.json":
                        manifest = json.loads(data)
                        manifest["settings"]["limit_margin"] = 0.9
                        data = json.dumps(manifest).encode()
                    target.writestr(item, data)
            with self.assertRaisesRegex(ValueError, "beschadigd"):
                read_package(bad)
            bad_variant = Path(tmp) / "bad-variant.olstune"
            with zipfile.ZipFile(path) as source, zipfile.ZipFile(bad_variant, "w") as target:
                for item in source.namelist():
                    data = source.read(item)
                    if item.startswith("variants/"):
                        data = data.replace(b'"stage1"', b'"stage9"', 1)
                    target.writestr(item, data)
            with self.assertRaisesRegex(ValueError, "beschadigd"):
                generate(original, "stage1", read_package(bad_variant))

    def test_coherent_changes_are_learned_with_support(self):
        variant_id = self.package["families"][0]["variants"][0]["variant_id"]
        regions = self.package.load_variant(variant_id)["profiles"]["stage1"]["regions"]
        starts = {r["start"] for r in regions}
        for offset in MAPS:
            self.assertTrue(any(offset <= s < offset + 200 for s in starts))
        self.assertIn(SWITCH_OFFSET, starts)
        self.assertTrue(all(r["support"] == 1.0 for r in regions))

    def assertRejected(self, data, code, package=None):
        with self.assertRaises(GenerationRejected) as ctx:
            generate(data, "stage1", package or self.package)
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)
        self.assertTrue(ctx.exception.message)
        return ctx.exception

    def test_rejections_name_the_cause(self):
        base = make_original(42)
        self.assertRejected(make_original(7, Variant(sw="SWBETA-0002")), "software_variant_not_validated")
        # Other hardware with otherwise identical bytes: transferred, clearly marked.
        other_hw = make_original(7, Variant(hw="HW9999-0001"))
        transferred = generate(other_hw, "stage1", self.package)
        self.assertEqual(transferred.evidence_level, "DONOR_TRANSFER")
        expected = make_stage1(other_hw)
        self.assertLessEqual(max(abs(struct.unpack_from("<H", transferred.output, i)[0]
                                     - struct.unpack_from("<H", expected, i)[0])
                                 for i in range(0, len(expected) - 1, 2)), 1)
        self.assertRejected(make_original(7, Variant(ecu="OTHERECU-1", hw="HWX-123456", sw="SWX-999999")),
                            "ecu_not_recognized")
        self.assertRejected(make_stage1(base), "already_tuned")
        half = bytearray(base)
        tuned = make_stage1(base)
        first = min(MAPS)
        half[first:first + 128] = tuned[first:first + 128]
        self.assertRejected(bytes(half), "partially_tuned")
        self.assertRejected(base[:-1024], "unknown_segment_layout")
        self.assertRejected(b"", "input_empty")
        self.assertRejected(os.urandom(SIZE), "input_encrypted")
        self.assertRejected(b"PK\x03\x04" + base[4:], "input_archive")
        # Maps moved by 2 bytes: found again and transferred.
        moved = make_original(42, map_shift=2)
        moved_result = generate(moved, "stage1", self.package)
        self.assertEqual(moved_result.evidence_level, "DONOR_TRANSFER")
        expected_moved = make_stage1(moved, map_shift=2)
        self.assertLessEqual(max(abs(struct.unpack_from("<H", moved_result.output, i)[0]
                                     - struct.unpack_from("<H", expected_moved, i)[0])
                                 for i in range(0, len(moved) - 1, 2)), 1)
        hot = bytearray(base)
        struct.pack_into("<H", hot, first, 60000)
        self.assertRejected(bytes(hot), "cell_out_of_range")
        patched = bytearray(base)
        patched[SWITCH_OFFSET] = 0x7F
        self.assertRejected(bytes(patched), "patch_region_mismatch")

    def test_other_modification_zone_blocks_mixed_input(self):
        base = make_original(1)
        other = bytearray(base)
        other[0x7800:0x7810] = bytes(range(0xA0, 0xB0))
        mod = make_pair(1, profile="other")
        mod.tuned = bytes(other)
        mod.record = dict(mod.record, tuned_version_name="P&B")
        package = build_package(self.pairs, other_pairs=[mod])
        mixed = bytearray(make_original(42))
        mixed[0x7800:0x7810] = bytes(range(0xA0, 0xB0))
        error = self.assertRejected(bytes(mixed), "other_modification_present", package)
        self.assertIn("P&B", error.message)

    def test_mixed_profile_is_not_available(self):
        self.assertRaises(GenerationRejected, generate, make_original(42), "stage2", self.package)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Klant mapje é ü"
        self.root.mkdir()
        self.package = build_package([make_pair(s, scale=sc) for s, sc in [(1, 1.0), (2, 0.95)]])
        self.original_path = self.root / "origineel ÄÖ.bin"
        self.original_path.write_bytes(make_original(42, calibration_scale=1.01))
        self.original_hash = sha(self.original_path.read_bytes())
        self.out = self.root / "uitvoer map"

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, contracts=READY):
        return generate(self.original_path.read_bytes(), "stage1", self.package, contracts)

    def leftovers(self):
        return [p.name for p in self.out.iterdir() if p.name.startswith(".tune-")] if self.out.exists() else []

    def test_unknown_integrity_blocks_export(self):
        with self.assertRaises(ExportBlocked) as ctx:
            export_result(self.result({}), self.original_path, self.out, "x.bin", self.package)
        self.assertEqual(ctx.exception.code, "integrity_unknown")
        self.assertFalse((self.out / "x.bin").exists())

    def test_export_writes_verified_file_and_report_without_overwrite(self):
        result = self.result()
        final, report = export_result(result, self.original_path, self.out, "stage1 ü.bin", self.package)
        self.assertEqual(sha(final.read_bytes()), result.output_sha256)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["input"]["sha256"], self.original_hash)
        self.assertEqual(data["output"]["sha256"], result.output_sha256)
        self.assertEqual(data["package"]["package_id"], self.package.package_id)
        self.assertTrue(data["verification"]["passed"])
        self.assertEqual(data["verification"]["changes_outside_allowed_regions"], 0)
        self.assertTrue(data["changed_regions"])
        with self.assertRaises(ExportBlocked) as ctx:
            export_result(result, self.original_path, self.out, "stage1 ü.bin", self.package)
        self.assertEqual(ctx.exception.code, "exists")
        with self.assertRaises(ExportBlocked):
            export_result(result, self.original_path, self.root, self.original_path.name, self.package)
        self.assertEqual(sha(self.original_path.read_bytes()), self.original_hash)
        self.assertEqual(self.leftovers(), [])

    def test_two_simultaneous_exports_yield_one_file(self):
        result = self.result()
        outcomes = []

        def run():
            try:
                export_result(result, self.original_path, self.out, "same.bin", self.package)
                outcomes.append("ok")
            except ExportBlocked as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), ["exists", "ok"])
        self.assertEqual(self.leftovers(), [])

    def test_full_disk_leaves_no_partial_bin(self):
        result = self.result()
        real_fdopen = os.fdopen

        class Full:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

            def write(self, data):
                self.handle.write(data[: len(data) // 2])
                raise OSError(28, "No space left on device")

        with mock.patch.object(export_module.os, "fdopen", lambda fd, mode: Full(real_fdopen(fd, mode))):
            with self.assertRaises(ExportBlocked) as ctx:
                export_result(result, self.original_path, self.out, "vol.bin", self.package)
        self.assertEqual(ctx.exception.code, "write_failed")
        self.assertFalse((self.out / "vol.bin").exists())
        self.assertEqual(self.leftovers(), [])

    def test_filesystem_without_hard_links_still_never_overwrites(self):
        result = self.result()
        with mock.patch.object(export_module.os, "link", side_effect=OSError(95, "Operation not supported")):
            final, report = export_result(result, self.original_path, self.out, "usb.bin", self.package)
            self.assertEqual(sha(final.read_bytes()), result.output_sha256)
            self.assertTrue(report.exists())
            with self.assertRaises(ExportBlocked) as ctx:
                export_result(result, self.original_path, self.out, "usb.bin", self.package)
        self.assertEqual(ctx.exception.code, "exists")
        self.assertEqual(self.leftovers(), [])

    def test_changed_original_is_refused(self):
        result = self.result()
        self.original_path.write_bytes(make_original(43))
        with self.assertRaises(ExportBlocked) as ctx:
            export_result(result, self.original_path, self.out, "y.bin", self.package)
        self.assertEqual(ctx.exception.code, "input_changed")


class InputClassificationTests(unittest.TestCase):
    def test_ols_project_and_text_are_not_reads(self):
        self.assertEqual(classify_input(b"\x0b\x00\x00\x00WinOLS File\x00" + b"\x00" * 100).kind, "ols_project")
        self.assertEqual(classify_input(b"a;b;c\n1;2;3\n" * 1000).kind, "text")
        self.assertEqual(classify_input(make_original(1)).kind, "raw_image")


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "Database 1"
        self.source.mkdir()
        self.reader = FakeReader()
        self.store = PackageStore(self.root / "store")

    def tearDown(self):
        self.tmp.cleanup()

    def add_project(self, name, seed, scale=1.0, extra=None, folder=None):
        original = make_original(seed, calibration_scale=scale)
        images = [original, make_stage1(original)]
        names = [("Stage 1", "NOCS")]
        if extra:
            images.append(extra(original))
            names.append(("Stage 1 + P&B", "NOCS"))
        path = (folder or self.source) / name
        layout = write_fake_project(path, images, names)
        self.reader.register(path, layout, metadata())
        return path, original

    def test_scan_train_resume_and_report_new_project(self):
        for i, scale in enumerate([1.0, 0.96, 1.05]):
            self.add_project(f"proj {i}.ols", i + 1, scale)
        library = Library(self.store)
        stop = threading.Event()
        first = library.scan({"Database 1": self.source}, self.reader, max_files=2, retry_delay=0)
        self.assertEqual(first["sources"]["Database 1"]["status"], "batch_limit_reached")
        second = library.scan({"Database 1": self.source}, self.reader, retry_delay=0)
        self.assertEqual(second["sources"]["Database 1"]["skipped_unchanged"], 2)
        self.assertEqual(second["sources"]["Database 1"]["new_pairs"], 1)
        self.assertEqual(len(library.all_pairs()), 3)
        rows = library.db.execute("SELECT COUNT(*), COUNT(DISTINCT path) FROM files").fetchone()
        self.assertEqual(rows[0], rows[1])
        trained = library.train()
        self.assertEqual(trained["pairs_used"], 3)
        first_package = trained["package_id"]
        again = library.train()
        self.assertEqual(again["package_id"], first_package)  # reproducible
        self.add_project("nieuw ü.ols", 9, 1.02)
        library.scan({"Database 1": self.source}, self.reader, retry_delay=0)
        third = library.train()
        self.assertEqual(len(third["pairs_added_since_previous"]), 1)
        self.assertEqual(third["previous_package_id"], first_package)
        self.assertEqual(self.store.rollback(), first_package)
        stop.set()
        interrupted = library.scan({"Database 1": self.source}, self.reader, stop=stop)
        self.assertEqual(interrupted["sources"]["Database 1"]["status"], "interrupted")
        library.close()

    def test_network_failure_is_retried_and_never_drops_projects(self):
        path, _ = self.add_project("flaky.ols", 1)
        library = Library(self.store)
        self.reader.fail(path, 1)
        ok = library.scan({"Database 1": self.source}, self.reader, retry_delay=0)
        self.assertEqual(ok["sources"]["Database 1"]["confirmed_pairs"], 1)
        path2, _ = self.add_project("down.ols", 2)
        self.reader.fail(path2, 10)
        failed = library.scan({"Database 1": self.source}, self.reader, retry_delay=0, retries=2)
        self.assertEqual(failed["sources"]["Database 1"]["retry_pending"], 1)
        status = library.db.execute("SELECT status FROM files WHERE path=?", (str(path2),)).fetchone()[0]
        self.assertEqual(status, "retry_pending")
        self.reader.fail(path2, 0)
        recovered = library.scan({"Database 1": self.source}, self.reader, retry_delay=0)
        self.assertEqual(recovered["sources"]["Database 1"]["new_pairs"], 1)
        missing = library.scan({"Database 9": self.root / "bestaat niet"}, self.reader)
        self.assertTrue(missing["sources"]["Database 9"]["status"].startswith("unavailable"))
        self.assertEqual(len(library.all_pairs()), 2)  # nothing lost
        library.close()

    def test_combined_versions_become_their_own_profile(self):
        def pnb(original):
            image = bytearray(make_stage1(original))
            image[0x7800:0x7804] = b"\xde\xad\xbe\xef"
            return bytes(image)

        self.add_project("p.ols", 1, extra=pnb)
        library = Library(self.store)
        library.scan({"Database 1": self.source}, self.reader)
        library.train()
        package = self.store.load_active()
        self.assertEqual(sorted(s["profile"] for s in package["training_selection"]),
                         ["stage1", "stage1+pop_bang"])
        variant_id = package["families"][0]["variants"][0]["variant_id"]
        blob = package.load_variant(variant_id)
        self.assertEqual(sorted(blob["profiles"]), ["stage1", "stage1+pop_bang"])
        original = make_original(1)
        self.assertEqual(generate(original, "stage1+pop_bang", package).output, pnb(original))
        self.assertEqual(generate(original, "stage1", package).output, make_stage1(original))
        with self.assertRaises(GenerationRejected) as ctx:
            generate(pnb(original), "stage1", package)
        self.assertIn(ctx.exception.code, ("already_tuned", "other_modification_present"))
        library.close()

    def test_holdout_closure_follows_duplicates(self):
        a = make_pair(1)
        dup = make_pair(1)
        dup.record = dict(dup.record, project_sha256="copy-of-proj")
        dup.pair_id = "dup"
        other = make_pair(2)
        excluded = holdout_closure([a, dup, other], ["proj0001"])
        self.assertEqual(excluded, {a.pair_id, "dup"})


PY_OLS = os.environ.get("PY_OLS_PATH")
REPO = Path(__file__).resolve().parents[1]
EXPORTS = REPO / "ols files geextracteert"


class RealSampleReproductionTests(unittest.TestCase):
    """Uses the built-in reader: runs everywhere the repository samples exist."""

    @classmethod
    def setUpClass(cls):
        from ols_platform.tuning.ols_native import NativeOlsReader

        cls.tmp = tempfile.TemporaryDirectory()
        store = PackageStore(Path(cls.tmp.name) / "store")
        library = Library(store)
        cls.before = {p.name: sha(p.read_bytes()) for p in (REPO / "ols files als ols").glob("*.ols")}
        cls.scan = library.scan({"repo": REPO / "ols files als ols"}, NativeOlsReader(),
                                export_dirs=[EXPORTS])
        cls.training = library.train()
        library.close()
        cls.package = store.load_active()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_confirmed_pairs_are_independently_export_matched(self):
        selection = [s for s in self.package["training_selection"] if s["profile"] == "stage1"]
        self.assertEqual(sorted(s["ecu_type"] for s in selection), ["MDG1_MD1CP002", "SIMOS18.10"])
        for item in selection:
            self.assertIn("original_matches_independent_winols_export_sha256", item["evidence"])
            self.assertIn("tuned_matches_independent_winols_export_sha256", item["evidence"])
            self.assertEqual(item["checksum_state"], "NOT_CORRECTED_IN_REFERENCE")

    def test_known_pairs_reproduce_byte_for_byte_from_exports(self):
        for original, tuned in [
            ("WinOLS (BMW 7er (Original) - B57D30S0-G1).bin", "WinOLS (BMW 7er (Stage 1) - B57D30S0-G1).bin"),
            ("WinOLS (VW Golf (Original) - SCG0051000000)", "WinOLS (VW Golf (Stage 1) - SCG0051000000)"),
        ]:
            data = (EXPORTS / original).read_bytes()
            result = generate(data, "stage1", self.package)
            self.assertEqual(result.output, (EXPORTS / tuned).read_bytes(), original)
            self.assertEqual(result.evidence_level, "KNOWN_PAIR")
            # Same original with a different byte outside the tuned areas
            # (e.g. VIN/immobiliser data) still yields the known Stage 1.
            changed = set()
            for c in result.changes:
                changed.update(range(c["start"], c["end"]))
            spot = next(i for i in range(len(data) // 2, len(data)) if i not in changed and data[i] != 0x55)
            variant = bytearray(data)
            variant[spot] = 0x55
            other = generate(bytes(variant), "stage1", self.package)
            self.assertEqual(other.evidence_level, "KNOWN_PAIR")
            self.assertFalse(other.identity["original_sha256_equal"])
            self.assertEqual(other.output[spot], 0x55)

    def test_real_unsuitable_inputs_are_rejected(self):
        cases = {
            "WinOLS (BMW 7er (Stage 1) - B57D30S0-G1).bin": "already_tuned",
            "WinOLS (VW Golf (P&B) - SCG0051000000)": "other_modification_present",
            "WinOLS (VW Golf (Stage 1) - SCG0051000000)": "already_tuned",
            "WinOLS (Audi A8 (Original) - 522813).DTF": "profile_not_available",
        }
        for name, code in cases.items():
            with self.assertRaises(GenerationRejected) as ctx:
                generate((EXPORTS / name).read_bytes(), "stage1", self.package)
            self.assertEqual(ctx.exception.code, code, name)

    def test_sources_stay_unchanged(self):
        after = {p.name: sha(p.read_bytes()) for p in (REPO / "ols files als ols").glob("*.ols")}
        self.assertEqual(self.before, after)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(PY_OLS and Path(PY_OLS).exists(), "PY_OLS_PATH niet gezet (py-ols checkout nodig)")
class NativeReaderParityTests(unittest.TestCase):
    def test_native_reader_matches_py_ols_on_all_samples(self):
        from ols_platform.reader import PyOlsReader
        from ols_platform.tuning.ols_native import NativeOlsReader

        py, native = PyOlsReader(Path(PY_OLS)), NativeOlsReader()
        for path in sorted((REPO / "ols files als ols").glob("*.ols")):
            a, b = py.inspect(path), native.inspect(path)
            self.assertEqual({k: v for k, v in a.metadata.items() if v},
                             {k: v for k, v in b.metadata.items() if v}, path.name)
            self.assertEqual([(s.slot_index, s.blob_offset, s.blob_size) for s in a.version_slots],
                             [(s.slot_index, s.blob_offset, s.blob_size) for s in b.version_slots], path.name)


class ScaleBehaviourTests(unittest.TestCase):
    def test_software_variants_are_learned_separately(self):
        alpha = [make_pair(s) for s in (1, 2)]
        beta_variant = Variant(sw="SWBETA-0002")
        beta = [make_pair(s, beta_variant) for s in (3, 4)]
        package = build_package(alpha + beta)
        self.assertEqual(len(package["families"]), 1)
        self.assertEqual(len(package["families"][0]["variants"]), 2)
        for variant, seed in ((Variant(), 9), (beta_variant, 10)):
            original = make_original(seed, variant, calibration_scale=1.01)
            result = generate(original, "stage1", package)
            self.assertIn(variant.sw, result.variant_key)

    def test_most_common_tune_wins_for_a_known_original(self):
        pair = make_pair(1)
        other = make_pair(1)
        alt = bytearray(other.tuned)
        alt[SWITCH_OFFSET] = 0x02
        other.tuned = bytes(alt)
        other.pair_id = "zz-alternative"
        other.record = dict(other.record, tuned_sha256=sha(other.tuned))
        twin = make_pair(1)
        twin.pair_id = "aa-duplicate-project"
        twin.record = dict(twin.record, project_sha256="another-project")
        package = build_package([pair, other, twin])
        result = generate(pair.original, "stage1", package)
        self.assertEqual(result.output, pair.tuned)
        self.assertEqual(result.evidence_level, "KNOWN_PAIR")
        self.assertTrue(any("andere varianten" in n for n in result.notes))


class AllProfilesRealSampleTests(unittest.TestCase):
    """Every WinOLS version export in the repository is reproduced exactly."""

    def test_every_exported_version_is_reproduced(self):
        from ols_platform.tuning.ols_native import NativeOlsReader

        with tempfile.TemporaryDirectory() as tmp:
            store = PackageStore(Path(tmp) / "store")
            library = Library(store)
            library.scan({"repo": REPO / "ols files als ols"}, NativeOlsReader(), workers=2)
            library.train()
            library.close()
            package = store.load_active()
            checked = self._check_all(package)
        self.assertEqual(checked, 10)

    def _check_all(self, package):
        import re

        from ols_platform.tuning.generate import available_profiles

        exports = sorted(EXPORTS.iterdir())
        checked = 0
        for original in exports:
            match = re.match(r"WinOLS \((.+?) \((.+)\) - ", original.name)
            if match.group(2) != "Original":
                continue
            data = original.read_bytes()
            _f, _v, profiles = available_profiles(data, package)
            for tuned in exports:
                other = re.match(r"WinOLS \((.+?) \((.+)\) - ", tuned.name)
                if other.group(1) != match.group(1) or other.group(2) == "Original":
                    continue
                profile = classify_profile(other.group(2).replace("Km-h", "Km/h"))
                self.assertIn(profile, profiles, tuned.name)
                self.assertEqual(generate(data, profile, package).output, tuned.read_bytes(), tuned.name)
                checked += 1
        return checked
