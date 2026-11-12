import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ols_platform.ols_sample_diagnostics import (
    build_sample_report,
    compare_reference,
    make_reference_template,
)
from ols_platform.reader import (
    ParsedAxis,
    ParsedMap,
    ParsedVersion,
    ParsedVersionSlot,
    PyOlsReader,
    ProjectSnapshot,
)


class SnapshotReader:
    parser_name = "test-parser"
    parser_version = "1"

    def inspect(self, source: Path) -> ProjectSnapshot:
        if not source.is_file():
            raise FileNotFoundError(source)
        return ProjectSnapshot(
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            format_version=597,
            metadata={"make": "", "model": "Test"},
            versions=(ParsedVersion("Original", "", 0, 1, 0, 8, "", 0),),
            maps=(
                ParsedMap(
                    "",
                    "",
                    "MAP",
                    "UWORD",
                    2,
                    1,
                    4,
                    "",
                    1.0,
                    0.0,
                    axes=(
                        ParsedAxis("x", 2, 0, "UWORD", "", 1.0, 0.0, "", ""),
                    ),
                ),
            ),
            version_slots=(
                ParsedVersionSlot(
                    0,
                    0,
                    8,
                    "",
                    "",
                    "4d41524b",
                    hashlib.sha256(b"MARKdata").hexdigest(),
                    hashlib.sha256(b"data").hexdigest(),
                    4,
                    "present_nonempty_unverified",
                    "unresolved",
                ),
            ),
            issues=(),
            raw_counts={"enumerated_binary_slots": 1},
            data_structure={
                "metadata_field_status": {
                    "make": "empty_in_parser_object",
                    "model": "present_in_parser_object",
                }
            },
        )


class OlsSampleDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "sample.ols"
        self.source.write_bytes(b"synthetic test input")
        self.report = build_sample_report(self.source, SnapshotReader())

    def tearDown(self):
        self.temporary.cleanup()

    def test_report_distinguishes_empty_fields_and_incomplete_structures(self):
        self.assertEqual(self.report["metadata"]["make"]["status"], "empty_in_parser_object")
        self.assertEqual(self.report["metadata"]["model"]["status"], "present_in_parser_object")
        self.assertEqual(self.report["counts"]["maps_by_type"], {"MAP": 1})
        self.assertEqual(self.report["counts"]["incomplete_axes"], 1)
        self.assertEqual(self.report["maps"][0]["name_status"], "empty_name")
        self.assertEqual(self.report["maps"][0]["axes"][0]["status"], "structurally_incomplete")
        self.assertEqual(
            compare_reference(self.report, None)["status"],
            "NOT_TESTED",
        )
        self.assertEqual(
            make_reference_template(self.report)["reference"]["validation_status"],
            "NOT_TESTED",
        )

    def test_independent_hashed_reference_compares_exact_fields(self):
        artifact = self.root / "winols-export.json"
        artifact.write_text('{"export":"independent"}', encoding="utf-8")
        manifest = self.root / "sample.json"
        package = {
            "schema_version": 1,
            "sample": {
                "filename": self.source.name,
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            },
            "reference": {
                "source": "independent_winols_export",
                "export_method": "documented test export",
                "software_version": "test-version",
                "artifact_path": artifact.name,
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "project_relation": "confirmed",
                "validation_status": "independently_confirmed",
                "validated_by": "test reviewer",
                "validation_notes": "Test package uses a synthetic export fixture.",
                "coverage_complete": True,
            },
            "observations": {
                "parser.format_version": 597,
                "counts.named_versions": 1,
                "metadata.make.value": "",
                "maps.0.name": "",
                "not_exposed.field": "value",
            },
        }
        manifest.write_text(json.dumps(package), encoding="utf-8")

        result = compare_reference(self.report, manifest)

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(
            [item["status"] for item in result["comparisons"]],
            ["PASS", "PASS", "PASS", "PASS", "NOT_COMPARABLE"],
        )

    def test_complete_comparable_reference_can_pass(self):
        artifact = self.root / "winols-export.json"
        artifact.write_text('{"export":"independent"}', encoding="utf-8")
        manifest = self.root / "sample.json"
        package = {
            "schema_version": 1,
            "sample": {
                "filename": self.source.name,
                "sha256": self.report["source"]["sha256"],
            },
            "reference": {
                "source": "independent_winols_export",
                "export_method": "documented test export",
                "software_version": "test-version",
                "artifact_path": artifact.name,
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "project_relation": "confirmed",
                "validation_status": "independently_confirmed",
                "validated_by": "test reviewer",
                "validation_notes": "Synthetic test fixture only.",
                "coverage_complete": True,
            },
            "observations": {
                "parser.format_version": 597,
                "counts.named_versions": 1,
                "versions.0.name": "Original",
                "maps.0.name": "",
            },
        }
        manifest.write_text(json.dumps(package), encoding="utf-8")

        self.assertEqual(compare_reference(self.report, manifest)["status"], "PASS")
        artifact.write_text('{"export":"tampered"}', encoding="utf-8")
        result = compare_reference(self.report, manifest)
        self.assertEqual(result["status"], "INCONCLUSIVE")
        self.assertIn("SHA-256", result["reason"])

    def test_stale_or_non_independent_reference_cannot_validate_parse(self):
        artifact = self.root / "export.json"
        artifact.write_text("{}", encoding="utf-8")
        manifest = self.root / "sample.json"
        package = {
            "schema_version": 1,
            "sample": {
                "filename": self.source.name,
                "sha256": self.report["source"]["sha256"],
            },
            "reference": {
                "source": "py-ols",
                "export_method": "parser output",
                "software_version": "1.1.0",
                "artifact_path": artifact.name,
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "project_relation": "confirmed",
                "validation_status": "independently_confirmed",
            },
            "observations": {"parser.format_version": 597},
        }
        manifest.write_text(json.dumps(package), encoding="utf-8")

        result = compare_reference(self.report, manifest)

        self.assertEqual(result["status"], "INCONCLUSIVE")
        self.assertIn("not independently confirmed", result["reason"])

        package["sample"]["sha256"] = "stale"
        manifest.write_text(json.dumps(package), encoding="utf-8")
        result = compare_reference(self.report, manifest)
        self.assertEqual(result["status"], "INCONCLUSIVE")
        self.assertIn("exact OLS sample", result["reason"])

    def test_adapter_distinguishes_empty_metadata_from_missing_parser_fields(self):
        class FakeParser:
            data = b""

            def __init__(self, source):
                self.source = source
                self.version = 597
                self.make = ""
                self.engine = None
                self.binary_versions = []
                self.parameters = []
                self.folders = []
                self.cal_block = None
                self.is_kp_file = False
                self.big_endian_data = False

            def parse(self):
                return self

        module = SimpleNamespace(
            OLSReader=FakeParser,
            read_ols=lambda source: source,
            __version__="test",
        )
        with patch.object(PyOlsReader, "_load_parser", return_value=module):
            snapshot = PyOlsReader().inspect(self.source)

        field_status = snapshot.data_structure["metadata_field_status"]
        self.assertEqual(field_status["make"], "empty_in_parser_object")
        self.assertEqual(field_status["engine"], "empty_in_parser_object")
        self.assertEqual(field_status["model"], "missing_from_parser_object")


if __name__ == "__main__":
    unittest.main()
