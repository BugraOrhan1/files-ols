import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ols_platform.database import IndexDatabase
from ols_platform.ecu_workbench import (
    compare_analyses,
    get_analysis,
    history,
    import_binary_stream,
    import_reference_artifact,
    list_reference_artifacts,
    reanalyze,
)
from ols_platform.importer import import_sources
from ols_platform.reader import ParsedVersion, ParsedVersionSlot, ProjectSnapshot


class WorkbenchReader:
    parser_name = "test-parser"
    parser_version = "1"

    def __init__(self, slot_payload: bytes = b""):
        self.slot_payload = slot_payload

    def inspect(self, source):
        digest = hashlib.sha256(self.slot_payload).hexdigest()
        return ProjectSnapshot(
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            format_version=503,
            metadata={
                "ecu_type": "ECU-A",
                "hw_number": "HW-1",
                "sw_number": "SW-1",
            },
            versions=(ParsedVersion("Original", "", 0, 1, 0, 8, "", 0),),
            maps=(),
            version_slots=(
                ParsedVersionSlot(
                    0, 0, len(self.slot_payload), "Original", "", "",
                    digest, digest, len(self.slot_payload),
                    "present_nonempty_unverified", "named_record",
                ),
            ) if self.slot_payload else (),
        )


class EcuWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.index_path = self.root / "index.sqlite3"
        self.index = IndexDatabase(self.index_path)
        self.storage = self.root / "ecu-workbench"

    def tearDown(self):
        self.index.close()
        self.temporary.cleanup()

    def _import_bin(self, filename, content, metadata=None):
        return import_binary_stream(
            io.BytesIO(content),
            filename=filename,
            expected_size=len(content),
            storage_root=self.storage,
            index_database=SimpleNamespace(connection=self.index.connection),
            identifications=metadata,
        )

    def _import_project(self, source_name, source_content, slot_payload=b""):
        source = self.root / source_name
        source.write_bytes(source_content)
        return next(import_sources(
            sources=[source],
            reader=WorkbenchReader(slot_payload),
            database=self.index,
            max_file_bytes=1024 * 1024,
            progress=False,
        ))

    def _record_live_ols_hash(self, outcome):
        source = Path(outcome.path)
        source_id = self.index.register_source(
            source_key="test-live-hash",
            path=str(source.parent),
            status="available",
        )
        self.index.record_file_location(
            source_id=source_id,
            path=str(source.resolve()),
            file_kind="ols",
            size_bytes=source.stat().st_size,
            modified_ns=source.stat().st_mtime_ns,
            sha256=outcome.sha256,
            state="hashed",
        )

    def test_streaming_bin_import_persists_hash_and_never_invents_identifiers(self):
        content = bytes(range(256)) * 1024
        original = self.root / "customer.bin"
        original.write_bytes(content)
        analysis = self._import_bin("customer.bin", content)

        self.assertEqual(analysis["source"]["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(analysis["source"]["size_bytes"], len(content))
        self.assertEqual(analysis["identifications"]["automatically_extracted"], {})
        self.assertEqual(analysis["identifications"]["user_provided"], {})
        self.assertEqual(original.read_bytes(), content)
        self.assertEqual(
            Path(analysis["source"]["stored_copy"]).read_bytes(), content
        )
        self.assertEqual(analysis["report"]["status"], "NO_MATCH")
        self.assertEqual(len(history(self.storage)["analyses"]), 1)

    def test_ori_import_is_hashed_as_raw_bytes_and_reanalysis_keeps_its_format(self):
        content = b"raw original binary data"
        original = self.root / "customer.ori"
        original.write_bytes(content)

        analysis = self._import_bin("customer.ori", content)

        self.assertEqual(analysis["source"]["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(analysis["source"]["size_bytes"], len(content))
        self.assertEqual(analysis["report"]["file"]["format"], "raw_ori")
        self.assertEqual(original.read_bytes(), content)

        rerun = reanalyze(
            self.storage,
            analysis["analysis_id"],
            SimpleNamespace(connection=self.index.connection),
        )
        self.assertEqual(rerun["report"]["file"]["format"], "raw_ori")

    def test_rejects_unsupported_and_incomplete_uploads(self):
        with self.assertRaisesRegex(ValueError, "Only raw .bin and .ori"):
            self._import_bin("firmware.hex", b":00000001FF")
        with self.assertRaisesRegex(ValueError, "ended early"):
            import_binary_stream(
                io.BytesIO(b"short"),
                filename="short.bin",
                expected_size=10,
                storage_root=self.storage,
                index_database=SimpleNamespace(connection=self.index.connection),
            )
        self.assertEqual(history(self.storage)["analyses"], [])

    def test_missing_indexed_reference_path_is_reported_without_analysis(self):
        outcome = self._import_project("missing-reference-project.ols", b"OLS source")
        project_id = self.index.connection.execute(
            "SELECT id FROM projects WHERE source_id=(SELECT id FROM source_files WHERE path=?)",
            (outcome.path,),
        ).fetchone()[0]
        with self.index.connection:
            cursor = self.index.connection.execute(
                """
                INSERT INTO reference_artifacts(
                    project_id,manifest_path,manifest_sha256,artifact_index,kind,path,
                    sha256,size_bytes,version_name,source_slot_index,relation_status,
                    representation,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    project_id, str(self.root / "manifest.json"), "a" * 64, 0,
                    "binary_version", str(self.root / "missing-reference.bin"),
                    hashlib.sha256(b"missing").hexdigest(), len(b"missing"),
                    "Original", 0, "confirmed", "full_binary", "{}",
                ),
            )
        with self.assertRaises(FileNotFoundError):
            import_reference_artifact(
                self.storage,
                cursor.lastrowid,
                SimpleNamespace(connection=self.index.connection),
            )
        self.assertEqual(history(self.storage)["analyses"], [])

    def test_ols_source_hash_is_not_treated_as_raw_bin_reference_hash(self):
        content = b"same bytes but a different representation label"
        outcome = self._import_project("source.ols", content)
        self._record_live_ols_hash(outcome)

        analysis = self._import_bin("copy.bin", content)

        self.assertEqual(analysis["report"]["status"], "NO_MATCH")
        self.assertEqual(
            analysis["report"]["references"]["exact_references"], []
        )

    def test_full_binary_reference_requires_representation_and_validation(self):
        data = b"verified binary export"
        outcome = self._import_project("project.ols", b"OLS source")
        self._record_live_ols_hash(outcome)
        analysis_project_id = self.index.connection.execute(
            "SELECT id FROM projects WHERE source_id=(SELECT id FROM source_files WHERE path=?)",
            (outcome.path,),
        ).fetchone()[0]
        digest = hashlib.sha256(data).hexdigest()
        with self.index.connection:
            cursor = self.index.connection.execute(
                """
                INSERT INTO reference_artifacts(
                    project_id,manifest_path,manifest_sha256,artifact_index,kind,path,
                    sha256,size_bytes,version_name,source_slot_index,relation_status,
                    representation,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    analysis_project_id, str(self.root / "manifest.json"), "a" * 64,
                    0, "binary_version", str(self.root / "reference.bin"), digest,
                    len(data), "Original", 0, "confirmed", "full_binary", "{}",
                ),
            )
            self.index.connection.execute(
                """
                INSERT INTO reference_checks(
                    artifact_id,source_sha256,status,message,comparison_json
                ) VALUES(?,?,'PASS','test fixture','{}')
                """,
                (cursor.lastrowid, outcome.sha256),
            )
            self.index.connection.execute(
                """
                INSERT INTO reference_artifacts(
                    project_id,manifest_path,manifest_sha256,artifact_index,kind,path,
                    sha256,size_bytes,version_name,source_slot_index,relation_status,
                    representation,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    analysis_project_id, str(self.root / "other-manifest.json"),
                    "b" * 64, 0, "binary_version",
                    str(self.root / "unvalidated-reference.bin"), digest,
                    len(data), "Modified", 1, "unverified", "full_binary", "{}",
                ),
            )

        analysis = self._import_bin("customer.bin", data)
        exact_matches = analysis["report"]["references"]["exact_references"]
        self.assertEqual(analysis["report"]["status"], "CONFIRMED_REFERENCE")
        self.assertEqual(len(exact_matches), 2)
        self.assertEqual(
            {item["status"] for item in exact_matches},
            {"CONFIRMED_REFERENCE", "EXACT_CONTENT_MATCH"},
        )
        reference = next(
            item for item in exact_matches
            if item["status"] == "CONFIRMED_REFERENCE"
        )
        with self.index.connection:
            self.index.connection.execute(
                "UPDATE file_locations SET sha256=? WHERE path=? AND file_kind='ols'",
                ("f" * 64, outcome.path),
            )
        stale = self._import_bin("customer.bin", data)
        self.assertEqual(stale["report"]["status"], "EXACT_CONTENT_MATCH")
        self.assertFalse(
            stale["report"]["references"]["exact_references"][0][
                "validation_matches_current_source"
            ]
        )

    def test_reference_artifact_is_rehashed_before_archival(self):
        data = b"reference version bytes"
        outcome = self._import_project("project.ols", b"OLS source")
        self._record_live_ols_hash(outcome)
        project_id = self.index.connection.execute(
            "SELECT id FROM projects WHERE source_id=(SELECT id FROM source_files WHERE path=?)",
            (outcome.path,),
        ).fetchone()[0]
        artifact_path = self.root / "reference.bin"
        artifact_path.write_bytes(data)
        with self.index.connection:
            cursor = self.index.connection.execute(
                """
                INSERT INTO reference_artifacts(
                    project_id,manifest_path,manifest_sha256,artifact_index,kind,path,
                    sha256,size_bytes,version_name,source_slot_index,relation_status,
                    representation,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    project_id, str(self.root / "manifest.json"), "a" * 64, 0,
                    "binary_version", str(artifact_path),
                    hashlib.sha256(data).hexdigest(), len(data), "Original", 0,
                    "confirmed", "full_binary", "{}",
                ),
            )
            self.index.connection.execute(
                """
                INSERT INTO reference_checks(
                    artifact_id,source_sha256,status,message,comparison_json
                ) VALUES(?,?,'PASS','test fixture','{}')
                """,
                (cursor.lastrowid, outcome.sha256),
            )

        available = list_reference_artifacts(
            SimpleNamespace(connection=self.index.connection)
        )
        self.assertEqual(available[0]["status"], "CONFIRMED_REFERENCE")
        imported = import_reference_artifact(
            self.storage, available[0]["artifact_id"],
            SimpleNamespace(connection=self.index.connection),
        )
        self.assertEqual(imported["report"]["status"], "CONFIRMED_REFERENCE")
        self.assertEqual(Path(imported["source"]["stored_copy"]).read_bytes(), data)

        artifact_path.write_bytes(b"changed after reference registration")
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            import_reference_artifact(
                self.storage, available[0]["artifact_id"],
                SimpleNamespace(connection=self.index.connection),
            )

    def test_parser_slot_match_stays_inconclusive_and_stale_metadata_is_not_likely(self):
        payload = b"candidate payload"
        outcome = self._import_project("candidate.ols", b"OLS source", payload)
        self._record_live_ols_hash(outcome)
        slot_match = self._import_bin("payload.bin", payload)
        self.assertEqual(slot_match["report"]["status"], "INCONCLUSIVE")
        self.assertEqual(
            slot_match["report"]["references"]["slot_candidates"][0]["status"],
            "INCONCLUSIVE",
        )

        metadata_match = self._import_bin(
            "ecu.bin",
            b"different raw image",
            {"ecu_type": "ECU-A", "hw_number": "HW-1"},
        )
        candidate = metadata_match["report"]["references"]["metadata_candidates"][0]
        self.assertEqual(candidate["status"], "LIKELY")
        self.assertTrue(candidate["parser_snapshot_matches_current_content"])
        differing = self._import_bin(
            "differing-id.bin",
            b"another raw image",
            {
                "ecu_type": "ECU-A",
                "hw_number": "HW-OTHER",
                "sw_number": "SW-1",
            },
        )
        identity_candidate = differing["report"]["references"]["metadata_candidates"][0]
        self.assertEqual(identity_candidate["status"], "INCONCLUSIVE")
        self.assertEqual(
            identity_candidate["differing_identifications"][0]["field"],
            "hw_number",
        )
        with self.index.connection:
            self.index.connection.execute(
                "UPDATE file_locations SET sha256=? WHERE path=? AND file_kind='ols'",
                ("f" * 64, outcome.path),
            )
        stale = self._import_bin(
            "stale.bin",
            b"another raw image",
            {"ecu_type": "ECU-A", "hw_number": "HW-1"},
        )
        self.assertEqual(stale["report"]["status"], "INCONCLUSIVE")
        self.assertEqual(
            stale["report"]["references"]["metadata_candidates"][0]["status"],
            "INCONCLUSIVE",
        )

    def test_reanalysis_appends_history_and_comparison_reports_ranges(self):
        first = self._import_bin("original.bin", b"012345")
        second = self._import_bin("tuned.bin", b"01XY45!")
        comparison = compare_analyses(
            self.storage,
            first["analysis_id"],
            second["analysis_id"],
        )
        self.assertEqual(comparison["differing_bytes"], 3)
        self.assertEqual(
            comparison["changed_ranges"],
            [{"start": 2, "end_exclusive": 4}, {"start": 6, "end_exclusive": 7}],
        )
        self.assertEqual(comparison["semantic_status"], "INCONCLUSIVE")

        rerun = reanalyze(
            self.storage,
            first["analysis_id"],
            SimpleNamespace(connection=self.index.connection),
        )
        self.assertNotEqual(rerun["analysis_id"], first["analysis_id"])
        self.assertEqual(rerun["parent_analysis_id"], first["analysis_id"])
        self.assertEqual(
            get_analysis(self.storage, first["analysis_id"])["analysis_id"],
            first["analysis_id"],
        )
        saved = history(self.storage)
        self.assertEqual(len(saved["analyses"]), 3)
        self.assertEqual(len(saved["comparisons"]), 1)


if __name__ == "__main__":
    unittest.main()
