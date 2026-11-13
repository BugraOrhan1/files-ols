"""'95% match -> 100%': transferring a known tune to a similar original."""

import random
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tuning_fixtures import MAPS, SWITCH_OFFSET, Variant, make_original, make_pair, make_stage1  # noqa: E402

from ols_platform.tuning.generate import GenerationRejected, available_profiles, generate  # noqa: E402
from ols_platform.tuning.package import build_package, read_package, write_package  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
EXPORTS = REPO / "ols files geextracteert"


def with_other_sw(image: bytes, noise: float = 0.0, seed: int = 1) -> bytes:
    """Same calibration layout, other software string, some changed code bytes."""

    data = bytearray(image)
    pos = data.find(b"SWALPHA-0001")
    data[pos:pos + 12] = b"SWALPHA-0999"
    protected = {i for offset, (c, r, _b, _f) in MAPS.items() for i in range(offset - 64, offset + 2 * c * r + 64)}
    protected |= set(range(SWITCH_OFFSET - 32, SWITCH_OFFSET + 32)) | set(range(0x100, 0x200))
    rng = random.Random(seed)
    for _ in range(int(len(data) * noise)):
        p = rng.randrange(len(data))
        if p not in protected:
            data[p] = rng.randrange(256)
    return bytes(data)


def max_word_deviation(a: bytes, b: bytes) -> int:
    return max(abs(struct.unpack_from("<H", a, i)[0] - struct.unpack_from("<H", b, i)[0])
               for i in range(0, len(a) - 1, 2))


class TransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pair = make_pair(1)
        cls.package = build_package([cls.pair])

    def test_other_software_same_layout_gives_complete_tune(self):
        data = with_other_sw(self.pair.original, noise=0.03)
        result = generate(data, "stage1", self.package)
        self.assertEqual(result.evidence_level, "DONOR_TRANSFER")
        self.assertEqual(result.output, make_stage1(data))
        self.assertEqual(result.identity["coverage"], 1.0)

    def test_moved_maps_with_other_values_are_recalculated(self):
        original = make_original(7, calibration_scale=1.04, map_shift=64)
        data = with_other_sw(original)
        expected = make_stage1(original, map_shift=64)
        expected = with_other_sw(expected)  # same software string as the input
        result = generate(data, "stage1", self.package)
        self.assertEqual(result.evidence_level, "DONOR_TRANSFER")
        self.assertLessEqual(max_word_deviation(result.output, expected), 1)
        self.assertTrue(any(c["method"] == "omgerekend" for c in result.changes))
        changed = {i for i in range(len(data)) if result.output[i] != data[i]}
        allowed = {i for s, e in result.allowed_ranges for i in range(s, e)}
        self.assertTrue(changed <= allowed)

    def test_already_tuned_similar_file_is_refused(self):
        tuned = with_other_sw(make_stage1(self.pair.original))
        with self.assertRaises(GenerationRejected) as ctx:
            generate(tuned, "stage1", self.package)
        self.assertEqual(ctx.exception.code, "already_tuned")

    def test_unrelated_file_is_refused(self):
        data = make_original(3, Variant(ecu="OTHER-ECU9", hw="HWZZ-999999", sw="SWOTHER-777"))
        data = bytes(random.Random(3).randrange(256) if i % 3 else b for i, b in enumerate(data))
        with self.assertRaises(GenerationRejected):
            generate(data, "stage1", self.package)

    def test_incomplete_transfer_respects_minimum_coverage(self):
        data = bytearray(with_other_sw(self.pair.original))
        # The switch byte and its surroundings differ: that change cannot be placed safely.
        data[SWITCH_OFFSET - 30:SWITCH_OFFSET + 30] = bytes(range(1, 61))
        data = bytes(data)
        with self.assertRaises(GenerationRejected) as ctx:
            generate(data, "stage1", self.package)
        self.assertEqual(ctx.exception.code, "transfer_incomplete")
        result = generate(data, "stage1", self.package, min_coverage=0.9)
        self.assertLess(result.identity["coverage"], 1.0)
        self.assertTrue(any("niet geplaatst" in n for n in result.notes))

    def test_profiles_and_fingerprints_survive_package_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_package(self.package, Path(tmp) / "p.olstune")
            package = read_package(path)
            data = with_other_sw(self.pair.original, noise=0.02)
            _f, _v, profiles = available_profiles(data, package)
            self.assertEqual(profiles, ["stage1"])
            self.assertEqual(generate(data, "stage1", package).output, make_stage1(data))


class RealTransferTests(unittest.TestCase):
    """Real BMW and VW exports with moved maps, as between software versions."""

    @classmethod
    def setUpClass(cls):
        from ols_platform.tuning.library import Library
        from ols_platform.tuning.ols_native import NativeOlsReader
        from ols_platform.tuning.package import PackageStore

        cls.tmp = tempfile.TemporaryDirectory()
        store = PackageStore(Path(cls.tmp.name) / "store")
        library = Library(store)
        library.scan({"repo": REPO / "ols files als ols"}, NativeOlsReader(), workers=1)
        library.train()
        library.close()
        cls.package = store.load_active()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def shift(image: bytes, at: int, count: int) -> bytes:
        return image[:at] + bytes(count) + image[at:-count]

    def test_moved_maps_are_found_byte_exact(self):
        cases = [
            ("WinOLS (BMW 7er (Original) - B57D30S0-G1).bin", "WinOLS (BMW 7er (Stage 1) - B57D30S0-G1).bin",
             [(0x100000, 64), (0x130000, 128), (0x1A0000, 32)]),
            ("WinOLS (VW Golf (Original) - SCG0051000000)", "WinOLS (VW Golf (Stage 1) - SCG0051000000)",
             [(0x200000, 512)]),
        ]
        for original_name, tuned_name, shifts in cases:
            original = (EXPORTS / original_name).read_bytes()
            tuned = (EXPORTS / tuned_name).read_bytes()
            for at, count in shifts:
                original = self.shift(original, at, count)
                tuned = self.shift(tuned, at, count)
            result = generate(original, "stage1", self.package)
            self.assertEqual(result.evidence_level, "DONOR_TRANSFER", original_name)
            self.assertEqual(result.output, tuned, original_name)
            with self.assertRaises(GenerationRejected) as ctx:
                generate(tuned, "stage1", self.package)
            self.assertIn(ctx.exception.code, ("already_tuned", "partially_tuned"))


if __name__ == "__main__":
    unittest.main()
