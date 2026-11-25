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
        chosen, result = tr.with_second_opinions(ORIGINAL, "stage1", None, mine)
    return dict(result, chosen=chosen), called


class TowardTests(unittest.TestCase):
    """Values already on the tuned side mean the file is probably tuned."""

    def count(self, data):
        edits = [[0, ORIGINAL.hex(), UP.hex()]]
        placed = [{"how": "omgerekend", "start": 0, "end": 16, "donor_start": 0}]
        return tr.moved_toward_tune(data, edits, placed)

    def test_values_on_the_tuned_side(self):
        other_tuner = struct.pack("<8H", *[1100 + 40 * i for i in range(8)])
        self.assertEqual(self.count(other_tuner), (8, 8))

    def test_stock_values_differ_both_ways_or_the_other_way(self):
        mixed = struct.pack("<8H", *[1000 + 40 * i + (30 if i % 2 else -30) for i in range(8)])
        cells, toward = self.count(mixed)
        self.assertLess(toward, tr.TOWARD_SHARE * cells)
        self.assertEqual(self.count(ORIGINAL), (0, 0))

    def test_a_small_shift_is_a_stock_variant(self):
        slightly = struct.pack("<8H", *[1030 + 40 * i for i in range(8)])
        self.assertEqual(self.count(slightly), (8, 0))

    def test_one_tuned_map_among_stock_ones(self):
        tuned_map = struct.pack("<8H", *[1100 + 40 * i for i in range(8)])
        stock_map = struct.pack("<8H", *[900 + 40 * i for i in range(8)])
        edits = [[16 * k, ORIGINAL.hex(), UP.hex()] for k in range(4)]
        placed = [{"how": "omgerekend", "start": 16 * k, "end": 16 * k + 16, "donor_start": 16 * k} for k in range(4)]
        data = tuned_map + stock_map * 3
        cells, toward = tr.moved_toward_tune(data, edits, placed)
        self.assertLess(toward, tr.TOWARD_SHARE * cells)  # lost in the total ...
        self.assertEqual(tr.one_map_moved(data, edits, placed), (8, 8))  # ... not on its own
        self.assertEqual(tr.one_map_moved(stock_map * 4, edits, placed), (0, 0))

    def test_identical_bytes_are_not_counted(self):
        placed = [{"how": "zelfde plek", "start": 0, "end": 16, "donor_start": 0}]
        self.assertEqual(tr.moved_toward_tune(UP, [[0, ORIGINAL.hex(), UP.hex()]], placed), (0, 0))


class OpinionTests(unittest.TestCase):
    def test_agreeing_donors(self):
        result, called = run([UP, UP])
        self.assertEqual((result["opinions"], result["agreement"], result["contradicted"]), (2, 1.0, False))
        self.assertEqual(called.call_args_list[1].kwargs["skip"], {"me", "d0"})

    def test_contradiction_needs_two_opinions(self):
        self.assertTrue(run([UP, DOWN, DOWN, DOWN])[0]["contradicted"])
        self.assertFalse(run([DOWN])[0]["contradicted"])

    def test_the_tune_the_others_agree_with_is_used(self):
        result, _ = run([DOWN, DOWN])  # mine is the odd one out
        self.assertEqual(result["chosen"]["output"], DOWN)
        self.assertTrue(result["replaced_first"])
        result, _ = run([UP, DOWN])
        self.assertEqual(result["chosen"]["donor"]["original_sha256"], "me")
        result, _ = run([DOWN])  # two tunes: no majority, the most similar one stays
        self.assertEqual(result["chosen"]["donor"]["original_sha256"], "me")

    def test_much_less_similar_donors_are_not_asked(self):
        self.assertEqual(run([DOWN, DOWN], [0.80, 0.80])[0]["opinions"], 0)

    def test_nothing_in_common_is_no_opinion(self):
        self.assertIsNone(run([ORIGINAL, ORIGINAL])[0]["agreement"])


if __name__ == "__main__":
    unittest.main()
