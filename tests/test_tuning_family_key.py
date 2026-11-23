import tempfile
import unittest
from pathlib import Path

from ols_platform.tuning.package import PackageStore, normalize_family_key, normalize_family_label


class FamilyKeyNormalizationTests(unittest.TestCase):
    def test_case_and_spacing_variants_share_one_family(self):
        self.assertEqual(normalize_family_label("mg1cs002"), "MG1CS002")
        self.assertEqual(normalize_family_label(" MG1CS002 "), "MG1CS002")
        self.assertEqual(normalize_family_label("Bosch  031479"), "BOSCH 031479")
        self.assertEqual(normalize_family_label(""), "onbekend")
        self.assertEqual(normalize_family_label("onbekend"), "onbekend")
        self.assertEqual(normalize_family_key("mg1cs002|2097152"), "MG1CS002|2097152")
        self.assertEqual(normalize_family_key("onbekend|8650768"), "onbekend|8650768")

    def test_contracts_recorded_with_old_spelling_still_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PackageStore(Path(tmp))
            store.contracts_path.write_text(
                '{"edc16u31|2097152": {"contract": "FLASH_TOOL_CORRECTS_ON_WRITE", "evidence": "x"}}',
                encoding="utf-8")
            self.assertIn("EDC16U31|2097152", store.contracts())
            store.set_contract("mg1cs002|6291456", "FLASH_TOOL_CORRECTS_ON_WRITE", "bevestigd in test")
            self.assertIn("MG1CS002|6291456", store.contracts())
            self.assertIn("EDC16U31|2097152", store.contracts())


class ProfileClassificationTests(unittest.TestCase):
    def test_underscored_and_spelled_out_catalyst_names_are_cat_off(self):
        from ols_platform.tuning.ols_versions import classify_profile
        self.assertEqual(classify_profile("stage_1 catalyst_off vmax_off"), "stage1+cat_off+vmax")
        self.assertEqual(classify_profile("Katalysator aus"), "cat_off")
        self.assertEqual(classify_profile("dpf_off"), "dpf_off")
        self.assertEqual(classify_profile("stage_2"), "stage2")
        self.assertEqual(classify_profile("Stage 1"), "stage1")


if __name__ == "__main__":
    unittest.main()
