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

    def test_a_version_can_be_chosen_and_must_belong_to_the_original(self):
        version = known_versions(self.pair.original, self.package)[0]
        key = f"{version['profile']}@{version['pair_id']}"
        self.assertEqual(generate(self.pair.original, key, self.package, {}).output, self.pair.tuned)
        with self.assertRaises(GenerationRejected) as ctx:
            generate(self.pair.original, f"{version['profile']}@0000", self.package, {})
        self.assertEqual(ctx.exception.code, "version_not_found")


if __name__ == "__main__":
    unittest.main()
