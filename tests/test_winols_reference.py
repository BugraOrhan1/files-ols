import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ols_platform.database import IndexDatabase
from ols_platform.importer import import_sources
from ols_platform.reader import ParsedVersion, ParsedVersionSlot, ProjectSnapshot
from ols_platform.binary_reference_analysis import analyze_project, store_analysis_report
from ols_platform.winols_reference import (
    build_project_report,
    import_manifest,
    validate_project,
    write_templates,
)


class ReferenceReader:
    parser_name = "reference-test"
    parser_version = "1"

    def inspect(self, source: Path) -> ProjectSnapshot:
        return ProjectSnapshot(
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            format_version=597,
            metadata={"make": "Test", "model": "Vehicle"},
            versions=(ParsedVersion("Original", "", 0, 1, 0, 8, "", 0),),
            maps=(),
            version_slots=(
                ParsedVersionSlot(
                    0,
                    0,
                    8,
                    "Original",
                    "",
                    "4d41524b",
                    hashlib.sha256(b"MARKabcd").hexdigest(),
                    hashlib.sha256(b"abcd").hexdigest(),
                    4,
                    "present_nonempty_unverified",
                    "named_record",
                ),
            ),
        )


class WinOlsReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = IndexDatabase(self.root / "index.sqlite3")
        self.source = self.root / "project.ols"
        self.source.write_bytes(b"MARKabcd")
        outcome = next(
            import_sources(
                sources=[self.source],
                reader=ReferenceReader(),
                database=self.database,
                max_file_bytes=1024,
                progress=False,
            )
        )
        self.assertEqual(outcome.status, "imported")
        self.project_id = self.database.connection.execute(
            "SELECT id FROM projects"
        ).fetchone()["id"]
        self.manifest_path = self.root / "references" / str(self.project_id) / "manifest.json"
        self.manifest_path.parent.mkdir(parents=True)

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def _manifest(self, artifacts, relation="confirmed"):
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        return {
            "schema_version": 1,
            "project_id": self.project_id,
            "source": {"filename": self.source.name, "sha256": source_hash},
            "winols": {"version": "unknown"},
            "export_method": "manual test fixture",
            "project_relation": {
                "status": relation,
                "evidence": "test fixture project mapping",
            },
            "coverage_complete": False,
            "artifacts": artifacts,
        }

    def _write_manifest(self, manifest):
        self.manifest_path.write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def test_template_preserves_source_identity_and_unknowns(self):
        paths = write_templates(
            self.database, self.root / "templates", project_id=self.project_id
        )
        self.assertEqual(len(paths), 1)
        manifest = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["source"]["sha256"], hashlib.sha256(
            self.source.read_bytes()
        ).hexdigest())
        self.assertEqual(manifest["winols"]["version"], "unknown")
        self.assertEqual(manifest["project_relation"]["status"], "unknown")
        self.assertEqual(manifest["export_settings"]["format"], "unknown")
        self.assertEqual(manifest["artifacts"], [])

    def test_confirmed_post_marker_binary_is_compared_exactly_and_idempotently(self):
        reference = self.root / "references" / "original.bin"
        reference.write_bytes(b"abcd")
        artifact = {
            "kind": "binary_version",
            "path": "../original.bin",
            "version_name": "Original",
            "source_slot_index": 0,
            "relation_status": "confirmed",
            "relation_evidence": "manually checked source project and version",
            "representation": "post_marker_payload",
        }
        self._write_manifest(self._manifest([artifact]))
        first = import_manifest(self.database, self.manifest_path)
        second = import_manifest(self.database, self.manifest_path)
        self.assertEqual(first["artifacts"][0]["status"], "imported")
        self.assertEqual(second["artifacts"][0]["status"], "already_imported")
        self.assertEqual(len(self.database.list_reference_artifacts(self.project_id)), 1)

        report = validate_project(self.database, self.project_id)
        self.assertEqual(report["checks"][0]["status"], "PASS")
        self.assertEqual(report["validation_status"], "PARTIAL")
        self.assertFalse(report["coverage_complete_declared"])
        self.assertEqual(report["checks"][0]["comparison"]["differing_bytes_in_common_range"], 0)
        self.assertEqual(report["counts"]["independently_confirmed_versions"], 1)
        self.assertEqual(
            self.database.get_diagnostic(self.project_id)["reference_validation"]["status"],
            "not_validated",
        )

    def test_editing_manifest_updates_artifact_without_duplicate(self):
        first = self.root / "references" / "first.bin"
        second = self.root / "references" / "second.bin"
        first.write_bytes(b"abcd")
        second.write_bytes(b"abXd")
        manifest = self._manifest([{
            "kind": "binary_version",
            "path": "../first.bin",
        }])
        self._write_manifest(manifest)
        import_manifest(self.database, self.manifest_path)
        manifest["artifacts"][0]["path"] = "../second.bin"
        self._write_manifest(manifest)
        result = import_manifest(self.database, self.manifest_path)
        self.assertEqual(result["artifacts"][0]["status"], "updated")
        artifacts = self.database.list_reference_artifacts(self.project_id)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["path"], str(second.resolve()))

    def test_project_identity_does_not_confirm_individual_export_version(self):
        reference = self.root / "references" / "unlinked.bin"
        reference.write_bytes(b"abcd")
        self._write_manifest(self._manifest([{
            "kind": "binary_version",
            "path": "../unlinked.bin",
        }], relation="confirmed"))
        import_manifest(self.database, self.manifest_path)

        artifact = self.database.list_reference_artifacts(self.project_id)[0]
        self.assertEqual(artifact["relation_status"], "unknown")
        self.assertEqual(
            artifact["metadata"]["manifest_project_relation_status"],
            "confirmed",
        )

    def test_reference_check_history_survives_manifest_update(self):
        first = self.root / "references" / "first.bin"
        second = self.root / "references" / "second.bin"
        first.write_bytes(b"abcd")
        second.write_bytes(b"abXd")
        manifest = self._manifest([{
            "kind": "binary_version",
            "path": "../first.bin",
        }])
        self._write_manifest(manifest)
        import_manifest(self.database, self.manifest_path)
        validate_project(self.database, self.project_id)
        manifest["artifacts"][0]["path"] = "../second.bin"
        self._write_manifest(manifest)
        import_manifest(self.database, self.manifest_path)

        history = self.database.list_reference_check_history(self.project_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["artifact_path"], str(first.resolve()))
        self.assertEqual(
            history[0]["artifact_sha256"],
            hashlib.sha256(b"abcd").hexdigest(),
        )

    def test_differing_binary_fails_and_is_not_silently_normalized(self):
        reference = self.root / "references" / "different.bin"
        reference.write_bytes(b"abXd")
        self._write_manifest(self._manifest([{
            "kind": "binary_version",
            "path": "../different.bin",
            "version_name": "Original",
            "source_slot_index": 0,
            "relation_status": "confirmed",
            "representation": "post_marker_payload",
        }]))
        import_manifest(self.database, self.manifest_path)
        report = validate_project(self.database, self.project_id)
        self.assertEqual(report["checks"][0]["status"], "FAIL")
        self.assertEqual(report["checks"][0]["comparison"]["differing_bytes_in_common_range"], 1)

    def test_unknown_mapping_and_representation_are_inconclusive(self):
        reference = self.root / "references" / "unknown.bin"
        reference.write_bytes(b"abcd")
        self._write_manifest(self._manifest([{
            "kind": "binary_version",
            "path": "../unknown.bin",
            "version_name": "Original",
        }], relation="suggested"))
        import_manifest(self.database, self.manifest_path)
        report = validate_project(self.database, self.project_id)
        self.assertEqual(report["checks"][0]["status"], "INCONCLUSIVE")
        self.assertEqual(report["counts"]["pass"], 0)

    def test_trailing_four_byte_candidate_is_reported_without_normalization(self):
        reference = self.root / "references" / "candidate.bin"
        reference.write_bytes(b"abcdTAIL")
        self._write_manifest(self._manifest([{
            "kind": "binary_version",
            "path": "../candidate.bin",
            "relation_status": "confirmed",
            "representation": "unknown",
        }]))
        import_manifest(self.database, self.manifest_path)

        report = validate_project(self.database, self.project_id)
        check = report["checks"][0]
        self.assertEqual(check["status"], "INCONCLUSIVE")
        self.assertEqual(
            check["comparison"]["candidate_slots"][0]["slot_index"], 0
        )
        self.assertEqual(
            check["comparison"]["candidate_slots"][0][
                "unexplained_trailing_bytes"
            ]["sha256"],
            hashlib.sha256(b"TAIL").hexdigest(),
        )
        self.assertEqual(report["counts"]["candidate_binary_slot_matches"], 1)
        self.assertEqual(report["counts"]["independently_confirmed_versions"], 0)

    def test_reference_changed_after_import_is_not_validated(self):
        reference = self.root / "references" / "changed.bin"
        reference.write_bytes(b"abcd")
        self._write_manifest(self._manifest([{
            "kind": "binary_version",
            "path": "../changed.bin",
            "source_slot_index": 0,
            "relation_status": "confirmed",
            "representation": "post_marker_payload",
        }]))
        import_manifest(self.database, self.manifest_path)
        reference.write_bytes(b"abXd")

        report = validate_project(self.database, self.project_id)
        self.assertEqual(report["checks"][0]["status"], "FAIL")
        self.assertIn("changed since it was imported", report["checks"][0]["message"])

    def test_binary_analysis_is_audited_and_manual_review_is_append_only(self):
        reference = self.root / "references" / "named.bin"
        reference.write_bytes(b"abcd")
        self._write_manifest(self._manifest([{
            "kind": "binary_version",
            "path": "../named.bin",
            "version_name": "Original",
            "source_slot_index": 0,
            "relation_status": "confirmed",
            "representation": "post_marker_payload",
        }]))
        import_manifest(self.database, self.manifest_path)

        report = analyze_project(self.database, self.project_id)
        candidate = report["artifact_reports"][0]["candidate_slots"][0]
        self.assertEqual(candidate["binary_match_status"], "PASS")
        self.assertEqual(candidate["version_relation_status"], "PASS")
        first_run_id = store_analysis_report(self.database, report)
        stored = self.database.list_binary_slot_candidates(self.project_id)
        self.assertEqual(stored[0]["run_id"], first_run_id)
        self.assertEqual(stored[0]["binary_status"], "PASS")

        self.database.review_version_match(
            stored[0]["id"],
            "confirm_version",
            "Reviewed against the explicit test version record.",
            reviewer="test-reviewer",
        )
        self.assertEqual(
            self.database.list_binary_slot_candidates(self.project_id)[0][
                "latest_review_decision"
            ],
            "confirm_version",
        )
        second_run_id = store_analysis_report(
            self.database,
            analyze_project(self.database, self.project_id),
        )
        self.assertNotEqual(first_run_id, second_run_id)
        self.assertEqual(
            len(self.database.list_binary_analysis_runs(self.project_id)), 2
        )

    def test_source_changed_after_indexing_blocks_reference_validation(self):
        reference = self.root / "references" / "source-check.bin"
        reference.write_bytes(b"abcd")
        self._write_manifest(self._manifest([{
            "kind": "binary_version",
            "path": "../source-check.bin",
            "source_slot_index": 0,
            "relation_status": "confirmed",
            "representation": "post_marker_payload",
        }]))
        import_manifest(self.database, self.manifest_path)
        self.source.write_bytes(b"MARKwxyz")

        report = validate_project(self.database, self.project_id)
        self.assertEqual(report["checks"][0]["status"], "FAIL")
        self.assertIn("Source OLS changed since indexing", report["checks"][0]["message"])

    def test_metadata_json_and_csv_are_hashed_and_counted(self):
        metadata = self.root / "references" / "metadata.json"
        metadata.write_text(json.dumps({"make": "Test", "model": "Vehicle"}), encoding="utf-8")
        maps = self.root / "references" / "maps.csv"
        with maps.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=["name", "address", "rows", "columns"])
            writer.writeheader()
            writer.writerow({"name": "Example", "address": "unknown", "rows": "2", "columns": "2"})
        self._write_manifest(self._manifest([
            {"kind": "project_metadata", "path": "../metadata.json"},
            {"kind": "map_metadata", "path": "../maps.csv"},
        ]))
        imported = import_manifest(self.database, self.manifest_path)
        self.assertEqual([x["status"] for x in imported["artifacts"]], ["imported", "imported"])
        self.assertEqual(imported["artifacts"][0]["metadata_records"], 1)
        self.assertEqual(imported["artifacts"][1]["metadata_records"], 1)
        report = build_project_report(self.database, self.project_id)
        self.assertEqual(report["counts"]["imported_map_metadata_records"], 1)
        self.assertEqual(report["validation_status"], "INCONCLUSIVE")

    def test_source_hash_mismatch_rejects_manifest_before_writing(self):
        manifest = self._manifest([])
        manifest["source"]["sha256"] = "0" * 64
        self._write_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "does not match"):
            import_manifest(self.database, self.manifest_path)
        self.assertEqual(self.database.list_reference_artifacts(self.project_id), [])

    def test_preflight_prevents_partial_import_when_later_artifact_is_invalid(self):
        valid = self.root / "references" / "valid.bin"
        valid.write_bytes(b"bytes")
        self._write_manifest(self._manifest([
            {"kind": "binary_version", "path": "../valid.bin"},
            {"kind": "binary_version", "path": "../missing.bin"},
        ]))
        with self.assertRaises(FileNotFoundError):
            import_manifest(self.database, self.manifest_path)
        self.assertEqual(self.database.list_reference_artifacts(self.project_id), [])


if __name__ == "__main__":
    unittest.main()
