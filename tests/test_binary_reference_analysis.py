import hashlib
import tempfile
import unittest
from pathlib import Path

from ols_platform.binary_reference_analysis import analyze_binary_artifact


class BinaryReferenceAnalysisTests(unittest.TestCase):
    def _project(self, source: Path, slots: list[dict]) -> dict:
        versions = [
            {
                "id": slot["version_id"],
                "name": slot.get("name", ""),
            }
            for slot in slots
            if slot.get("version_id") is not None
        ]
        return {
            "id": 1,
            "path": str(source),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size_bytes": source.stat().st_size,
            "format_version": 597,
            "versions": versions,
            "version_slots": [
                {
                    "slot_index": slot["slot_index"],
                    "version_id": slot.get("version_id"),
                    "blob_offset": slot["blob_offset"],
                    "blob_size": slot["blob_size"],
                    "marker_hex": slot["marker_hex"],
                    "raw_sha256": hashlib.sha256(
                        source.read_bytes()[
                            slot["blob_offset"]:
                            slot["blob_offset"] + slot["blob_size"]
                        ]
                    ).hexdigest(),
                    "payload_sha256": hashlib.sha256(
                        source.read_bytes()[
                            slot["blob_offset"] + 4:
                            slot["blob_offset"] + slot["blob_size"]
                        ]
                    ).hexdigest(),
                    "payload_size": slot["blob_size"] - 4,
                    "binary_status": "present_nonempty_unverified",
                    "name_status": (
                        "named_record" if slot.get("version_id") else "unresolved"
                    ),
                }
                for slot in slots
            ],
        }

    def _artifact(self, reference: Path, **overrides) -> dict:
        result = {
            "id": 1,
            "kind": "binary_version",
            "path": str(reference),
            "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
            "size_bytes": reference.stat().st_size,
            "version_name": "",
            "source_slot_index": None,
            "relation_status": "unknown",
            "representation": "unknown",
            "metadata": {
                "winols_version": "unknown",
                "export_method": "unknown",
                "export_settings": {},
            },
        }
        result.update(overrides)
        return result

    def test_exact_contiguous_range_explains_boundary_without_naming_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "project.ols"
            source.write_bytes(b"MARKabcdTAIL")
            export = root / "export.bin"
            export.write_bytes(b"abcdTAIL")
            project = self._project(
                source,
                [{
                    "slot_index": 0,
                    "version_id": None,
                    "blob_offset": 0,
                    "blob_size": 8,
                    "marker_hex": b"MARK".hex(),
                }],
            )

            report = analyze_binary_artifact(project, self._artifact(export))
            self.assertEqual(len(report["candidate_slots"]), 1)
            slot = report["candidate_slots"][0]
            self.assertEqual(slot["binary_match_status"], "PASS")
            self.assertEqual(slot["version_relation_status"], "INCONCLUSIVE")
            self.assertEqual(slot["extra_bytes"]["position_in_export"], 4)
            self.assertEqual(slot["extra_bytes"]["hex"], b"TAIL".hex())
            self.assertTrue(
                slot["extra_bytes"]["matches_immediately_following_source_bytes"]
            )
            self.assertEqual(
                slot["extra_bytes"]["semantic_interpretation"], "unclassified"
            )
            self.assertTrue(
                slot["comparison"]["complete_contiguous_source_range_exact"]
            )

    def test_nonmatching_suffix_does_not_get_normalized_or_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "project.ols"
            source.write_bytes(b"MARKabcdTAIL")
            export = root / "export.bin"
            export.write_bytes(b"abcdXXXX")
            project = self._project(
                source,
                [{
                    "slot_index": 0,
                    "version_id": None,
                    "blob_offset": 0,
                    "blob_size": 8,
                    "marker_hex": b"MARK".hex(),
                }],
            )

            report = analyze_binary_artifact(project, self._artifact(export))
            self.assertEqual(report["candidate_slots"], [])
            result = report["slot_results"][0]
            self.assertTrue(
                result["comparison"]["payload_prefix_exact"]
            )
            self.assertFalse(
                result["extra_bytes"]["matches_immediately_following_source_bytes"]
            )
            self.assertEqual(result["binary_match_status"], "INCONCLUSIVE")

    def test_multiple_exact_slots_remain_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "project.ols"
            source.write_bytes(b"MARKabcdTAILMARKabcdTAIL")
            export = root / "export.bin"
            export.write_bytes(b"abcdTAIL")
            project = self._project(
                source,
                [
                    {
                        "slot_index": 0,
                        "version_id": None,
                        "blob_offset": 0,
                        "blob_size": 8,
                        "marker_hex": b"MARK".hex(),
                    },
                    {
                        "slot_index": 1,
                        "version_id": None,
                        "blob_offset": 12,
                        "blob_size": 8,
                        "marker_hex": b"MARK".hex(),
                    },
                ],
            )

            report = analyze_binary_artifact(project, self._artifact(export))
            self.assertEqual(
                [slot["slot_index"] for slot in report["candidate_slots"]],
                [0, 1],
            )


if __name__ == "__main__":
    unittest.main()
