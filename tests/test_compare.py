import json
import tempfile
import unittest
from pathlib import Path

from ols_platform.cli import main
from ols_platform.compare import byte_similarity, compare_binary_files
from ols_platform.compare import store_comparison
from ols_platform.database import IndexDatabase


class BinaryComparisonTests(unittest.TestCase):
    def test_cli_no_store_compares_without_creating_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.bin"
            tuned = root / "tuned.bin"
            database = root / "must-not-exist.sqlite3"
            output = root / "report.json"
            original.write_bytes(b"abcd")
            tuned.write_bytes(b"abXd")

            exit_code = main(
                [
                    "--database",
                    str(database),
                    "compare",
                    str(original),
                    str(tuned),
                    "--no-store",
                    "--output",
                    str(output),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertFalse(database.exists())
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["differing_bytes"], 1)
            self.assertFalse(report["exact_match"])
            with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
                main(
                    [
                        "--database",
                        str(database),
                        "compare",
                        str(original),
                        str(tuned),
                        "--no-store",
                        "--output",
                        str(database),
                    ]
                )
            self.assertFalse(database.exists())

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

    def test_byte_similarity_threshold_and_incomparable_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.dat"
            second = root / "second.dat"
            first.write_bytes(b"A" * 1000)
            for matches, expected in ((949, False), (950, True), (951, True)):
                second.write_bytes(b"A" * matches + b"B" * (1000 - matches))
                comparison = compare_binary_files(first, second)
                result = byte_similarity(comparison)
                self.assertEqual(result["matching_bytes"], matches)
                self.assertEqual(result["compared_bytes"], 1000)
                self.assertEqual(result["similarity_percent"], matches / 10)
                self.assertEqual(result["meets_threshold"], expected)
                self.assertFalse(byte_similarity(comparison, 96)["meets_threshold"])
            second.write_bytes(b"A" * 999)
            self.assertIsNone(byte_similarity(compare_binary_files(first, second))["meets_threshold"])
            first.write_bytes(b"")
            second.write_bytes(b"")
            self.assertEqual(byte_similarity(compare_binary_files(first, second))["status"], "NOT_COMPARABLE")
            for invalid in (-1, 101, float("nan"), True, "95"):
                with self.assertRaises(ValueError):
                    byte_similarity(compare_binary_files(first, second), invalid)

    def test_cli_byte_threshold_does_not_create_database_on_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.dat"
            second = root / "second.dat"
            first.write_bytes(b"A" * 100)
            second.write_bytes(b"A" * 95 + b"B" * 5)
            database = root / "index.sqlite3"
            output = root / "result.json"
            self.assertEqual(main([
                "--database", str(database), "compare", str(first), str(second),
                "--no-store", "--byte-threshold", "95", "--output", str(output),
            ]), 0)
            similarity = json.loads(output.read_text(encoding="utf-8"))["byte_similarity"]
            self.assertTrue(similarity["meets_threshold"])
            self.assertEqual(similarity["similarity_percent"], 95)
            with self.assertRaises(ValueError):
                main(["--database", str(database), "compare", str(first), str(second),
                      "--byte-threshold", "nan"])
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
