import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ols_platform.database import IndexDatabase
from ols_platform.importer import import_sources
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


def _fake_parser_class(
    *,
    data: bytes = b"",
    version: int = 597,
    binary_versions=(),
    parameters=(),
    map_record=None,
    parse_error: Exception | None = None,
):
    class FakeParser:
        def __init__(self, source):
            self.source = source
            self.data = data
            self.version = version
            self.make = ""
            self.binary_versions = list(binary_versions)
            self.parameters = list(parameters)
            self.folders = []
            self.cal_block = None
            self.is_kp_file = False
            self.big_endian_data = False

        def _seq_read_one_map(self):
            return map_record

        def parse(self):
            if parse_error is not None:
                raise parse_error
            if map_record is not None:
                self._seq_read_one_map()
            return self

    return FakeParser


def _reader_for_parser(parser_class):
    module = SimpleNamespace(
        OLSReader=parser_class,
        read_ols=lambda source: source,
        __version__="test",
    )
    with patch.object(PyOlsReader, "_load_parser", return_value=module):
        return PyOlsReader()


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

    def test_malformed_header_is_reported_as_parser_failure(self):
        parser = _fake_parser_class(parse_error=ValueError("invalid OLS header"))
        snapshot = _reader_for_parser(parser).inspect(self.source)

        self.assertEqual(snapshot.format_version, 0)
        self.assertIn("parser_failure", [issue.code for issue in snapshot.issues])

    def test_partial_parser_output_is_not_promoted_after_parse_exception(self):
        map_record = SimpleNamespace(name="partially parsed")

        class FakeParser:
            data = b""

            def __init__(self, source):
                self.source = source
                self.version = 597
                self.make = ""
                self.binary_versions = []
                self.parameters = [map_record]

            def _seq_read_one_map(self):
                return map_record

            def parse(self):
                self._seq_read_one_map()
                raise ValueError("truncated map table")

        module = SimpleNamespace(
            OLSReader=FakeParser,
            read_ols=lambda source: source,
            __version__="test",
        )
        with patch.object(PyOlsReader, "_load_parser", return_value=module):
            snapshot = PyOlsReader().inspect(self.source)

        self.assertEqual(snapshot.maps, ())
        self.assertEqual(snapshot.versions, ())
        self.assertEqual([issue.code for issue in snapshot.issues], ["parser_failure"])
        self.assertIn("truncated map table", snapshot.issues[0].message)

    def test_supported_format_versions_are_reported_as_observed(self):
        for version in (503, 597):
            with self.subTest(version=version):
                parser = _fake_parser_class(
                    data=self.source.read_bytes(), version=version
                )
                snapshot = _reader_for_parser(parser).inspect(self.source)
                self.assertEqual(snapshot.format_version, version)

    def test_binary_slot_bounds_and_layout_conflicts_are_explicit(self):
        self.source.write_bytes(b"0123456789abcdef")
        versions = [
            ParsedVersion("One", "", 0, 2, 4, 8, "", 0),
        ]

        slots, layout, issues = PyOlsReader._enumerate_binary_slots(
            self.source, versions, self.source.stat().st_size
        )

        self.assertEqual(layout["status"], "enumerated_from_parser_binary_record")
        self.assertEqual(
            [(slot.blob_offset, slot.blob_size, slot.binary_status) for slot in slots],
            [(4, 8, "present_nonempty_unverified"), (12, 8, "out_of_file_bounds")],
        )
        self.assertIn("binary_slot_out_of_bounds", [issue.code for issue in issues])

        conflicting_layouts = [
            ParsedVersion("One", "", 0, 2, 4, 8, "", 0),
            ParsedVersion("Two", "", 1, 2, 13, 8, "", 0),
        ]
        slots, layout, issues = PyOlsReader._enumerate_binary_slots(
            self.source, conflicting_layouts, self.source.stat().st_size
        )
        self.assertEqual(slots, [])
        self.assertEqual(layout["status"], "inconsistent")
        self.assertIn("inconsistent_binary_layout", [issue.code for issue in issues])

    def test_conflicting_version_records_do_not_choose_a_name_or_break_import(self):
        self.source.write_bytes(b"MARKdata")
        versions = [
            SimpleNamespace(
                name="Original",
                description="",
                version_index=0,
                version_count=1,
                blob_offset=0,
                blob_size=8,
            ),
            SimpleNamespace(
                name="Modified",
                description="",
                version_index=0,
                version_count=1,
                blob_offset=0,
                blob_size=8,
            ),
        ]
        parser = _fake_parser_class(
            data=self.source.read_bytes(), binary_versions=versions
        )
        reader = _reader_for_parser(parser)
        snapshot = reader.inspect(self.source)
        self.assertEqual(snapshot.versions, ())
        self.assertEqual(snapshot.raw_counts["named_version_records"], 2)
        self.assertEqual(snapshot.raw_counts["conflicting_version_slots"], 1)
        self.assertEqual(
            len(snapshot.data_structure["version_record_observations"]), 2
        )
        self.assertIn(
            "conflicting_version_records",
            [issue.code for issue in snapshot.issues],
        )
        database = IndexDatabase(self.root / "temporary.sqlite3")
        try:
            outcome = next(
                import_sources(
                    sources=[self.source],
                    reader=reader,
                    database=database,
                    max_file_bytes=1024,
                    progress=False,
                )
            )
            self.assertEqual(outcome.status, "partial")
            stored_versions = database.connection.execute(
                "SELECT name FROM project_versions"
            ).fetchall()
            stored_slot = database.connection.execute(
                """
                SELECT v.name, s.name_status, s.version_id
                FROM project_version_slots s
                LEFT JOIN project_versions v ON v.id=s.version_id
                """
            ).fetchone()
        finally:
            database.close()

        self.assertEqual(stored_versions, [])
        self.assertEqual(tuple(stored_slot), (None, "conflicting_records", None))

    def test_exact_duplicate_version_records_are_deduplicated_for_storage(self):
        self.source.write_bytes(b"MARKdata")
        version = SimpleNamespace(
            name="Original",
            description="",
            version_index=0,
            version_count=1,
            blob_offset=0,
            blob_size=8,
        )
        parser = _fake_parser_class(
            data=self.source.read_bytes(), binary_versions=[version, version]
        )
        database = IndexDatabase(self.root / "duplicate.sqlite3")
        try:
            outcome = next(
                import_sources(
                    sources=[self.source],
                    reader=_reader_for_parser(parser),
                    database=database,
                    max_file_bytes=1024,
                    progress=False,
                )
            )
            count = database.connection.execute(
                "SELECT COUNT(*) FROM project_versions"
            ).fetchone()[0]
        finally:
            database.close()

        self.assertEqual(outcome.status, "partial")
        self.assertEqual(count, 1)
        self.assertIn("duplicate_version_records", outcome.message)

    def test_parser_data_size_cannot_expand_source_slot_bounds(self):
        self.source.write_bytes(b"tiny")
        version = SimpleNamespace(
            name="Original",
            description="",
            version_index=0,
            version_count=1,
            blob_offset=0,
            blob_size=8,
        )
        parser = _fake_parser_class(data=b"larger-than-source", binary_versions=[version])

        snapshot = _reader_for_parser(parser).inspect(self.source)

        issue_codes = [issue.code for issue in snapshot.issues]
        self.assertIn("parser_source_size_mismatch", issue_codes)
        self.assertIn("binary_slot_out_of_bounds", issue_codes)
        self.assertEqual(snapshot.version_slots[0].binary_status, "out_of_file_bounds")

    def test_truncated_slot_read_is_reported_with_actual_payload_size(self):
        self.source.write_bytes(b"MARK")
        version = ParsedVersion("Original", "", 0, 1, 0, 8, "", 0)

        slots, _, issues = PyOlsReader._enumerate_binary_slots(
            self.source, [version], file_size=8
        )

        self.assertEqual(slots[0].binary_status, "short_read")
        self.assertEqual(slots[0].payload_size, 0)
        self.assertIn("binary_slot_short_read", [issue.code for issue in issues])

    def test_invalid_map_offsets_and_dimensions_are_not_silently_accepted(self):
        self.source.write_bytes(b"0123456789")
        maps = [
            SimpleNamespace(
                name="",
                description="",
                param_type="MAP",
                data_type="UWORD",
                cols=0,
                rows=2,
                data_offset=-1,
                unit="",
                factor=1.0,
                offset=0.0,
                folder_id=0,
                type_code=0,
                x_axis=None,
                y_axis=None,
            ),
            SimpleNamespace(
                name="",
                description="",
                param_type="MAP",
                data_type="UWORD",
                cols=5,
                rows=3,
                data_offset=0,
                unit="",
                factor=1.0,
                offset=0.0,
                folder_id=0,
                type_code=0,
                x_axis=None,
                y_axis=None,
            ),
        ]
        parser = _fake_parser_class(data=self.source.read_bytes(), parameters=maps)

        snapshot = _reader_for_parser(parser).inspect(self.source)
        issue_codes = [issue.code for issue in snapshot.issues]

        self.assertIn("unresolved_map_dimensions", issue_codes)
        self.assertIn("invalid_map_data_offset", issue_codes)
        self.assertIn("map_dimensions_exceed_source_bounds", issue_codes)

    def test_unreasonable_dimensions_are_rejected_without_integer_overflow(self):
        issues = PyOlsReader._map_structure_issues(
            ParsedMap(
                "huge",
                "",
                "MAP",
                "UWORD",
                10**100,
                10**100,
                0,
                "",
                1.0,
                0.0,
            ),
            source_size=1024,
        )

        self.assertIn(
            "map_dimensions_exceed_source_bounds",
            [issue.code for issue in issues],
        )

    def test_unknown_format_version_and_parser_success_do_not_claim_validation(self):
        parser = _fake_parser_class(data=self.source.read_bytes(), version=9999)
        report = build_sample_report(self.source, _reader_for_parser(parser))

        self.assertEqual(report["parser"]["format_version_evidence"], "NOT_TESTED")
        self.assertEqual(report["reference_validation"]["status"], "NOT_TESTED")

    def test_repeated_diagnostics_are_deterministic_for_unchanged_input(self):
        reader = _reader_for_parser(
            _fake_parser_class(data=self.source.read_bytes(), version=597)
        )

        first = build_sample_report(self.source, reader)
        second = build_sample_report(self.source, reader)

        self.assertEqual(first, second)
        self.assertEqual(first["source"]["size_bytes"], self.source.stat().st_size)
        self.assertEqual(
            first["source"]["sha256"],
            hashlib.sha256(self.source.read_bytes()).hexdigest(),
        )

    def test_partial_map_records_are_reported_against_declared_count(self):
        data = bytearray(0x110)
        data[0x100:0x104] = b"\x77\x33\x88\x11"
        data[0x104] = 0
        data[0x105:0x109] = (2).to_bytes(4, "little")
        map_record = SimpleNamespace(name="one")
        parser = _fake_parser_class(
            data=bytes(data),
            map_record=map_record,
            parameters=[
                SimpleNamespace(
                    name="",
                    description="",
                    param_type="MAP",
                    data_type="UWORD",
                    cols=2,
                    rows=2,
                    data_offset=0,
                    unit="",
                    factor=1.0,
                    offset=0.0,
                    folder_id=0,
                    type_code=0,
                    x_axis=None,
                    y_axis=None,
                )
            ],
        )

        snapshot = _reader_for_parser(parser).inspect(self.source)

        self.assertIn(
            "map_records_not_returned",
            [issue.code for issue in snapshot.issues],
        )


if __name__ == "__main__":
    unittest.main()
