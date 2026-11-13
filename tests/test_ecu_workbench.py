import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ols_platform.compare import compare_binary_files
from ols_platform.cli import main
from ols_platform.database import IndexDatabase
from ols_platform.ecu_workbench import (
    compare_analyses,
    export_candidates,
    export_preview,
    export_selected_binary,
    get_analysis,
    history,
    import_binary_stream,
    import_reference_artifact,
    list_reference_artifacts,
    reanalyze,
)
from ols_platform.importer import import_sources
from ols_platform.reader import ParsedVersion, ParsedVersionSlot, ProjectSnapshot, ReadIssue


class WorkbenchReader:
    parser_name = "test-parser"
    parser_version = "1"

    def __init__(self, slot_payload: bytes = b"", issues=()):
        self.slot_payload = slot_payload
        self.issues = issues

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
            issues=self.issues,
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

    def _import_project(self, source_name, source_content, slot_payload=b"", issues=()):
        source = self.root / source_name
        source.write_bytes(source_content)
        return next(import_sources(
            sources=[source],
            reader=WorkbenchReader(slot_payload, issues),
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

    def _add_export_artifact(self, project_id, name, content, representation="full_binary"):
        source = self.root / name
        source.write_bytes(content)
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
                    project_id, str(self.root / "manifest.json"),
                    hashlib.sha256(name.encode()).hexdigest(), 0,
                    "binary_version", str(source), hashlib.sha256(content).hexdigest(),
                    len(content), "", None, "unknown", representation, "{}",
                ),
            )
        return cursor.lastrowid, source

    def test_interactive_export_copies_exact_selected_candidate_and_manifest(self):
        self._import_project(
            "candidate.ols", b"project data",
            issues=(ReadIssue("missing_axis", "Axis information is missing."),),
        )
        first_id, first = self._add_export_artifact(1, "first.bin", b"A" * 100)
        second_id, second = self._add_export_artifact(1, "second.bin", b"B" * 100)
        original = self.root / "original.bin"
        original.write_bytes(b"O" * 100)
        output = self.root / "output"
        output.mkdir()
        index_before = list(self.index.connection.iterdump())
        with patch("builtins.input", side_effect=["2", "EXPORT"]), patch("builtins.print"):
            result = main([
                "--database", str(self.index_path), "ecu-bin-export", str(original),
                "--output-dir", str(output), "--ecu-type", "ECU-A",
                "--hw-number", "CONFLICT", "--label", "research",
            ])
        self.assertEqual(result, 0)
        exported = output / "second.bin"
        self.assertEqual(exported.read_bytes(), second.read_bytes())
        self.assertNotEqual(exported.read_bytes(), first.read_bytes())
        manifest = json.loads((output / "second.bin.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_artifact_id"], second_id)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(manifest["original"]["sha256"], hashlib.sha256(original.read_bytes()).hexdigest())
        self.assertEqual(manifest["candidate"]["sha256"], hashlib.sha256(second.read_bytes()).hexdigest())
        self.assertEqual(manifest["output"]["sha256"], manifest["candidate"]["sha256"])
        self.assertTrue(manifest["source_output_sha256_equal"])
        self.assertEqual(manifest["compatibility"], "COMPATIBILITY_NOT_VALIDATED")
        self.assertEqual(manifest["user_label"], "research")
        self.assertEqual(manifest["candidate"]["differing_features"][0]["field"], "hw_number")
        self.assertTrue(manifest["candidate"]["parser_warnings"])
        self.assertEqual(manifest["candidate"]["tuning_type"], "UNKNOWN")
        self.assertEqual(original.read_bytes(), b"O" * 100)
        self.assertEqual(first.read_bytes(), b"A" * 100)
        self.assertEqual(second.read_bytes(), b"B" * 100)
        self.assertEqual(list(self.index.connection.iterdump()), index_before)

    def test_export_requires_confirmation_and_never_overwrites_or_uses_changed_source(self):
        self._import_project("project.ols", b"project")
        artifact_id, source = self._add_export_artifact(1, "candidate.bin", b"source data")
        analysis = self._import_bin("original.bin", b"original", {"ecu_type": "ECU-A"})
        index = SimpleNamespace(connection=self.index.connection)
        preview = export_preview(self.storage, analysis["analysis_id"], artifact_id, index)
        kwargs = dict(
            output_directory=self.root, output_name="candidate.bin",
            confirmation_token=preview["confirmation_token"], confirmed=True,
        )
        with self.assertRaises(FileExistsError):
            export_selected_binary(self.storage, analysis["analysis_id"], artifact_id, index, **kwargs)
        self.assertEqual(source.read_bytes(), b"source data")
        kwargs["output_name"] = "output.bin"
        with self.assertRaisesRegex(ValueError, "confirmation"):
            export_selected_binary(
                self.storage, analysis["analysis_id"], artifact_id, index,
                **{**kwargs, "confirmed": False},
            )
        source.write_bytes(b"changed data")
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            export_selected_binary(self.storage, analysis["analysis_id"], artifact_id, index, **kwargs)
        self.assertFalse((self.root / "output.bin").exists())

    def test_export_candidates_require_evidence_or_explicit_other_project_browse(self):
        self._import_project("project.ols", b"project")
        artifact_id, source = self._add_export_artifact(1, "research.bin", b"candidate", "unknown")
        analysis = self._import_bin("original.bin", b"original")
        index = SimpleNamespace(connection=self.index.connection)
        self.assertEqual(export_candidates(self.storage, analysis["analysis_id"], index)["candidates"], [])
        candidates = export_candidates(
            self.storage, analysis["analysis_id"], index, include_other_projects=True
        )["candidates"]
        self.assertEqual([item["artifact_id"] for item in candidates], [artifact_id])
        self.assertEqual(candidates[0]["match_methods"], ["manual_browse"])
        self.assertEqual(candidates[0]["compatibility"], "COMPATIBILITY_NOT_VALIDATED")
        self.assertEqual(candidates[0]["identifications"]["ecu_type"], "ECU-A")
        self.assertEqual(candidates[0]["matched_features"], [])
        source.unlink()
        with self.assertRaises(FileNotFoundError):
            export_preview(self.storage, analysis["analysis_id"], artifact_id, index)

    def test_cli_no_candidates_empty_input_and_cancel_do_not_export(self):
        original = self.root / "original.bin"
        output = self.root / "output"
        output.mkdir()
        original.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "BIN upload"):
            main(["--database", str(self.index_path), "ecu-bin-export", str(original),
                  "--output-dir", str(output)])
        original.write_bytes(b"original")
        with patch("builtins.input", side_effect=["A", "Q"]), patch("builtins.print"):
            self.assertEqual(main(["--database", str(self.index_path), "ecu-bin-export",
                                   str(original), "--output-dir", str(output)]), 0)
        self.assertEqual(list(output.iterdir()), [])

    def test_existing_manifest_prompts_for_new_name_and_preserves_files(self):
        self._import_project("project.ols", b"project")
        _, source = self._add_export_artifact(1, "candidate.bin", b"candidate")
        original = self.root / "original.bin"
        original.write_bytes(b"original")
        output = self.root / "output"
        output.mkdir()
        existing = output / "candidate.bin.manifest.json"
        existing.write_text("untouched", encoding="utf-8")
        with patch("builtins.input", side_effect=["1", "renamed.bin", "EXPORT"]), patch("builtins.print"):
            self.assertEqual(main([
                "--database", str(self.index_path), "ecu-bin-export", str(original),
                "--output-dir", str(output), "--ecu-type", "ECU-A",
            ]), 0)
        self.assertEqual(existing.read_text(encoding="utf-8"), "untouched")
        self.assertEqual((output / "renamed.bin").read_bytes(), source.read_bytes())
        self.assertEqual(original.read_bytes(), b"original")

    def test_export_rejects_changed_evidence_and_archived_original(self):
        self._import_project("project.ols", b"project")
        artifact_id, _ = self._add_export_artifact(1, "candidate.bin", b"candidate")
        analysis = self._import_bin("original.bin", b"original", {"ecu_type": "ECU-A"})
        index = SimpleNamespace(connection=self.index.connection)
        preview = export_preview(self.storage, analysis["analysis_id"], artifact_id, index)
        with self.index.connection:
            self.index.connection.execute(
                "UPDATE reference_artifacts SET version_name=? WHERE id=?",
                ("changed evidence", artifact_id),
            )
        kwargs = dict(output_directory=self.root, output_name="result.bin",
                      confirmation_token=preview["confirmation_token"], confirmed=True)
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            export_selected_binary(self.storage, analysis["analysis_id"], artifact_id, index, **kwargs)
        Path(analysis["source"]["stored_copy"]).write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "original copy"):
            export_preview(self.storage, analysis["analysis_id"], artifact_id, index)
        self.assertFalse((self.root / "result.bin").exists())

    def test_failed_manifest_write_removes_only_created_output(self):
        self._import_project("project.ols", b"project")
        artifact_id, source = self._add_export_artifact(1, "candidate.bin", b"candidate")
        analysis = self._import_bin("original.bin", b"original", {"ecu_type": "ECU-A"})
        index = SimpleNamespace(connection=self.index.connection)
        preview = export_preview(self.storage, analysis["analysis_id"], artifact_id, index)
        with patch("ols_platform.ecu_workbench.json.dump", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                export_selected_binary(
                    self.storage, analysis["analysis_id"], artifact_id, index,
                    output_directory=self.root, output_name="result.bin",
                    confirmation_token=preview["confirmation_token"], confirmed=True,
                )
        self.assertFalse((self.root / "result.bin").exists())
        self.assertFalse((self.root / "result.bin.manifest.json").exists())
        self.assertEqual(source.read_bytes(), b"candidate")

    def test_exact_original_reference_does_not_claim_sibling_byte_match(self):
        self._import_project("project.ols", b"project")
        exact_id, _ = self._add_export_artifact(1, "original-reference.bin", b"original")
        other_id, _ = self._add_export_artifact(1, "other.bin", b"different")
        analysis = self._import_bin("original.bin", b"original")
        candidates = export_candidates(
            self.storage, analysis["analysis_id"],
            SimpleNamespace(connection=self.index.connection),
        )["candidates"]
        methods = {item["artifact_id"]: item["match_methods"] for item in candidates}
        self.assertIn("indexed_candidate_sha256_matches_original", methods[exact_id])
        self.assertNotIn("indexed_candidate_sha256_matches_original", methods[other_id])

    def test_empty_candidate_is_not_exported(self):
        self._import_project("project.ols", b"project")
        artifact_id, source = self._add_export_artifact(1, "empty.bin", b"")
        analysis = self._import_bin("original.bin", b"original", {"ecu_type": "ECU-A"})
        with self.assertRaisesRegex(ValueError, "candidate is empty"):
            export_preview(self.storage, analysis["analysis_id"], artifact_id,
                           SimpleNamespace(connection=self.index.connection))
        self.assertEqual(source.read_bytes(), b"")

    def test_candidate_pagination_and_preview_after_first_page(self):
        self._import_project("project.ols", b"project")
        artifact_id, source = self._add_export_artifact(1, "candidate.bin", b"candidate")
        with self.index.connection:
            for number in range(500):
                self.index.connection.execute(
                    """
                    INSERT INTO reference_artifacts(
                        project_id,manifest_path,manifest_sha256,artifact_index,kind,path,
                        sha256,size_bytes,version_name,relation_status,representation,metadata_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (1, str(self.root / "manifest.json"), "z" * 64, number,
                     "binary_version", str(source), hashlib.sha256(b"candidate").hexdigest(),
                     9, "", "unknown", "full_binary", "{}"),
                )
            last_id = self.index.connection.execute(
                "SELECT MAX(id) FROM reference_artifacts"
            ).fetchone()[0]
        analysis = self._import_bin("original.bin", b"original", {"ecu_type": "ECU-A"})
        index = SimpleNamespace(connection=self.index.connection)
        first = export_candidates(self.storage, analysis["analysis_id"], index)
        second = export_candidates(self.storage, analysis["analysis_id"], index, offset=500)
        self.assertEqual(len(first["candidates"]), 500)
        self.assertTrue(first["truncated"])
        self.assertEqual([item["artifact_id"] for item in second["candidates"]], [last_id])
        self.assertFalse(second["truncated"])
        self.assertEqual(export_preview(self.storage, analysis["analysis_id"], last_id, index)
                         ["candidate"]["artifact_id"], last_id)
        self.assertNotEqual(artifact_id, last_id)

    def test_streaming_bin_import_persists_hash_and_never_invents_identifiers(self):
        content = bytes(range(256)) * 1024
        original = self.root / "customer.bin"
        original.write_bytes(content)
        analysis = self._import_bin("customer.bin", content)

        self.assertEqual(analysis["source"]["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(analysis["source"]["size_bytes"], len(content))
        self.assertEqual(analysis["identifications"]["automatically_extracted"], {})
        self.assertEqual(analysis["identifications"]["user_provided"], {})
        self.assertEqual(analysis["identifications"]["evidence"]["status"], "UNKNOWN")
        self.assertEqual(
            analysis["report"]["ecu_identity"]["fields"]["ecu_type"]["status"],
            "UNKNOWN",
        )
        self.assertEqual(
            analysis["report"]["ecu_identity"]["format_status"],
            "extension_only",
        )
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
        self.assertEqual(
            analysis["report"]["ecu_identity"]["format"], "raw_ori"
        )
        self.assertEqual(
            analysis["report"]["ecu_identity"]["status"], "UNKNOWN"
        )
        self.assertEqual(
            analysis["report"]["ecu_identity"]["fields"]["ecu_type"]["status"],
            "UNKNOWN",
        )
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
        self.assertEqual(analysis["report"]["status"], "INCONCLUSIVE")
        self.assertEqual(len(exact_matches), 2)
        self.assertEqual(
            {item["status"] for item in exact_matches}, {"INCONCLUSIVE"}
        )
        self.assertEqual(
            analysis["report"]["ecu_identity"]["status"], "INCONCLUSIVE"
        )
        self.assertEqual(
            analysis["report"]["ecu_identity"]["fields"]["ecu_type"]["status"],
            "INCONCLUSIVE",
        )
        with self.index.connection:
            self.index.connection.execute(
                "UPDATE file_locations SET sha256=? WHERE path=? AND file_kind='ols'",
                ("f" * 64, outcome.path),
            )
        stale = self._import_bin("customer.bin", data)
        self.assertEqual(stale["report"]["status"], "INCONCLUSIVE")
        self.assertFalse(
            stale["report"]["references"]["exact_references"][0][
                "validation_matches_current_source"
            ]
        )

    def test_single_validated_binary_reference_derives_parser_reported_identity(self):
        data = b"single validated export"
        outcome = self._import_project("single-reference.ols", b"OLS source")
        self._record_live_ols_hash(outcome)
        project_id = self.index.connection.execute(
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
                    project_id, str(self.root / "single-manifest.json"), "c" * 64,
                    0, "binary_version", str(self.root / "single-reference.bin"),
                    digest, len(data), "Original", 0, "confirmed", "full_binary", "{}",
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

        analysis = self._import_bin("single-customer.bin", data)
        identity = analysis["report"]["ecu_identity"]
        self.assertEqual(analysis["report"]["status"], "CONFIRMED_REFERENCE")
        self.assertEqual(identity["status"], "DERIVED")
        self.assertEqual(identity["fields"]["ecu_type"]["status"], "DERIVED")
        self.assertEqual(identity["fields"]["ecu_type"]["value"], "ECU-A")
        candidates = export_candidates(
            self.storage, analysis["analysis_id"], SimpleNamespace(connection=self.index.connection)
        )["candidates"]
        self.assertIn("ecu_type", candidates[0]["matched_features"])
        self.assertEqual(candidates[0]["original_identification_sources"]["ecu_type"],
                         "DERIVED_FROM_INDEXED_REFERENCE")
        self.assertEqual(
            identity["candidates"][0]["parser"]["name"], "test-parser"
        )
        self.assertEqual(
            identity["candidates"][0]["metadata"]["ecu_type"],
            {
                "value": "ECU-A",
                "status": "direct",
                "source": "project_metadata",
            },
        )
        self.assertEqual(
            identity["candidates"][0]["version_name"], "Original"
        )
        self.assertEqual(identity["parser_status"], "not_run_no_validated_raw_binary_decoder")

    def test_conflicting_project_metadata_keeps_exact_reference_unconfirmed(self):
        data = b"same bytes conflicting project identities"
        first = self._import_project("first-conflict.ols", b"first project")
        second = self._import_project("second-conflict.ols", b"second project")
        self._record_live_ols_hash(first)
        self._record_live_ols_hash(second)
        project_ids = [
            self.index.connection.execute(
                "SELECT id FROM projects WHERE source_id=(SELECT id FROM source_files WHERE path=?)",
                (item.path,),
            ).fetchone()[0]
            for item in (first, second)
        ]
        with self.index.connection:
            self.index.connection.execute(
                "UPDATE project_metadata SET value='ECU-B' "
                "WHERE project_id=? AND field='ecu_type'",
                (project_ids[1],),
            )
            for index, project_id in enumerate(project_ids):
                cursor = self.index.connection.execute(
                    """
                    INSERT INTO reference_artifacts(
                        project_id,manifest_path,manifest_sha256,artifact_index,kind,path,
                        sha256,size_bytes,version_name,source_slot_index,relation_status,
                        representation,metadata_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        project_id, str(self.root / f"conflict-{index}.json"),
                        str(index + 1) * 64, 0, "binary_version",
                        str(self.root / f"conflict-{index}.bin"),
                        hashlib.sha256(data).hexdigest(), len(data), "Original",
                        0, "confirmed", "full_binary", "{}",
                    ),
                )
                source_hash = first.sha256 if index == 0 else second.sha256
                self.index.connection.execute(
                    """
                    INSERT INTO reference_checks(
                        artifact_id,source_sha256,status,message,comparison_json
                    ) VALUES(?,?,'PASS','test fixture','{}')
                    """,
                    (cursor.lastrowid, source_hash),
                )

        analysis = self._import_bin("conflicting.bin", data)
        identity = analysis["report"]["ecu_identity"]
        self.assertEqual(analysis["report"]["status"], "INCONCLUSIVE")
        self.assertEqual(identity["status"], "CONFLICTING")
        self.assertEqual(identity["fields"]["ecu_type"]["status"], "CONFLICTING")
        self.assertIsNone(identity["fields"]["ecu_type"]["value"])

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

    def test_partial_candidate_keeps_evidence_and_warnings_on_reanalysis(self):
        original = b"A" * 100
        candidate_bytes = b"A" * 95 + b"B" * 5
        original_path = self.root / "original.dat"
        candidate_path = self.root / "candidate.dat"
        original_path.write_bytes(original)
        candidate_path.write_bytes(candidate_bytes)
        self.assertEqual(compare_binary_files(original_path, candidate_path).differing_bytes, 5)
        outcome = self._import_project(
            "candidate.ols", b"synthetic project", original,
            (ReadIssue("missing_axis", "Axis information is missing."),),
        )
        self._record_live_ols_hash(outcome)
        analysis = self._import_bin(
            "candidate.bin", candidate_bytes,
            {"ecu_type": "ECU-A", "hw_number": "HW-1", "calibration_id": "unknown"},
        )
        candidates = analysis["report"]["references"]["metadata_candidates"]
        self.assertEqual(len(candidates), 1)
        result = candidates[0]
        self.assertEqual(result["status"], "LIKELY")
        self.assertEqual(result["matched_identifications"], ["ecu_type", "hw_number"])
        self.assertEqual(result["missing_identifications"], ["calibration_id"])
        self.assertTrue(result["parser_warnings"])
        self.assertFalse(result["parser_errors"])
        self.assertTrue(result["rank_reasons"])
        self.assertIn("not probability", result["evidence"])
        self.assertEqual(analysis["report"]["references"]["slot_candidates"], [])
        repeated = reanalyze(
            self.storage, analysis["analysis_id"],
            SimpleNamespace(connection=self.index.connection),
        )["report"]["references"]["metadata_candidates"][0]
        self.assertEqual(result["rank_score"], repeated["rank_score"])
        self.assertEqual(result["rank_reasons"], repeated["rank_reasons"])

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

    def test_byte_similarity_keeps_user_identity_conflict_visible(self):
        first = self._import_bin("first.bin", b"A" * 100, {"ecu_type": "ECU-A"})
        second = self._import_bin(
            "second.bin", b"A" * 95 + b"B" * 5, {"ecu_type": "ECU-B"},
        )
        comparison = compare_analyses(
            self.storage, first["analysis_id"], second["analysis_id"],
            threshold_percent=95,
        )
        self.assertTrue(comparison["byte_similarity"]["meets_threshold"])
        self.assertEqual(comparison["byte_similarity"]["similarity_percent"], 95)
        self.assertEqual(comparison["user_identification_conflicts"], [
            {"field": "ecu_type", "left": "ECU-A", "right": "ECU-B"},
        ])
        self.assertEqual(comparison["semantic_status"], "INCONCLUSIVE")
        below_threshold = compare_analyses(
            self.storage, first["analysis_id"], second["analysis_id"],
            threshold_percent=96,
        )
        self.assertFalse(below_threshold["byte_similarity"]["meets_threshold"])
        with self.assertRaises(ValueError):
            compare_analyses(
                self.storage, first["analysis_id"], second["analysis_id"],
                threshold_percent=True,
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
