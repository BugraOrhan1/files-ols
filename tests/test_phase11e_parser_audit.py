import json
import unittest
from pathlib import Path


class Phase11EParserAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        report_path = (
            Path(__file__).resolve().parent.parent
            / "reports"
            / "phase11e_parser_audit.json"
        )
        cls.report = json.loads(report_path.read_text(encoding="utf-8"))
        cls.samples = {sample["filename"]: sample for sample in cls.report["samples"]}

    def test_report_covers_all_samples_without_claiming_independent_validation(self):
        self.assertEqual(self.report["schema_version"], 1)
        self.assertEqual(self.report["independent_winols_validation"], "NOT_TESTED")
        self.assertEqual(
            set(self.samples),
            {
                "100004.ols",
                "100007.ols",
                "100008.ols",
                "100009.ols",
                "100010.ols",
                "10001004.ols",
                "10001005.ols",
                "100010054.ols",
            },
        )
        for sample in self.samples.values():
            self.assertEqual(
                sample["parser_a"]["independent_validation"],
                "NOT_TESTED",
            )
            self.assertEqual(
                sample["parser_b"]["independent_validation"],
                "NOT_TESTED",
            )
            self.assertTrue(sample["parser_b"]["source_hash_unchanged_during_run"])

    def test_parser_disagreements_are_recorded_without_promoting_either_parser(self):
        expected = {
            "100009.ols": (4, 0),
            "100010054.ols": (12, 0),
        }
        for filename, (parser_a_count, parser_b_count) in expected.items():
            sample = self.samples[filename]
            self.assertEqual(sample["parser_a"]["map_count"], parser_a_count)
            self.assertEqual(sample["parser_b"]["returned_map_count"], parser_b_count)
            self.assertEqual(
                sample["observed_differences"],
                [
                    {
                        "field": "returned_map_definition_count",
                        "parser_a": parser_a_count,
                        "parser_b": parser_b_count,
                        "status": "DIFFERENT_OBSERVATIONS",
                    }
                ],
            )
            self.assertEqual(
                sample["parser_a"]["independent_validation"],
                "NOT_TESTED",
            )

    def test_version_records_are_not_conflated_with_enumerated_slots(self):
        sample = self.samples["10001005.ols"]
        self.assertEqual(sample["parser_a"]["returned_version_count"], 1)
        self.assertEqual(sample["parser_a"]["enumerated_slot_count"], 3)
        self.assertEqual(sample["parser_b"]["returned_version_count"], 3)
        self.assertFalse(sample["version_counts_comparable"])

    def test_payload_hashes_and_segment_bounds_are_explicitly_scoped(self):
        for sample in self.samples.values():
            for slot in sample["parser_a"]["slots"]:
                self.assertEqual(len(slot["payload_sha256"]), 64)
                self.assertEqual(slot["read_status"], "present_nonempty_unverified")
            for segment in sample["parser_b"]["segments"]:
                self.assertIn("source_offset", segment)
                self.assertIn("framing_bytes_reported", segment)
                self.assertEqual(
                    segment["recorded_segment_extent_in_source_bounds"],
                    True,
                )
            self.assertFalse(sample["parser_b"]["payload_sha256_exposed"])


if __name__ == "__main__":
    unittest.main()
