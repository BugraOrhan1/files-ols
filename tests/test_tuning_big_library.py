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
        blobs["b2"] = b"other" * 1000
        blobs["a"] = b"replaced"
        self.assertEqual((blobs["a"], blobs["b2"]), (b"replaced", b"other" * 1000))
        blobs["a"] = b"0123456789" * 10_000
        self.assertLess(blobs._end, 8_000)
        self.assertIsNone(blobs.get("b"))
        self.assertEqual(len(blobs), 2)

    def test_package_survives_the_round_trip(self):
        package = build_package([make_pair(1), make_pair(2, scale=0.95)])
        with tempfile.TemporaryDirectory() as tmp:
            path = write_package(package, Path(tmp) / "p.olstune")
            again = read_package(path)
            self.assertEqual(again.package_id, package.package_id)
            for _family, variant in package.variants():
                self.assertEqual(again.load_variant(variant["variant_id"]), package.load_variant(variant["variant_id"]))
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["p.olstune"])

    def library(self, root, count=3):
        from ols_platform.tuning.library import Library, save_pair
        from ols_platform.tuning.package import PackageStore

        library = Library(PackageStore(Path(root)))
        for i in range(1, count + 1):
            pair = make_pair(i, scale=1 - i / 100)
            save_pair(library.root, pair)
            library.db.execute(
                "INSERT INTO pairs(pair_id,source,project_sha256,original_sha256,tuned_sha256,profile,family_key,"
                "first_seen,variant_key,project_name,tuned_version_name) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (pair.pair_id, "s", pair.record["project_sha256"], pair.record["original_sha256"],
                 pair.record["tuned_sha256"], pair.profile, pair.family_key, "", f"v{i}",
                 pair.record.get("project_name", ""), "Stage 1"))
        library.db.commit()
        return library

    def test_parallel_training_gives_the_same_package(self):
        from unittest import mock

        from ols_platform.tuning import library as lib

        ids = []
        for limit in (64, 1):  # in this process, and with worker processes
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(lib, "PARALLEL_VARIANTS", limit):
                library = self.library(tmp)
                ids.append(library.train()["package_id"])
                again = library.train()
                self.assertEqual(again["variants_reused_from_cache"], 3)
                library.close()
        self.assertEqual(ids[0], ids[1])

    def test_current_family_name_reaches_the_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = self.library(tmp, 1)
            library.db.execute("UPDATE pairs SET family_key='EDC17_CP44|65536'")
            library.train()
            package = library.store.load_active()
            library.close()
            self.assertEqual([f["family_key"] for f in package["families"]], ["EDC17CP44|65536"])
            self.assertEqual(package.fingerprints()[0]["family_key"], "EDC17CP44|65536")

    def test_one_damaged_example_does_not_stop_training(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = self.library(tmp)
            victim = sorted((library.root / "pairs").iterdir())[0]
            victim.write_bytes(b"kapot")
            messages = []
            library.train(progress=messages.append)
            package = library.store.load_active()
            library.close()
            self.assertEqual(sum(1 for _ in package.variants()), 2)
            self.assertTrue(any("overgeslagen" in m for m in messages))

    def test_read_ahead_ignores_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            ahead = _ReadAhead(Path(tmp), {"x": "y"})
            ahead.add(["x", "unknown"])
            ahead.add_file(Path(tmp) / "missing")
            ahead.pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
