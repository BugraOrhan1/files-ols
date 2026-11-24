"""Other donors as a second opinion on a transferred tune."""

import struct
import unittest
from unittest import mock

from ols_platform.tuning import transfer as tr

ORIGINAL = struct.pack("<8H", *[1000 + 40 * i for i in range(8)])
UP = struct.pack("<8H", *[1200 + 40 * i for i in range(8)])
DOWN = struct.pack("<8H", *[800 + 40 * i for i in range(8)])


def run(outputs, similarities=None):
    similarities = similarities or [0.99] * len(outputs)
    answers = [{"status": "ok", "output": o, "donor": {"original_sha256": f"d{i}", "similarity": s}}
               for i, (o, s) in enumerate(zip(outputs, similarities))] + [{"status": "failed"}] * 9
    mine = {"output": UP, "donor": {"original_sha256": "me", "similarity": 1.0}}
    with mock.patch.object(tr, "transfer", side_effect=answers) as called:
        result = tr.second_opinions(ORIGINAL, "stage1", None, mine)
    return result, called


class OpinionTests(unittest.TestCase):
    def test_agreeing_donors(self):
        result, called = run([UP, UP])
        self.assertEqual((result["opinions"], result["agreement"], result["contradicted"]), (2, 1.0, False))
        self.assertEqual(called.call_args_list[1].kwargs["exclude"], {"me", "d0"})

    def test_contradiction_needs_two_opinions(self):
        self.assertTrue(run([DOWN, DOWN])[0]["contradicted"])
        self.assertFalse(run([DOWN])[0]["contradicted"])

    def test_much_less_similar_donors_are_not_asked(self):
        self.assertEqual(run([DOWN, DOWN], [0.80, 0.80])[0]["opinions"], 0)

    def test_nothing_in_common_is_no_opinion(self):
        self.assertIsNone(run([ORIGINAL, ORIGINAL])[0]["agreement"])


if __name__ == "__main__":
    unittest.main()
