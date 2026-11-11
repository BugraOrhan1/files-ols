import json
import tempfile
import unittest
from pathlib import Path

from ols_platform.database import IndexDatabase
from ols_platform.export_queue import plan_export_tasks, write_manual_export_batch
from ols_platform.importer import import_sources, stream_sha256
from ols_platform.inventory import scan_inventory
from ols_platform.project_matching import compare_projects, find_project_matches
from ols_platform.reader import (
    ParsedMap,
    ParsedVersion,
    ParsedVersionSlot,
    ProjectSnapshot,
    ReadIssue,
)


class InventoryAndExportQueueTests(unittest.TestCase):
    def test_inventory_is_read_only_hashes_duplicate_candidates_and_compares_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            indexed_file = source / "indexed.ols"
            duplicate_file = source / "copy.bin"
            different_file = source / "different.ols"
            indexed_file.write_bytes(b"same contents")
            duplicate_file.write_bytes(b"same contents")
            different_file.write_bytes(b"diff contents")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                stats = indexed_file.stat()
                database.connection.execute(
                    """
                    INSERT INTO source_files(path,sha256,size_bytes,modified_ns)
                    VALUES(?,?,?,?)
                    """,
                    (
                        str(indexed_file.resolve()),
                        stream_sha256(indexed_file),
                        stats.st_size,
                        stats.st_mtime_ns,
                    ),
                )
                database.connection.commit()
                original_hash = stream_sha256(indexed_file)
                file_list = root / "reports" / "files.jsonl"
                report = scan_inventory(
                    roots=[source, indexed_file, root / "missing"],
                    database=database,
                    hash_duplicate_candidates=True,
                    verify_indexed_hashes=True,
                    file_list_path=file_list,
                )

                self.assertEqual(report["mode"], "dry_run")
                self.assertIsInstance(report["inventory_run_id"], int)
                self.assertEqual(report["roots"][2]["status"], "missing")
                self.assertEqual(report["scan"]["files"], 3)
                self.assertEqual(report["scan"]["ols_projects"], 2)
                self.assertEqual(
                    report["scan"]["repeated_paths_from_overlapping_roots"], 1
                )
                latest_scan = database.get_latest_inventory_run()
                self.assertEqual(latest_scan["id"], report["inventory_run_id"])
                self.assertEqual(latest_scan["report"]["scan"]["files"], 3)
                self.assertEqual(report["indexed_comparison"]["full_hash_match"], 1)
                self.assertEqual(report["indexed_comparison"]["not_indexed_ols_files"], 1)
                self.assertEqual(report["duplicates"]["exact_duplicate_groups"], 1)
                self.assertEqual(
                    report["duplicates"]["exact_duplicate_files_beyond_first"], 1
                )
                self.assertEqual(indexed_file.read_bytes(), b"same contents")
                self.assertEqual(stream_sha256(indexed_file), original_hash)
                records = [
                    json.loads(line)
                    for line in file_list.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(len(records), 3)
                self.assertEqual(
                    next(row for row in records if row["path"].endswith("indexed.ols"))[
                        "indexed_status"
                    ],
                    "hash_match",
                )
                with self.assertRaisesRegex(ValueError, "must not be inside source root"):
                    scan_inventory(
                        roots=[source],
                        database=database,
                        file_list_path=source / "inventory.jsonl",
                    )
            finally:
                database.close()

    def test_inventory_does_not_claim_unchanged_content_without_hashing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "one.ols"
            source.write_bytes(b"contents")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                stats = source.stat()
                database.connection.execute(
                    """
                    INSERT INTO source_files(path,sha256,size_bytes,modified_ns)
                    VALUES(?,?,?,?)
                    """,
                    (
                        str(source.resolve()),
                        "0" * 64,
                        stats.st_size,
                        stats.st_mtime_ns,
                    ),
                )
                database.connection.commit()
                report = scan_inventory(roots=[source], database=database)
                self.assertEqual(
                    report["indexed_comparison"]["stat_matched_hash_unverified"], 1
                )
                self.assertEqual(report["indexed_comparison"]["full_hash_match"], 0)
            finally:
                database.close()

    def test_export_queue_only_requests_missing_references_and_is_idempotent(self):
        class Reader:
            parser_name = "fake"
            parser_version = "1"

            def inspect(self, _source):
                return ProjectSnapshot(
                    parser_name=self.parser_name,
                    parser_version=self.parser_version,
                    format_version=503,
                    metadata={},
                    versions=(ParsedVersion("Original", "", 0, 1, 0, 8, "", 0),),
                    maps=(
                        ParsedMap("Map A", "", "MAP", "UWORD", 2, 2, 0, "", 1, 0),
                    ),
                    issues=(ReadIssue("partial", "Partial parser data."),),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "one.ols"
            source.write_bytes(b"source")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                outcome = next(
                    import_sources(
                        sources=[source],
                        reader=Reader(),
                        database=database,
                        max_file_bytes=1024,
                        progress=False,
                    )
                )
                self.assertEqual(outcome.status, "partial")
                first = plan_export_tasks(database)
                second = plan_export_tasks(database)
                self.assertEqual(first["tasks_planned"], 2)
                self.assertEqual(second["tasks_planned"], 2)
                self.assertEqual(
                    [task["id"] for task in database.list_export_tasks()],
                    first["task_ids"],
                )
                queue = database.list_export_tasks()
                self.assertEqual(
                    {item["export_types"][0] for item in queue},
                    {"binary_version", "map_metadata"},
                )
                batch = write_manual_export_batch(
                    database,
                    output_directory=root / "manual-batch",
                )
                batch_manifest = json.loads(
                    Path(batch["manifest"]).read_text(encoding="utf-8")
                )
                self.assertEqual(batch["automation"], "not_used")
                self.assertEqual(batch_manifest["winols_automation"], "not_used")
                self.assertEqual(len(batch_manifest["tasks"]), 2)
                self.assertFalse((root / "manual-batch" / "exports").exists())
                binary_task = next(
                    item for item in queue if item["export_types"] == ["binary_version"]
                )
                self.assertEqual(binary_task["version_names"], ["Original"])
                self.assertEqual(binary_task["priority"], "HIGH_PRIORITY")
                task_id = binary_task["id"]
                database.update_export_task(
                    task_id,
                    status="in_progress",
                    result="Started manual WinOLS export.",
                )
                database.update_export_task(
                    task_id,
                    status="completed",
                    result="Completed with export packet received.",
                )
                updated = next(
                    item
                    for item in database.list_export_tasks()
                    if item["id"] == task_id
                )
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(updated["attempts"], 1)
                self.assertEqual(len(database.list_export_task_history(task_id)), 2)
                with self.assertRaisesRegex(ValueError, "Invalid export task transition"):
                    database.update_export_task(
                        task_id,
                        status="pending",
                        result="Invalid transition.",
                    )
            finally:
                database.close()

    def test_project_matching_reports_evidence_without_promoting_parser_slots(self):
        class Reader:
            parser_name = "fake"
            parser_version = "1"

            def inspect(self, source):
                return ProjectSnapshot(
                    parser_name=self.parser_name,
                    parser_version=self.parser_version,
                    format_version=503,
                    metadata={"sw_number": "SW-123", "make": "Example"},
                    versions=(ParsedVersion("Original", "", 0, 1, 0, 8, "", 0),),
                    maps=(),
                    version_slots=(
                        # The common digest is parser-derived, not WinOLS-confirmed.
                        ParsedVersionSlot(
                            0,
                            0,
                            8,
                            "Original",
                            "",
                            "",
                            "a" * 64,
                            "b" * 64,
                            4,
                            "present_nonempty_unverified",
                            "named_record",
                        ),
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.ols"
            second = root / "second.ols"
            exact_copy = root / "exact-copy.ols"
            first.write_bytes(b"first source")
            second.write_bytes(b"second source")
            exact_copy.write_bytes(b"first source")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                for source in (first, second, exact_copy):
                    next(
                        import_sources(
                            sources=[source],
                            reader=Reader(),
                            database=database,
                            max_file_bytes=1024,
                            progress=False,
                        )
                    )
                report = find_project_matches(database, 1)
                self.assertEqual(report["candidate_count"], 2)
                match = next(
                    item for item in report["matches"] if item["right"]["id"] == 2
                )
                self.assertEqual(match["status"], "LIKELY")
                self.assertFalse(match["source_file_exact_match"])
                self.assertEqual(len(match["parser_slot_payload_matches"]), 1)
                self.assertIn(
                    "parser_slot_extent_unverified",
                    match["parser_slot_payload_matches"][0]["relation_status"],
                )
                self.assertEqual(match["right"]["metadata"]["sw_number"], "SW-123")
                self.assertEqual(len(match["right"]["versions"]), 1)
                self.assertEqual(match["right"]["maps"], [])
                self.assertEqual(match["right"]["hash_provenance"], "parser_import")
                self.assertIsNone(
                    match["right"]["parser_snapshot_matches_current_content"]
                )
                self.assertTrue(match["matching_evidence"])
                self.assertTrue(match["unresolved_uncertainties"])
                direct = compare_projects(database, 1, 2)
                self.assertEqual(direct["status"], "LIKELY")
                exact = compare_projects(database, 1, 3)
                self.assertEqual(exact["status"], "CONFIRMED")
                self.assertTrue(exact["source_file_exact_match"])
                with database.connection:
                    database.connection.executemany(
                        """
                        INSERT INTO project_metadata(project_id,field,value,status)
                        VALUES(?,'ecu_type',?,'parser_reported')
                        ON CONFLICT(project_id,field) DO UPDATE SET value=excluded.value
                        """,
                        ((1, "ECU-A"), (2, "ECU-B")),
                    )
                rejected = compare_projects(database, 1, 2)
                self.assertEqual(rejected["status"], "REJECTED")
                self.assertTrue(any(
                    item["kind"] == "conflicting_ecu_identity"
                    and item["status"] == "REJECTED"
                    for item in rejected["matching_evidence"]
                ))
                stale_source_id = database.register_source(
                    source_key="stale-match-test",
                    path=str(root),
                    status="available",
                )
                database.record_file_location(
                    source_id=stale_source_id,
                    path=str(first.resolve()),
                    file_kind="ols",
                    size_bytes=first.stat().st_size,
                    modified_ns=first.stat().st_mtime_ns,
                    sha256="f" * 64,
                )
                stale = compare_projects(database, 1, 2)
                self.assertEqual(stale["status"], "INCONCLUSIVE")
                self.assertEqual(stale["left"]["hash_provenance"], "live_file_location")
                self.assertFalse(
                    stale["left"]["parser_snapshot_matches_current_content"]
                )
                self.assertIn("different file revision", stale["basis"])
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
