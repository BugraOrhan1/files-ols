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


class SpellingTests(unittest.TestCase):
    def test_spellings_of_one_ecu_are_joined(self):
        from ols_platform.tuning.package import normalize_family_key as key

        for name in ("EDC17_CP44", "EDC 17CP44", "edc17cp44", "EDC17 CP44", "EDC17-CP44"):
            self.assertEqual(key(f"{name}|4194304"), "EDC17CP44|4194304")
        self.assertEqual(key("MED17;5|1"), "MED17.5|1")
        self.assertEqual(key("SID 209|1"), "SID209|1")
        self.assertEqual(key("MDG1_MD1CP002|1"), key("MD1CP002|1"))

    def test_different_ecus_stay_apart(self):
        from ols_platform.tuning.package import normalize_family_key as key

        self.assertNotEqual(key("EDC15P|1"), key("EDC15P+|1"))
        self.assertNotEqual(key("MED17.1.1|1"), key("MED17.11|1"))
        self.assertEqual(key("Bosch 0281010082|1"), "BOSCH 0281010082|1")
        self.assertEqual(key("Siemens/Continental PO300|1"), "SIEMENS/CONTINENTAL PO300|1")
        self.assertEqual(key("onbekend|1"), "onbekend|1")


class VersionNameTests(unittest.TestCase):
    def check(self, names, expected):
        from ols_platform.tuning.ols_versions import classify_profile

        for name in names:
            self.assertEqual(classify_profile(name), expected, name)

    def test_hidden_emission_spellings_are_not_learned(self):
        self.check(("Stage1 + dpfoff", "Stage1 + Lamda_Off", "stage2 - egrdpf", "Stage1_+_V-Max_+_Agroff",
                    "Stage1 NO Catalysator", "stage_1 p0420", "stage1 vmax noegr_nodpf", "stage2 racecat",
                    "dpfoff", "noegr", "Lamda Remove"), "other")

    def test_recognised_spellings_keep_their_profile(self):
        self.check(("stage1 dpf_off",), "stage1+dpf_off")
        self.check(("EGR off",), "egr_off")

    def test_ordinary_names_stay_ordinary(self):
        self.check(("Stage1 Drehmoment begrenzer raus", "stage 1 350 degrees", "Stage_1 see description",
                    "stage 1 TTE500", "stage_1 350hp P350", "Stage 1 catch can"), "stage1")
        self.check(("Vmax 250Km/h begrenzung",), "vmax")

    def test_trial_versions_are_not_learned(self):
        self.check(("Stage 2 TEST", "stage1 test2", "stage_1 not tested yet", "Stage1 Testing", "Stage 1 voor chinees , maar is nog niet volledig",
                    "stage1 nicht fertig", "Stage 2 unfinished"), "other")
        self.check(("Stage 1 tested ok", "stage1 fastest"), "stage1")

    def test_versions_marked_bad_are_never_learned(self):
        self.check(("stage_1 bad_file see description", "Stage_1 gives errors", "stage 1 goes into limp mode",
                    "stage_1 pop_and_bang jerking pops Bad_File", "Stage 1 läuft nicht"), "other")



if __name__ == "__main__":
    unittest.main()
