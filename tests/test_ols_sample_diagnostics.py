import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ols_platform.ols_sample_diagnostics import (
    build_sample_report,
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
            self.report["reference_validation"]["status"],
            "NOT_TESTED",
        )
        self.assertIn(
            "canonical WinOLS reference-package commands",
            self.report["reference_validation"]["reason"],
        )

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
