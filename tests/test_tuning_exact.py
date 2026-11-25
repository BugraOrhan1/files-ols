"""An identical original comes before every other recognition."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tuning_fixtures import make_pair  # noqa: E402

from ols_platform.tuning.exact import known_versions  # noqa: E402
from ols_platform.tuning.generate import GenerationRejected, available_profiles, generate  # noqa: E402
from ols_platform.tuning.package import build_package  # noqa: E402


class ExactTests(unittest.TestCase):
    def setUp(self):
        self.pair = make_pair(1)
        self.package = build_package([self.pair, make_pair(2, scale=0.95)])

    def test_exact_original_gives_its_own_tune(self):
        result = generate(self.pair.original, self.pair.profile, self.package, {})
        self.assertEqual(result.output, self.pair.tuned)
        self.assertEqual(result.evidence_level, "KNOWN_PAIR")
        self.assertIn(self.pair.profile, available_profiles(self.pair.original, self.package)[2])

    def test_one_changed_byte_is_not_the_same_original(self):
        data = bytearray(self.pair.original)
        data[-1] ^= 0xFF
        self.assertEqual(known_versions(bytes(data), self.package), [])

    def test_unnamed_project_takes_the_ecu_recognised_in_the_file(self):
        from unittest import mock

        from ols_platform.tuning import generate as g

        unnamed, named = {"family_key": "BOSCH 031471|6"}, {"family_key": "MG1CS002|6"}
        other = {"family_key": "EDC17C46|6"}
        for found, expected in (([named], named), ([unnamed, named], named), ([named, other], unnamed),
                                ([], unnamed), ([{"family_key": "UNKNOWN|6"}], unnamed)):
            with mock.patch.object(g, "_matching_families", return_value=found):
                self.assertIs(g._named_family(b"", None, unnamed), expected)
                self.assertIs(g._named_family(b"", None, other), other)  # a name is never replaced

    def test_which_labels_name_an_ecu(self):
        from ols_platform.tuning.package import is_named_label

        for label in ("MG1CS002", "EDC17_CP44", "SIMOS19.6", "BOSCH EDC17C46", "DQ250", "MVSX", "Siemens EMS3155"):
            self.assertTrue(is_named_label(label), label)
        for label in ("onbekend", "UNKNOWN", "NoData", "BOSCH 031471", "0005", "SIEMENS/CONTINENTAL 4RDQID 1234",
                      "Denso 89661-0F092", "", "--"):
            self.assertFalse(is_named_label(label), label)

    def test_a_version_can_be_chosen_and_must_belong_to_the_original(self):
        version = known_versions(self.pair.original, self.package)[0]
        key = f"{version['profile']}@{version['pair_id']}"
        self.assertEqual(generate(self.pair.original, key, self.package, {}).output, self.pair.tuned)
        with self.assertRaises(GenerationRejected) as ctx:
            generate(self.pair.original, f"{version['profile']}@0000", self.package, {})
        self.assertEqual(ctx.exception.code, "version_not_found")


if __name__ == "__main__":
    unittest.main()
