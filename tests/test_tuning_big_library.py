"""Package building for large libraries: compressed in memory, streamed to disk."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tuning_fixtures import make_pair  # noqa: E402

from ols_platform.tuning.library import _ReadAhead  # noqa: E402
from ols_platform.tuning.package import _CompressedBlobs, build_package, read_package, write_package  # noqa: E402


class BigLibraryTests(unittest.TestCase):
    def test_blobs_are_compressed_and_come_back_identical(self):
        blobs = _CompressedBlobs()
        blobs["a"] = b"0123456789" * 10_000
        self.assertEqual(blobs["a"], b"0123456789" * 10_000)
        self.assertLess(len(blobs._data["a"]), 5_000)
        self.assertIsNone(blobs.get("b"))
        self.assertEqual(len(blobs), 1)

    def test_package_survives_the_round_trip(self):
        package = build_package([make_pair(1), make_pair(2, scale=0.95)])
        with tempfile.TemporaryDirectory() as tmp:
            path = write_package(package, Path(tmp) / "p.olstune")
            again = read_package(path)
            self.assertEqual(again.package_id, package.package_id)
            for _family, variant in package.variants():
                self.assertEqual(again.load_variant(variant["variant_id"]), package.load_variant(variant["variant_id"]))
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["p.olstune"])

    def test_read_ahead_ignores_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            ahead = _ReadAhead(Path(tmp), {"x": "y"})
            ahead.add(["x", "unknown"])
            ahead.add_file(Path(tmp) / "missing")
            ahead.pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
