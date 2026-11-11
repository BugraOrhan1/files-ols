import tempfile
import unittest
from pathlib import Path

from ols_platform.database import IndexDatabase
from ols_platform.importer import import_sources
from ols_platform.reader import (
    ParsedMap,
    ParsedVersion,
    ParsedVersionSlot,
    ProjectSnapshot,
    ReadIssue,
)


class FakeReader:
    parser_name = "test-parser"
    parser_version = "test"

    def inspect(self, source):
        if not source.is_file():
            raise FileNotFoundError(source)
        return ProjectSnapshot(
            parser_name="test-parser",
            parser_version=self.parser_version,
            format_version=597,
            metadata={"make": "Example", "model": ""},
            versions=(
                ParsedVersion("Original", "", 0, 2, 100, 8, "", 0),
            ),
            version_slots=(
                ParsedVersionSlot(
                    0, 100, 8, "Original", "", "aabbccdd",
                    "a" * 64, "b" * 64, 4,
                    "present_nonempty_unverified", "named_record",
                ),
                ParsedVersionSlot(
                    1, 108, 8, "", "", "aabbccdd",
                    "c" * 64, "d" * 64, 4,
                    "present_nonempty_unverified", "unresolved",
                ),
            ),
            maps=(
                ParsedMap(
                    "", "Unknown definition", "MAP", "UWORD", 2, 2,
                    4, "", 1.0, 0.0,
                ),
            ),
            issues=(
                ReadIssue("incomplete_version_inventory", "One slot is missing.", "error"),
                ReadIssue("unnamed_maps", "A map has no name."),
            ),
        )


class ImporterTests(unittest.TestCase):
    def test_schema_upgrade_adds_project_detail_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.sqlite3"
            database = IndexDatabase(path)
            database.connection.execute("DROP TABLE project_folders")
            database.connection.execute("DROP TABLE cal_blocks")
            database.connection.execute("UPDATE schema_info SET version=1")
            database.connection.commit()
            database.close()

            database = IndexDatabase(path)
            try:
                version = database.connection.execute(
                    "SELECT version FROM schema_info"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in database.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertEqual(version, 16)
                self.assertIn("project_folders", tables)
                self.assertIn("cal_blocks", tables)
                self.assertIn("project_version_slots", tables)
                self.assertIn("project_diagnostics", tables)
                self.assertIn("reference_artifacts", tables)
                self.assertIn("reference_checks", tables)
                self.assertIn("binary_analysis_runs", tables)
                self.assertIn("binary_slot_candidates", tables)
                self.assertIn("version_match_reviews", tables)
                self.assertIn("reference_check_history", tables)
                self.assertIn("reference_package_runs", tables)
                self.assertIn("export_tasks", tables)
                self.assertIn("export_task_history", tables)
                self.assertIn("inventory_runs", tables)
                self.assertIn("registered_sources", tables)
                self.assertIn("index_queue", tables)
                self.assertIn("source_scan_runs", tables)
                self.assertIn("training_candidates", tables)
                self.assertIn("source_root_slots", tables)
                self.assertIn("file_contents", tables)
                self.assertIn("file_processing", tables)
                self.assertIn("file_locations", tables)
                self.assertIn("source_scan_directories", tables)
                self.assertIn("source_scan_errors", tables)
                self.assertIn("hash_jobs", tables)
                self.assertIn("hash_attempts", tables)
                self.assertIn("hash_runs", tables)
                self.assertIn("hash_worker_state", tables)
                self.assertEqual(
                    [slot["name"] for slot in database.list_source_slots()],
                    ["Database 1", "Database 2", "Database 3", "Database 4", "default"],
                )
                source_columns = {
                    row["name"]
                    for row in database.connection.execute(
                        "PRAGMA table_info(registered_sources)"
                    )
                }
                self.assertIn("last_attempt_at", source_columns)
            finally:
                database.close()

    def test_import_persists_partial_state_and_resume_skips_same_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.ols"
            source.write_bytes(b"real input placeholder")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                first = next(import_sources(
                    sources=[source],
                    reader=FakeReader(),
                    database=database,
                    max_file_bytes=1024,
                    progress=False,
                ))
                second = next(import_sources(
                    sources=[source],
                    reader=FakeReader(),
                    database=database,
                    max_file_bytes=1024,
                    progress=False,
                ))
                self.assertEqual(first.status, "partial")
                self.assertEqual(second.status, "skipped_unchanged")
                self.assertEqual(database.summary()["projects"], 1)
                self.assertEqual(database.summary()["versions_found"], 1)
                self.assertEqual(database.summary()["binary_slots_enumerated"], 2)
                self.assertEqual(database.summary()["unlabeled_binary_slots"], 1)
                self.assertEqual(database.summary()["folders_found"], 0)
                self.assertEqual(database.summary()["cal_blocks_found"], 0)
                self.assertEqual(database.summary()["map_definitions"], 1)
                self.assertEqual(database.summary()["import_errors"], 1)
                self.assertEqual(database.summary()["import_warnings"], 1)
                self.assertEqual(len(database.list_projects(query="Example")), 1)
                self.assertEqual(len(database.list_projects(query="missing")), 0)
                stored = database.get_project(1)
                self.assertEqual(stored["maps"][0]["axes"], [])
                self.assertEqual(stored["version_slots"][1]["name_status"], "unresolved")
                diagnostic = database.get_diagnostic(1)
                self.assertEqual(diagnostic["processing_status"], "partially_processed")
                self.assertEqual(
                    diagnostic["version_inventory"]["enumerated_binary_slots"],
                    2,
                )
                self.assertIsNone(
                    diagnostic["element_counts"]["unsupported_element_count"]
                )
                self.assertEqual(
                    diagnostic["element_counts"]["unsupported_element_count_status"],
                    "not_reported_by_parser",
                )
                self.assertEqual(
                    database.list_diagnostics(processing_status="partially_processed")[0][
                        "unlabeled_binary_slots"
                    ],
                    1,
                )
                self.assertIsNone(database.get_project(999))
                self.assertEqual(
                    database.connection.execute(
                        "SELECT status FROM project_versions"
                    ).fetchone()[0],
                    "named_record_binary_unverified",
                )
                changed_parser = FakeReader()
                changed_parser.parser_version = "test-next"
                third = next(import_sources(
                    sources=[source],
                    reader=changed_parser,
                    database=database,
                    max_file_bytes=1024,
                    progress=False,
                ))
                self.assertEqual(third.status, "partial")
                self.assertEqual(database.summary()["projects"], 1)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT parser_version FROM projects"
                    ).fetchone()[0],
                    "test-next",
                )
                source.write_bytes(b"changed input")
                fourth = next(import_sources(
                    sources=[source],
                    reader=changed_parser,
                    database=database,
                    max_file_bytes=1024,
                    progress=False,
                ))
                self.assertEqual(fourth.status, "partial")
                self.assertEqual(database.summary()["projects"], 1)
            finally:
                database.close()

    def test_oversized_input_is_blocked_before_parser_call(self):
        class NeverCallReader:
            def inspect(self, _source):
                raise AssertionError(
                    f"Reader must not be invoked for oversized file: {_source}"
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "too-large.ols"
            source.write_bytes(b"0123456789")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                result = next(import_sources(
                    sources=[source],
                    reader=NeverCallReader(),
                    database=database,
                    max_file_bytes=5,
                    progress=False,
                ))
                self.assertEqual(result.status, "blocked")
                self.assertEqual(database.summary()["projects"], 0)
                self.assertEqual(database.summary()["import_errors"], 1)
            finally:
                database.close()

    def test_failed_attempt_can_resume_without_duplicate_project(self):
        class FailOnceReader(FakeReader):
            def __init__(self):
                self.failed = False

            def inspect(self, source):
                if not self.failed:
                    self.failed = True
                    raise OSError("temporary read failure")
                return super().inspect(source)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "resume.ols"
            source.write_bytes(b"input")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                reader = FailOnceReader()
                first = next(import_sources(
                    sources=[source], reader=reader, database=database,
                    max_file_bytes=1024, progress=False,
                ))
                self.assertEqual(first.status, "failed")
                self.assertEqual(database.summary()["projects"], 0)
                second = next(import_sources(
                    sources=[source], reader=reader, database=database,
                    max_file_bytes=1024, progress=False,
                ))
                self.assertEqual(second.status, "partial")
                self.assertEqual(database.summary()["projects"], 1)
                self.assertEqual(
                    len(database.list_diagnostics(processing_status="failed")),
                    0,
                )
            finally:
                database.close()

    def test_failed_changed_source_is_visible_until_reimport_succeeds(self):
        class FailingReader(FakeReader):
            def inspect(self, source):
                raise OSError(f"parser unavailable for {source.name}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "changed.ols"
            source.write_bytes(b"first version")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                imported = next(import_sources(
                    sources=[source], reader=FakeReader(), database=database,
                    max_file_bytes=1024, progress=False,
                ))
                self.assertEqual(imported.status, "partial")
                source.write_bytes(b"changed version")
                failed = next(import_sources(
                    sources=[source], reader=FailingReader(), database=database,
                    max_file_bytes=1024, progress=False,
                ))
                self.assertEqual(failed.status, "failed")
                rows = database.list_diagnostics(
                    processing_status="failed",
                    parser_errors=True,
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["latest_import_status"], "failed")
                report = database.get_diagnostic(1)
                self.assertEqual(report["processing_status"], "failed")
                self.assertEqual(report["stored_processing_status"], "partially_processed")
                self.assertTrue(report["latest_import_attempt"]["source_changed"])

                recovered = next(import_sources(
                    sources=[source], reader=FakeReader(), database=database,
                    max_file_bytes=1024, progress=False,
                ))
                self.assertEqual(recovered.status, "partial")
                self.assertEqual(database.summary()["projects"], 1)
                self.assertEqual(
                    database.get_diagnostic(1)["processing_status"],
                    "partially_processed",
                )
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
