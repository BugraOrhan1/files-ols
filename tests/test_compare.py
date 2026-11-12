import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_empty_files_and_empty_vs_nonempty_are_reported_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty_a = root / "empty-a.bin"
            empty_b = root / "empty-b.bin"
            nonempty = root / "nonempty.bin"
            empty_a.write_bytes(b"")
            empty_b.write_bytes(b"")
            nonempty.write_bytes(b"data")

            identical = compare_binary_files(empty_a, empty_b, chunk_size=2)
            different = compare_binary_files(empty_a, nonempty, chunk_size=2)

        self.assertTrue(identical.exact_match)
        self.assertEqual(identical.changed_ranges, ())
        self.assertEqual(identical.differing_bytes, 0)
        self.assertFalse(different.exact_match)
        self.assertEqual(different.differing_bytes, 4)
        self.assertEqual(
            [(item.start, item.end_exclusive) for item in different.changed_ranges],
            [(0, 4)],
        )

    def test_changed_ranges_are_stable_across_chunk_sizes_and_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.bin"
            tuned = root / "tuned.bin"
            original_bytes = bytes(range(64))
            tuned_bytes = bytearray(original_bytes)
            tuned_bytes[7:12] = b"\xff" * 5
            tuned_bytes[33] ^= 0xFF
            original.write_bytes(original_bytes)
            tuned.write_bytes(tuned_bytes)

            results = [
                compare_binary_files(original, tuned, chunk_size=size)
                for size in (1, 2, 4, 7, 8, 16, 64)
            ]

        expected = [(7, 12), (33, 34)]
        for result in results:
            self.assertEqual(
                [(item.start, item.end_exclusive) for item in result.changed_ranges],
                expected,
            )
            self.assertEqual(result.differing_bytes, 6)
        self.assertTrue(all(result == results[0] for result in results[1:]))

    def test_streaming_reads_never_request_more_than_chunk_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.bin"
            tuned = root / "tuned.bin"
            original.write_bytes(b"a" * 10_000)
            tuned.write_bytes(b"a" * 9_999 + b"b")
            chunk_size = 257
            read_sizes = []
            real_open = Path.open

            class ReadTracker:
                def __init__(self, file):
                    self._file = file

                def read(self, size=-1):
                    read_sizes.append(size)
                    return self._file.read(size)

                def __enter__(self):
                    self._file.__enter__()
                    return self

                def __exit__(self, exc_type, exc_value, traceback):
                    return self._file.__exit__(exc_type, exc_value, traceback)

                def __getattr__(self, name):
                    return getattr(self._file, name)

            def tracked_open(path, *args, **kwargs):
                return ReadTracker(real_open(path, *args, **kwargs))

            with patch.object(Path, "open", tracked_open):
                result = compare_binary_files(
                    original, tuned, chunk_size=chunk_size
                )

        self.assertFalse(result.exact_match)
        self.assertTrue(read_sizes)
        self.assertTrue(all(0 < size <= chunk_size for size in read_sizes))

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
