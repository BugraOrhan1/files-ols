"""Flash-tool checksum rules with wildcards and scaled intermediate publishing."""

import unittest

from ols_platform.tuning.background import publish_due
from ols_platform.tuning.package import contract_for

OK = {"contract": "FLASH_TOOL_CORRECTS_ON_WRITE", "evidence": "AutoTuner corrigeert"}


class ContractRuleTests(unittest.TestCase):
    def test_exact_key_wins_over_rule(self):
        contracts = {"MG1CS002|6291456": {"contract": "UNKNOWN"}, "MG1*": OK}
        self.assertEqual(contract_for(contracts, "mg1cs002|6291456")["contract"], "UNKNOWN")

    def test_rule_matches_every_size(self):
        found = contract_for({"MG1*": OK}, "MG1CS002|8388608")
        self.assertEqual(found["contract"], "FLASH_TOOL_CORRECTS_ON_WRITE")
        self.assertEqual(found["rule"], "MG1*")

    def test_rule_with_size(self):
        contracts = {"EDC17C*|4194304": OK}
        self.assertEqual(contract_for(contracts, "EDC17C46|4194304")["contract"], OK["contract"])
        self.assertEqual(contract_for(contracts, "EDC17C46|2097152")["contract"], "UNKNOWN")

    def test_most_specific_rule_wins(self):
        contracts = {"MG1*": OK, "MG1CS00*": {"contract": "UNKNOWN", "evidence": "niet zeker"}}
        self.assertEqual(contract_for(contracts, "MG1CS002|6291456")["rule"], "MG1CS00*")

    def test_no_rule_is_unknown(self):
        self.assertEqual(contract_for({"MG1*": OK}, "EDC16C34|2097152")["contract"], "UNKNOWN")


class PublishScheduleTests(unittest.TestCase):
    def test_small_library_publishes_every_batch(self):
        self.assertTrue(publish_due(600, 0, 60, 0, 500, 1800))

    def test_large_library_needs_a_quarter_more(self):
        self.assertFalse(publish_due(24_000, 23_400, 600, 60, 500, 1800))
        self.assertTrue(publish_due(30_000, 23_400, 600, 60, 500, 1800))

    def test_long_training_spaces_publishing(self):
        # Training took 10 minutes: not again within 40 minutes, even with many new pairs.
        self.assertFalse(publish_due(40_000, 20_000, 30 * 60, 600, 500, 1800))
        self.assertTrue(publish_due(40_000, 20_000, 41 * 60, 600, 500, 1800))

    def test_restart_on_a_large_library_does_not_retrain_at_once(self):
        from ols_platform.tuning.background import TRAIN_SECONDS_PER_PAIR

        known = 35_000
        took = TRAIN_SECONDS_PER_PAIR * (known + 600)
        self.assertFalse(publish_due(known + 600, known, 60, took, 500, 1800))
        self.assertTrue(publish_due(known + 600, known, 4 * took + 1, took, 500, 1800))

    def test_nothing_new_never_publishes(self):
        self.assertFalse(publish_due(100, 100, 10_000, 0, 500, 1800))


if __name__ == "__main__":
    unittest.main()
