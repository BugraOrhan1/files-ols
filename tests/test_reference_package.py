import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ols_platform.database import IndexDatabase
from ols_platform.importer import import_sources
from ols_platform.reader import (
    ParsedAxis,
    ParsedMap,
    ParsedVersionSlot,
    ProjectSnapshot,
)
from ols_platform.reference_package import (
    compare_map_metadata,
    compare_project_metadata,
    import_reference_package,
    inspect_package,
    match_package_versions,
    write_package_template,
)


class PacketReader:
    parser_name = "synthetic-packet-unit-test"
    parser_version = "1"

    def __init__(self, source: Path, maps=()):
        self.source = source
        self.maps = tuple(maps)

    def inspect(self, source: Path) -> ProjectSnapshot:
        contents = source.read_bytes()
        slots = []
        for index, offset in enumerate(range(0, len(contents), 8)):
            raw = contents[offset : offset + 8]
            if len(raw) != 8:
                continue
            payload = raw[4:]
            slots.append(
                ParsedVersionSlot(
                    index,
                    offset,
                    len(raw),
                    "",
                    "",
                    raw[:4].hex(),
                    hashlib.sha256(raw).hexdigest(),
                    hashlib.sha256(payload).hexdigest(),
                    len(payload),
                    "present_nonempty_unverified",
                    "unresolved",
                )
            )
        return ProjectSnapshot(
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            format_version=503,
            metadata={"make": "", "model": ""},
            versions=(),
            maps=self.maps,
            version_slots=tuple(slots),
        )


class ReferencePackageTests(unittest.TestCase):
    """Package/matching unit tests use synthetic fixtures, never WinOLS proof."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = IndexDatabase(self.root / "index.sqlite3")
        self.source = self.root / "project.ols"
        self.source.write_bytes(b"MARKabcdMARKwxyz")
        outcome = next(
            import_sources(
                sources=[self.source],
                reader=PacketReader(self.source),
                database=self.database,
                max_file_bytes=1024,
                progress=False,
            )
        )
        self.assertEqual(outcome.status, "imported")
        self.project_id = self.database.connection.execute(
            "SELECT id FROM projects"
        ).fetchone()["id"]
        self.packet_dir = self.root / "packet"
        self.packet_dir.mkdir()
        self.manifest_path = self.packet_dir / "manifest.json"

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def _base_manifest(self):
        return {
            "schema_version": 1,
            "package_type": "winols-reference-package",
            "project_id": self.project_id,
            "source": {
                "path": str(self.source),
                "filename": self.source.name,
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                "size_bytes": self.source.stat().st_size,
                "format_version": 503,
            },
            "winols": {"version": "5.80.00"},
            "export_method": "WinOLS Export File",
            "project_identity": {
                "project_name": "Synthetic test project",
                "project_id": "test-project-id",
                "status": "confirmed",
                "evidence": "Synthetic unit-test relationship only.",
            },
            "export_settings": {
                "binary_format": "raw binary",
                "checksum_or_syncblock_options": "disabled for test fixture",
                "address_range": "full file",
                "version_selection": "one version per export",
                "map_address_representation": {
                    "parser": "unknown",
                    "reference": "unknown",
                    "project_base": None,
                },
            },
            "project_relation": {
                "status": "confirmed",
                "evidence": "Synthetic unit-test relationship; not external validation.",
            },
            "manual_review": {
                "status": "confirmed",
                "reviewer": "synthetic unit test",
                "reviewed_at": "2026-01-01",
                "method": "synthetic fixture",
                "evidence": "Tests logic only; this is not WinOLS validation.",
                "confirmed_fields": [
                    "project_identity",
                    "project_relation",
                    "version_inventory",
                ],
                "missing_evidence": [],
            },
            "coverage_complete": True,
            "exported_versions": [
                {
                    "version_key": "version-a",
                    "version_name": "Version A",
                    "source_slot_index": 0,
                    "relation_status": "confirmed",
                    "relation_evidence": "Synthetic slot relationship only.",
                },
                {
                    "version_key": "version-b",
                    "version_name": "Version B",
                    "source_slot_index": 1,
                    "relation_status": "confirmed",
                    "relation_evidence": "Synthetic slot relationship only.",
                },
            ],
            "artifacts": [],
        }

    def _add_binary_artifact(
        self,
        manifest,
        *,
        key,
        name,
        slot,
        content,
        representation="post_marker_payload",
        relation="confirmed",
        path=None,
        evidence="Unit-test explicit mapping only.",
    ):
        export_path = self.packet_dir / (path or f"{key}.bin")
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_bytes(content)
        manifest["artifacts"].append(
            {
                "kind": "binary_version",
                "path": str(export_path.relative_to(self.packet_dir)),
                "size_bytes": export_path.stat().st_size,
                "sha256": hashlib.sha256(content).hexdigest(),
                "version_key": key,
                "version_name": name,
                "source_slot_index": slot,
                "relation_status": relation,
                "relation_evidence": evidence,
                "representation": representation,
                "artifact_source": "winols_export",
                "manual_review": {
                    "status": "confirmed",
                    "reviewer": "synthetic unit test",
                    "reviewed_at": "2026-01-01",
                    "method": "synthetic fixture",
                    "evidence": "Tests logic only; this is not WinOLS validation.",
                    "confirmed_fields": [
                        "project_relation",
                        "version_relation",
                        "version_name",
                        "source_slot_index",
                        "representation",
                    ],
                    "missing_evidence": [],
                },
            }
        )
        return export_path

    def _add_metadata_artifact(self, manifest, metadata):
        export_path = self.packet_dir / "metadata.json"
        export_path.write_text(
            json.dumps({"metadata": metadata}),
            encoding="utf-8",
        )
        manifest["artifacts"].append(
            {
                "kind": "project_metadata",
                "path": export_path.name,
                "size_bytes": export_path.stat().st_size,
                "sha256": hashlib.sha256(export_path.read_bytes()).hexdigest(),
                "relation_status": "confirmed",
                "relation_evidence": "Synthetic metadata relation only.",
                "artifact_source": "winols_export",
                "coverage_complete": True,
                "manual_review": {
                    "status": "confirmed",
                    "reviewer": "synthetic unit test",
                    "reviewed_at": "2026-01-01",
                    "method": "synthetic fixture",
                    "evidence": "Tests logic only; this is not WinOLS validation.",
                    "confirmed_fields": [
                        "project_relation",
                        "metadata_inventory",
                        *[f"metadata.{field}" for field in metadata],
                    ],
                    "missing_evidence": [],
                },
            }
        )
        return export_path

    def _write(self, manifest):
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

    def test_template_is_not_tested_and_creates_requested_checklist(self):
        paths = write_package_template(
            self.database, self.project_id, self.root / "new-packet"
        )
        manifest = json.loads(Path(paths["manifest"]).read_text(encoding="utf-8"))
        report = inspect_package(self.database, Path(paths["manifest"]))
        self.assertEqual(manifest["source"]["sha256"], hashlib.sha256(
            self.source.read_bytes()
        ).hexdigest())
        self.assertEqual(manifest["exported_versions"], [])
        self.assertEqual(manifest["artifacts"], [])
        self.assertEqual(manifest["manual_review"]["status"], "NOT_TESTED")
        self.assertEqual(manifest["project_identity"]["status"], "unknown")
        self.assertEqual(report["status"], "NOT_TESTED")
        self.assertIn("No WinOLS export has been generated", Path(paths["checklist"]).read_text())

    def test_complete_package_has_per_version_exact_matches_and_idempotent_import(self):
        manifest = self._base_manifest()
        self._add_binary_artifact(
            manifest,
            key="version-a",
            name="Version A",
            slot=0,
            content=b"abcd",
        )
        self._add_binary_artifact(
            manifest,
            key="version-b",
            name="Version B",
            slot=1,
            content=b"wxyz",
        )
        map_path = self.packet_dir / "exports" / "maps.json"
        map_path.parent.mkdir(parents=True)
        map_path.write_text(json.dumps({"maps": [{"name": "Fuel"}]}))
        map_bytes = map_path.read_bytes()
        manifest["artifacts"].append(
            {
                "kind": "map_metadata",
                "path": "exports/maps.json",
                "size_bytes": len(map_bytes),
                "sha256": hashlib.sha256(map_bytes).hexdigest(),
                "relation_status": "unknown",
                "representation": "metadata_export",
            }
        )
        self._write(manifest)

        inspection = inspect_package(self.database, self.manifest_path)
        self.assertEqual(inspection["status"], "PASS")
        self.assertEqual(inspection["version_evidence_status"], "PASS")
        report = match_package_versions(self.database, self.manifest_path)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(
            [item["status"] for item in report["versions"]],
            ["PASS", "PASS"],
        )
        self.assertEqual(report["versions"][0]["exact_full_candidate_slots"], [0])
        self.assertEqual(report["versions"][1]["exact_full_candidate_slots"], [1])

        first = import_reference_package(self.database, self.manifest_path)
        second = import_reference_package(self.database, self.manifest_path)
        self.assertEqual(first["import"]["artifacts"][0]["status"], "imported")
        self.assertTrue(
            all(
                item["status"] == "already_imported"
                for item in second["import"]["artifacts"]
            )
        )
        self.assertEqual(
            len(self.database.list_reference_artifacts(self.project_id)),
            3,
        )
        stored = self.database.list_reference_artifacts(self.project_id)
        stored_binary = next(
            item for item in stored if item["kind"] == "binary_version"
        )
        self.assertEqual(stored_binary["metadata"]["artifact_source"], "winols_export")
        self.assertEqual(
            stored_binary["metadata"]["manual_review"],
            manifest["artifacts"][0]["manual_review"],
        )
        self.assertEqual(
            stored_binary["metadata"]["project_identity"],
            manifest["project_identity"],
        )
        self.assertEqual(
            stored_binary["metadata"]["version_relation_evidence"],
            manifest["exported_versions"][0]["relation_evidence"],
        )

    def test_missing_export_is_not_tested_and_packet_import_is_blocked(self):
        manifest = self._base_manifest()
        manifest["exported_versions"] = manifest["exported_versions"][:1]
        manifest["artifacts"] = [
            {
                "kind": "binary_version",
                "path": "exports/not-yet-exported.bin",
                "version_key": "version-a",
                "version_name": "Version A",
                "relation_status": "unknown",
                "representation": "unknown",
            }
        ]
        self._write(manifest)
        report = inspect_package(self.database, self.manifest_path)
        self.assertEqual(report["status"], "NOT_TESTED")
        self.assertEqual(report["artifacts"][0]["status"], "NOT_TESTED")
        with self.assertRaisesRegex(ValueError, "every declared artifact"):
            import_reference_package(self.database, self.manifest_path)

    def test_missing_version_name_is_inconclusive(self):
        manifest = self._base_manifest()
        manifest["exported_versions"] = [
            {"version_key": "unnamed", "version_name": ""}
        ]
        self._add_binary_artifact(
            manifest,
            key="unnamed",
            name="",
            slot=None,
            content=b"abcd",
            representation="unknown",
            relation="unknown",
        )
        self._write(manifest)
        report = inspect_package(self.database, self.manifest_path)
        self.assertEqual(report["status"], "INCONCLUSIVE")
        self.assertEqual(report["versions"][0]["name_status"], "missing")
        match = match_package_versions(self.database, self.manifest_path)
        self.assertEqual(match["versions"][0]["status"], "INCONCLUSIVE")

    def test_exact_bytes_do_not_confirm_a_wrong_project_relationship(self):
        manifest = self._base_manifest()
        self._add_binary_artifact(
            manifest,
            key="version-a",
            name="Version A",
            slot=0,
            content=b"abcd",
        )
        manifest["project_relation"]["status"] = "unknown"
        self._write(manifest)

        report = match_package_versions(self.database, self.manifest_path)

        self.assertEqual(report["versions"][0]["exact_full_candidate_slots"], [0])
        self.assertEqual(report["versions"][0]["status"], "INCONCLUSIVE")
        self.assertIn("do not confirm", report["versions"][0]["reason"])

    def test_incomplete_version_coverage_cannot_pass_an_exact_match(self):
        manifest = self._base_manifest()
        manifest["exported_versions"] = manifest["exported_versions"][:1]
        self._add_binary_artifact(
            manifest,
            key="version-a",
            name="Version A",
            slot=0,
            content=b"abcd",
        )
        manifest["coverage_complete"] = False
        self._write(manifest)

        report = match_package_versions(self.database, self.manifest_path)

        self.assertEqual(report["versions"][0]["exact_full_candidate_slots"], [0])
        self.assertEqual(report["versions"][0]["status"], "INCONCLUSIVE")

    def test_binary_export_hash_mismatch_fails_integrity_check(self):
        manifest = self._base_manifest()
        self._add_binary_artifact(
            manifest,
            key="version-a",
            name="Version A",
            slot=0,
            content=b"abcd",
        )
        manifest["exported_versions"] = manifest["exported_versions"][:1]
        manifest["artifacts"][0]["sha256"] = "0" * 64
        self._write(manifest)

        report = match_package_versions(self.database, self.manifest_path)

        self.assertEqual(report["versions"][0]["status"], "FAIL")
        self.assertEqual(report["versions"][0]["artifact_integrity_status"], "FAIL")

    def test_parser_output_is_not_accepted_as_independent_artifact_provenance(self):
        manifest = self._base_manifest()
        manifest["exported_versions"] = manifest["exported_versions"][:1]
        self._add_binary_artifact(
            manifest,
            key="version-a",
            name="Version A",
            slot=0,
            content=b"abcd",
        )
        manifest["artifacts"][0]["artifact_source"] = "py-ols parser output"
        self._write(manifest)

        report = match_package_versions(self.database, self.manifest_path)

        self.assertEqual(report["versions"][0]["status"], "INCONCLUSIVE")
        self.assertTrue(
            any(
                "Artifact source must be" in item
                for item in report["versions"][0]["version_evidence_gaps"]
            )
        )

    def test_metadata_comparison_requires_review_and_detects_hash_changes(self):
        manifest = self._base_manifest()
        artifact = self._add_metadata_artifact(
            manifest,
            {"make": "", "model": ""},
        )
        self._write(manifest)

        report = compare_project_metadata(self.database, self.manifest_path)

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(
            [item["status"] for item in report["comparisons"]],
            ["PASS", "PASS"],
        )

        artifact.write_text(
            json.dumps({"metadata": {"make": "changed", "model": ""}}),
            encoding="utf-8",
        )
        report = compare_project_metadata(self.database, self.manifest_path)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["artifact_checks"][0]["status"], "FAIL")

    def test_metadata_without_independent_project_relation_is_not_comparable(self):
        manifest = self._base_manifest()
        self._add_metadata_artifact(manifest, {"make": "", "model": ""})
        manifest["project_relation"]["status"] = "unknown"
        self._write(manifest)

        report = compare_project_metadata(self.database, self.manifest_path)

        self.assertEqual(report["status"], "INCONCLUSIVE")
        self.assertEqual(
            [item["status"] for item in report["comparisons"]],
            ["NOT_COMPARABLE", "NOT_COMPARABLE"],
        )

    def test_invalid_manifest_and_invalid_project_relationship_fail(self):
        manifest = self._base_manifest()
        manifest["project_id"] = 9999
        self._write(manifest)
        self.assertEqual(inspect_package(self.database, self.manifest_path)["status"], "FAIL")

        manifest = self._base_manifest()
        manifest["project_relation"] = {"status": "made-up", "evidence": ""}
        self._write(manifest)
        report = inspect_package(self.database, self.manifest_path)
        self.assertEqual(report["status"], "FAIL")

    def test_multiple_candidate_slots_are_not_auto_assigned(self):
        self.source.write_bytes(b"MARKabcdMARKabcd")
        self.database.close()
        self.database = IndexDatabase(self.root / "index.sqlite3")
        outcome = next(
            import_sources(
                sources=[self.source],
                reader=PacketReader(self.source),
                database=self.database,
                max_file_bytes=1024,
                progress=False,
            )
        )
        self.assertEqual(outcome.status, "imported")
        project_id = self.database.connection.execute(
            "SELECT id FROM projects WHERE source_id=(SELECT id FROM source_files WHERE path=?)",
            (str(self.source.resolve()),),
        ).fetchone()["id"]
        manifest = self._base_manifest()
        manifest["project_id"] = project_id
        manifest["source"].update(
            {
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                "size_bytes": self.source.stat().st_size,
            }
        )
        manifest["project_relation"]["status"] = "unknown"
        manifest["exported_versions"] = [
            {"version_key": "candidate", "version_name": "Candidate"}
        ]
        self._add_binary_artifact(
            manifest,
            key="candidate",
            name="Candidate",
            slot=None,
            content=b"abcd",
            representation="unknown",
            relation="unknown",
        )
        self._write(manifest)
        report = match_package_versions(self.database, self.manifest_path)
        candidate = report["versions"][0]
        self.assertEqual(candidate["status"], "INCONCLUSIVE")
        self.assertEqual(candidate["exact_full_candidate_slots"], [0, 1])

    def test_distinct_versions_claiming_one_slot_are_not_both_confirmed(self):
        manifest = self._base_manifest()
        self._add_binary_artifact(
            manifest,
            key="version-a",
            name="Version A",
            slot=0,
            content=b"abcd",
            path="version-a.bin",
        )
        self._add_binary_artifact(
            manifest,
            key="version-b",
            name="Version B",
            slot=0,
            content=b"abcd",
            path="version-b.bin",
        )
        manifest["exported_versions"][1]["source_slot_index"] = 0
        self._write(manifest)
        report = match_package_versions(self.database, self.manifest_path)
        self.assertEqual(report["status"], "INCONCLUSIVE")
        self.assertEqual(
            [item["status"] for item in report["versions"]],
            ["INCONCLUSIVE", "INCONCLUSIVE"],
        )
        self.assertTrue(
            all("Multiple exported versions claim slot" in item["reason"]
                for item in report["versions"])
        )

    def test_prefix_with_trailing_bytes_is_reported_without_trimming(self):
        self.source.write_bytes(b"MARKabcdTAIL")
        self.database.close()
        self.database = IndexDatabase(self.root / "index.sqlite3")
        outcome = next(
            import_sources(
                sources=[self.source],
                reader=PacketReader(self.source),
                database=self.database,
                max_file_bytes=1024,
                progress=False,
            )
        )
        self.assertEqual(outcome.status, "imported")
        project_id = self.database.connection.execute(
            "SELECT id FROM projects WHERE source_id=(SELECT id FROM source_files WHERE path=?)",
            (str(self.source.resolve()),),
        ).fetchone()["id"]
        manifest = self._base_manifest()
        manifest["project_id"] = project_id
        manifest["source"].update(
            {
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                "size_bytes": self.source.stat().st_size,
            }
        )
        manifest["project_relation"]["status"] = "unknown"
        manifest["exported_versions"] = [
            {"version_key": "extended", "version_name": "Extended"}
        ]
        self._add_binary_artifact(
            manifest,
            key="extended",
            name="Extended",
            slot=None,
            content=b"abcdTAIL",
            representation="unknown",
            relation="unknown",
        )
        self._write(manifest)
        report = match_package_versions(self.database, self.manifest_path)
        candidate = report["versions"][0]
        self.assertEqual(candidate["status"], "INCONCLUSIVE")
        self.assertEqual(candidate["exact_partial_candidate_slots"], [0])
        boundary = candidate["candidate_slots"][0][
            "contiguous_export_range_from_payload"
        ]
        self.assertTrue(boundary["exact_full_match"])
        self.assertEqual(
            candidate["candidate_slots"][0]["trailing_export_bytes"],
            4,
        )

    def test_full_binary_exact_match_and_wrong_version_slot(self):
        manifest = self._base_manifest()
        self._add_binary_artifact(
            manifest,
            key="version-a",
            name="Version A",
            slot=0,
            content=b"MARKabcd",
            representation="full_binary",
        )
        manifest["exported_versions"] = manifest["exported_versions"][:1]
        self._write(manifest)
        report = match_package_versions(self.database, self.manifest_path)
        self.assertEqual(report["versions"][0]["status"], "PASS")
        self.assertTrue(
            report["versions"][0]["candidate_slots"][0]["comparisons"][
                "full_slot"
            ]["exact_full_match"]
        )

        manifest = self._base_manifest()
        manifest["exported_versions"] = manifest["exported_versions"][:1]
        self._add_binary_artifact(
            manifest,
            key="version-a",
            name="Version A",
            slot=0,
            content=b"wxyz",
            representation="post_marker_payload",
        )
        self._write(manifest)
        report = match_package_versions(self.database, self.manifest_path)
        self.assertEqual(report["versions"][0]["status"], "FAIL")

    def test_unknown_relative_map_addresses_are_not_compared_as_absolute(self):
        map_record = ParsedMap(
            "Fuel",
            "Fuel quantity",
            "MAP",
            "U16",
            2,
            3,
            0x1000,
            "mg",
            1.0,
            0.0,
            axes=(
                ParsedAxis("x", 2, 0x200, "U8", "rpm", 1.0, 0.0, "rpm", ""),
            ),
        )
        map_source = self.root / "project-with-map.ols"
        map_source.write_bytes(b"MARKabcdMARKwxyz")
        self.database.close()
        self.database = IndexDatabase(self.root / "index.sqlite3")
        outcome = next(
            import_sources(
                sources=[map_source],
                reader=PacketReader(map_source, (map_record,)),
                database=self.database,
                max_file_bytes=1024,
                progress=False,
            )
        )
        self.assertEqual(outcome.status, "imported")
        project_id = self.database.connection.execute(
            "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        map_path = self.packet_dir / "maps.json"
        map_path.write_text(
            json.dumps(
                {
                    "maps": [
                        {
                            "parser_ordinal": 0,
                            "name": "Fuel",
                            "description": "Fuel quantity",
                            "columns": "2",
                            "rows": "3",
                            "address": "0x1000",
                            "data_type": "u16",
                            "factor": "1.0",
                            "conversion_offset": "0",
                            "axes": [{"role": "x", "points": 2}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        content = map_path.read_bytes()
        manifest = self._base_manifest()
        manifest["project_id"] = project_id
        manifest["source"].update(
            {
                "path": str(map_source),
                "filename": map_source.name,
                "sha256": hashlib.sha256(map_source.read_bytes()).hexdigest(),
                "size_bytes": map_source.stat().st_size,
            }
        )
        manifest["exported_versions"] = []
        manifest["artifacts"] = [
            {
                "kind": "map_metadata",
                "path": "maps.json",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "relation_status": "confirmed",
                "relation_evidence": "Synthetic map relationship only.",
                "artifact_source": "winols_export",
                "coverage_complete": True,
                "manual_review": {
                    "status": "confirmed",
                    "reviewer": "synthetic unit test",
                    "reviewed_at": "2026-01-01",
                    "method": "synthetic fixture",
                    "evidence": "Tests logic only; this is not WinOLS validation.",
                    "confirmed_fields": [
                        "project_relation",
                        "map_inventory",
                        "maps[*].parser_ordinal",
                        "maps[*].name",
                        "maps[*].description",
                        "maps[*].columns",
                        "maps[*].rows",
                        "maps[*].data_type",
                        "maps[*].axes[*].role",
                        "maps[*].axes[*].points",
                    ],
                    "missing_evidence": [],
                },
            }
        ]
        self._write(manifest)
        report = compare_map_metadata(self.database, self.manifest_path)
        self.assertEqual(report["status"], "INCONCLUSIVE")
        fields = report["matches"][0]["fields"]
        self.assertEqual(fields["columns"]["status"], "PASS")
        self.assertEqual(fields["rows"]["status"], "PASS")
        self.assertEqual(fields["address"]["status"], "NOT_COMPARABLE")
        self.assertEqual(fields["axes"]["status"], "NOT_COMPARABLE")
        self.assertIn("NOT_COMPARABLE", report["field_statuses"])

    def test_incomplete_parser_axes_do_not_validate_against_full_reference_axes(self):
        map_source = self.root / "project-with-incomplete-axis.ols"
        map_source.write_bytes(b"MARKabcd")
        incomplete_map = ParsedMap(
            "Fuel",
            "Fuel quantity",
            "MAP",
            "U16",
            2,
            3,
            0x1000,
            "mg",
            1.0,
            0.0,
            axes=(
                ParsedAxis("x", 2, 0x200, "U8", "", 1.0, 0.0, "", ""),
            ),
        )
        self.database.close()
        self.database = IndexDatabase(self.root / "index.sqlite3")
        outcome = next(
            import_sources(
                sources=[map_source],
                reader=PacketReader(map_source, (incomplete_map,)),
                database=self.database,
                max_file_bytes=1024,
                progress=False,
            )
        )
        self.assertEqual(outcome.status, "imported")
        project_id = self.database.connection.execute(
            "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        map_path = self.packet_dir / "incomplete-maps.json"
        map_path.write_text(
            json.dumps(
                {
                    "maps": [
                        {
                            "parser_ordinal": 0,
                            "name": "Fuel",
                            "description": "Fuel quantity",
                            "columns": 2,
                            "rows": 3,
                            "data_type": "U16",
                            "factor": 1.0,
                            "conversion_offset": 0.0,
                            "axes": [
                                {
                                    "role": "x",
                                    "points": 2,
                                    "data_type": "U8",
                                    "unit": "rpm",
                                    "factor": 1.0,
                                    "conversion_offset": 0.0,
                                    "identifier": "reviewed-axis",
                                    "description": "Reviewed axis",
                                }
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        content = map_path.read_bytes()
        manifest = self._base_manifest()
        manifest["project_id"] = project_id
        manifest["source"].update(
            {
                "path": str(map_source),
                "filename": map_source.name,
                "sha256": hashlib.sha256(map_source.read_bytes()).hexdigest(),
                "size_bytes": map_source.stat().st_size,
            }
        )
        manifest["exported_versions"] = []
        manifest["artifacts"] = [
            {
                "kind": "map_metadata",
                "path": map_path.name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "relation_status": "confirmed",
                "relation_evidence": "Synthetic map relationship only.",
                "artifact_source": "winols_export",
                "coverage_complete": True,
                "manual_review": {
                    "status": "confirmed",
                    "reviewer": "synthetic unit test",
                    "reviewed_at": "2026-01-01",
                    "method": "synthetic fixture",
                    "evidence": "Tests logic only; this is not WinOLS validation.",
                    "confirmed_fields": [
                        "project_relation",
                        "map_inventory",
                        "maps[*].parser_ordinal",
                        "maps[*].name",
                        "maps[*].description",
                        "maps[*].columns",
                        "maps[*].rows",
                        "maps[*].data_type",
                        "maps[*].conversion_factor",
                        "maps[*].conversion_offset",
                        "maps[*].axes[*].role",
                        "maps[*].axes[*].points",
                        "maps[*].axes[*].data_type",
                        "maps[*].axes[*].unit",
                        "maps[*].axes[*].factor",
                        "maps[*].axes[*].conversion_offset",
                        "maps[*].axes[*].identifier",
                        "maps[*].axes[*].description",
                    ],
                    "missing_evidence": [],
                },
            }
        ]
        self._write(manifest)

        report = compare_map_metadata(self.database, self.manifest_path)

        self.assertEqual(report["status"], "FAIL")
        axes = report["matches"][0]["fields"]["axes"]
        self.assertEqual(axes["status"], "FAIL")
        axis_fields = axes["axes"][0]["fields"]
        self.assertEqual(axis_fields["unit"]["status"], "FAIL")
        self.assertEqual(axis_fields["identifier"]["status"], "FAIL")

    def test_package_run_history_is_append_only(self):
        manifest = self._base_manifest()
        self._write(manifest)
        report = inspect_package(self.database, self.manifest_path)
        for _ in range(2):
            self.database.store_reference_package_run(
                project_id=self.project_id,
                source_sha256=report["source"]["indexed_sha256"],
                manifest_path=str(self.manifest_path.resolve()),
                manifest_sha256=report["manifest_sha256"],
                run_type="inspection",
                status=report["status"],
                report=report,
            )
        history = self.database.list_reference_package_runs(self.project_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(
            [item["status"] for item in history],
            ["NOT_TESTED", "NOT_TESTED"],
        )


if __name__ == "__main__":
    unittest.main()
