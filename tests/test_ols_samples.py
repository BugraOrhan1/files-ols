import os
import tempfile
import unittest
from pathlib import Path

from ols_platform.database import IndexDatabase
from ols_platform.importer import import_sources, stream_sha256
from ols_platform.reader import PyOlsReader


WORKSPACE = Path(__file__).resolve().parents[1]
SAMPLE_SUBDIRECTORY = WORKSPACE / "ols files als ols"
SAMPLE_NAMES = (
    "100004.ols",
    "100007.ols",
    "100008.ols",
    "100009.ols",
    "100010.ols",
    "10001004.ols",
    "10001005.ols",
    "100010054.ols",
)
SAMPLE_DIRECTORY = (
    SAMPLE_SUBDIRECTORY
    if all((SAMPLE_SUBDIRECTORY / name).is_file() for name in SAMPLE_NAMES)
    else WORKSPACE
)
SAMPLE_EXPECTATIONS = {
    "100004.ols": (597, 4, 0, 0),
    "100007.ols": (597, 4, 0, 0),
    "100008.ols": (597, 3, 0, 0),
    "100009.ols": (597, 2, 4, 8),
    "100010.ols": (597, 3, 0, 0),
    "10001004.ols": (503, 3, 0, 0),
    "10001005.ols": (503, 3, 0, 0),
    "100010054.ols": (503, 4, 12, 23),
}


def _parser_path() -> Path | None:
    value = os.environ.get("PY_OLS_PATH")
    if not value:
        return None
    path = Path(value)
    if path.is_dir():
        path /= "ols_reader.py"
    return path if path.is_file() else None


@unittest.skipUnless(
    _parser_path() and all((SAMPLE_DIRECTORY / name).is_file() for name in SAMPLE_NAMES),
    "Set PY_OLS_PATH and provide the eight workspace OLS samples.",
)
class RealOlsSampleTests(unittest.TestCase):
    def test_eight_projects_import_without_losing_parser_output(self):
        parser_path = _parser_path()
        assert parser_path is not None
        reader = PyOlsReader(parser_path)
        paths = [SAMPLE_DIRECTORY / name for name in SAMPLE_NAMES]
        snapshots = {path.name: reader.inspect(path) for path in paths}

        with tempfile.TemporaryDirectory() as directory:
            database = IndexDatabase(Path(directory) / "samples.sqlite3")
            try:
                outcomes = list(import_sources(
                    sources=paths,
                    reader=reader,
                    database=database,
                    max_file_bytes=256 * 1024 * 1024,
                    progress=False,
                ))
                self.assertEqual(len(outcomes), 8)
                self.assertTrue(all(item.status == "partial" for item in outcomes))

                total_slots = 0
                total_maps = 0
                total_axes = 0
                for path in paths:
                    snapshot = snapshots[path.name]
                    source_path = str(path.resolve())
                    project_id = database.connection.execute(
                        """
                        SELECT p.id FROM projects p
                        JOIN source_files s ON s.id=p.source_id
                        WHERE s.path=?
                        """,
                        (source_path,),
                    ).fetchone()["id"]
                    project = database.get_project(project_id)
                    self.assertIsNotNone(project)
                    format_version, expected_slots, expected_maps, expected_axes = (
                        SAMPLE_EXPECTATIONS[path.name]
                    )
                    diagnostic = project["diagnostics"]
                    inventory = diagnostic["version_inventory"]
                    self.assertEqual(project["format_version"], format_version)
                    self.assertEqual(len(project["versions"]), 1)
                    self.assertEqual(project["versions"][0]["name"], "Original")
                    self.assertEqual(len(project["version_slots"]), expected_slots)
                    self.assertEqual(len(project["maps"]), expected_maps)
                    self.assertEqual(
                        sum(len(item["axes"]) for item in project["maps"]),
                        expected_axes,
                    )
                    self.assertEqual(len(project["skipped_elements"]), 1)
                    self.assertEqual(
                        project["skipped_elements"][0]["raw_metadata_json"],
                        snapshot.skipped_elements[0].raw_metadata_json,
                    )
                    self.assertEqual(
                        inventory["enumerated_binary_slots"], expected_slots
                    )
                    self.assertEqual(
                        diagnostic["source"]["sha256"],
                        outcomes[SAMPLE_NAMES.index(path.name)].sha256,
                    )
                    self.assertEqual(stream_sha256(path), diagnostic["source"]["sha256"])
                    self.assertEqual(
                        diagnostic["reference_validation"]["status"],
                        "not_validated",
                    )
                    self.assertEqual(diagnostic["data_structure"], snapshot.data_structure)
                    for field, value in snapshot.metadata.items():
                        self.assertEqual(project["metadata"][field]["value"], value)
                    for field, value in snapshot.raw_counts.items():
                        self.assertEqual(diagnostic["metrics"][field], value)
                    self.assertEqual(
                        [item["code"] for item in diagnostic["errors"]],
                        [item.code for item in snapshot.issues if item.severity == "error"],
                    )
                    self.assertEqual(
                        [item["code"] for item in diagnostic["warnings"]],
                        [item.code for item in snapshot.issues if item.severity != "error"],
                    )
                    self.assertEqual(
                        len(project["maps"]),
                        len(snapshot.maps),
                    )
                    for stored_version, parsed_version in zip(
                        project["versions"], snapshot.versions, strict=True
                    ):
                        self.assertEqual(stored_version["name"], parsed_version.name)
                        self.assertEqual(
                            stored_version["description"], parsed_version.description
                        )
                        self.assertEqual(
                            stored_version["slot_index"], parsed_version.slot_index
                        )
                        self.assertEqual(
                            stored_version["declared_slot_count"],
                            parsed_version.declared_slot_count,
                        )
                        self.assertEqual(
                            stored_version["blob_offset"], parsed_version.blob_offset
                        )
                        self.assertEqual(
                            stored_version["blob_size"], parsed_version.blob_size
                        )
                        self.assertEqual(stored_version["epk"], parsed_version.epk)
                        self.assertEqual(
                            stored_version["epk_offset"], parsed_version.epk_offset
                        )
                        self.assertEqual(
                            stored_version["parent_id"], parsed_version.parent_id
                        )
                        self.assertEqual(
                            bool(stored_version["is_root"]), parsed_version.is_root
                        )
                    for stored_map, parsed_map in zip(
                        project["maps"], snapshot.maps, strict=True
                    ):
                        self.assertEqual(stored_map["name"], parsed_map.name)
                        self.assertEqual(
                            stored_map["description"], parsed_map.description
                        )
                        self.assertEqual(stored_map["map_type"], parsed_map.map_type)
                        self.assertEqual(stored_map["data_type"], parsed_map.data_type)
                        self.assertEqual(
                            stored_map["columns_count"], parsed_map.columns
                        )
                        self.assertEqual(stored_map["rows_count"], parsed_map.rows)
                        self.assertEqual(
                            stored_map["data_offset"], parsed_map.data_offset
                        )
                        self.assertEqual(stored_map["folder_id"], parsed_map.folder_id)
                        self.assertEqual(stored_map["type_code"], parsed_map.type_code)
                        self.assertEqual(
                            stored_map["version_relation_status"],
                            parsed_map.version_relation_status,
                        )
                        self.assertEqual(len(stored_map["axes"]), len(parsed_map.axes))
                        for stored_axis, parsed_axis in zip(
                            stored_map["axes"], parsed_map.axes, strict=True
                        ):
                            self.assertEqual(stored_axis["role"], parsed_axis.role)
                            self.assertEqual(
                                stored_axis["points"], parsed_axis.points
                            )
                            self.assertEqual(
                                stored_axis["address"], parsed_axis.address
                            )
                            self.assertEqual(
                                stored_axis["data_type"], parsed_axis.data_type
                            )
                            self.assertEqual(stored_axis["unit"], parsed_axis.unit)
                            self.assertEqual(
                                stored_axis["factor"], parsed_axis.factor
                            )
                            self.assertEqual(
                                stored_axis["conversion_offset"], parsed_axis.offset
                            )
                            self.assertEqual(
                                stored_axis["identifier"], parsed_axis.identifier
                            )
                            self.assertEqual(
                                stored_axis["description"], parsed_axis.description
                            )
                            self.assertEqual(
                                stored_axis["data_source"], parsed_axis.data_source
                            )
                    for slot in project["version_slots"]:
                        self.assertEqual(slot["payload_size"], slot["blob_size"] - 4)
                        self.assertEqual(len(slot["payload_sha256"]), 64)
                    for stored_slot, parsed_slot in zip(
                        project["version_slots"],
                        snapshot.version_slots,
                        strict=True,
                    ):
                        self.assertEqual(
                            stored_slot["slot_index"], parsed_slot.slot_index
                        )
                        self.assertEqual(
                            stored_slot["blob_offset"], parsed_slot.blob_offset
                        )
                        self.assertEqual(
                            stored_slot["blob_size"], parsed_slot.blob_size
                        )
                        self.assertEqual(
                            stored_slot["marker_hex"], parsed_slot.marker_hex
                        )
                        self.assertEqual(
                            stored_slot["raw_sha256"], parsed_slot.raw_sha256
                        )
                        self.assertEqual(
                            stored_slot["payload_sha256"],
                            parsed_slot.payload_sha256,
                        )
                        self.assertEqual(
                            stored_slot["payload_size"], parsed_slot.payload_size
                        )
                        self.assertEqual(
                            stored_slot["binary_status"], parsed_slot.binary_status
                        )
                        self.assertEqual(
                            stored_slot["name_status"], parsed_slot.name_status
                        )
                    total_slots += len(project["version_slots"])
                    total_maps += len(project["maps"])
                    total_axes += sum(len(item["axes"]) for item in project["maps"])

                self.assertEqual(total_slots, 26)
                self.assertEqual(total_maps, 16)
                self.assertEqual(total_axes, 31)
                self.assertEqual(database.summary()["unlabeled_binary_slots"], 18)
                self.assertEqual(
                    database.summary()["projects_needing_reference_validation"], 8
                )
                repeated = list(import_sources(
                    sources=paths,
                    reader=reader,
                    database=database,
                    max_file_bytes=256 * 1024 * 1024,
                    progress=False,
                ))
                self.assertTrue(
                    all(item.status == "skipped_unchanged" for item in repeated)
                )
                self.assertEqual(database.summary()["projects"], 8)
            finally:
                database.close()

    def test_damaged_file_is_reported_as_failed_by_real_parser(self):
        parser_path = _parser_path()
        assert parser_path is not None
        reader = PyOlsReader(parser_path)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "damaged.ols"
            source.write_bytes(b"not an OLS project")
            snapshot = reader.inspect(source)
            self.assertLessEqual(snapshot.format_version, 0)
            self.assertTrue(any(issue.severity == "error" for issue in snapshot.issues))

            database = IndexDatabase(root / "damaged.sqlite3")
            try:
                outcome = next(import_sources(
                    sources=[source],
                    reader=reader,
                    database=database,
                    max_file_bytes=1024,
                    progress=False,
                ))
                self.assertEqual(outcome.status, "failed")
                report = database.list_diagnostics(
                    processing_status="failed",
                    parser_errors=True,
                )
                self.assertEqual(len(report), 1)
                self.assertEqual(report[0]["path"], str(source.resolve()))
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
