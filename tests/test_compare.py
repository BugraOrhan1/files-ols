import tempfile
import unittest
from pathlib import Path

from ols_platform.compare import compare_binary_files
from ols_platform.compare import store_comparison
from ols_platform.database import IndexDatabase


class BinaryComparisonTests(unittest.TestCase):
    def test_reports_changed_ranges_and_size_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.bin"
            tuned = root / "tuned.bin"
            original.write_bytes(b"abcdef")
            tuned.write_bytes(b"abXYef!")

            result = compare_binary_files(original, tuned, chunk_size=2)

            self.assertEqual(result.differing_bytes, 3)
            self.assertEqual(
                [(item.start, item.end_exclusive) for item in result.changed_ranges],
                [(2, 4), (6, 7)],
            )
            self.assertFalse(result.exact_match)
            self.assertEqual(original.read_bytes(), b"abcdef")
            self.assertEqual(tuned.read_bytes(), b"abXYef!")

    def test_identical_files_have_no_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.bin"
            tuned = root / "tuned.bin"
            original.write_bytes(b"\x00\x01\x02")
            tuned.write_bytes(b"\x00\x01\x02")

            result = compare_binary_files(original, tuned, chunk_size=1)

            self.assertTrue(result.exact_match)
            self.assertEqual(result.differing_bytes, 0)
            self.assertEqual(result.changed_ranges, ())

    def test_comparison_is_persisted_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.bin"
            tuned = root / "tuned.bin"
            original.write_bytes(b"old")
            tuned.write_bytes(b"new")
            database_path = root / "index.sqlite3"
            database = IndexDatabase(database_path)
            database.close()

            result = compare_binary_files(original, tuned)
            store_comparison(database_path, result)
            store_comparison(database_path, result)

            database = IndexDatabase(database_path)
            try:
                count = database.connection.execute(
                    "SELECT COUNT(*) FROM binary_comparisons"
                ).fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
