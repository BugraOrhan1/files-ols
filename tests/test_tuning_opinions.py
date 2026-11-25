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

    def test_rows_of_one_map_are_judged_together(self):
        edits = [[40 * k, ORIGINAL[:6].hex(), UP[:6].hex()] for k in range(3)]
        tuned_rows = b"".join(struct.pack("<3H", 1100, 1140, 1180) + bytes(34) for _ in range(3))
        placed = [{"how": "omgerekend", "start": 40 * k, "end": 40 * k + 6, "donor_start": 40 * k} for k in range(3)]
        self.assertEqual(tr.one_map_moved(tuned_rows * 2, edits + [[120 + e[0], *e[1:]] for e in edits], placed + [
            dict(p, start=120 + p["start"], end=120 + p["end"], donor_start=120 + p["donor_start"]) for p in placed])[0], 18)
        far = [[400 * k, *e[1:]] for k, e in enumerate(edits)]
        apart = [dict(p, start=400 * k, end=400 * k + 6, donor_start=400 * k) for k, p in enumerate(placed)]
        data = b"".join(struct.pack("<3H", 1100, 1140, 1180) + bytes(394) for _ in range(3))
        self.assertEqual(tr.one_map_moved(data, far, apart), (0, 0))

    def test_a_whole_map_a_little_higher_is_a_hint(self):
        edits = [[16 * k, ORIGINAL.hex(), UP.hex()] for k in range(4)]
        placed = [{"how": "omgerekend", "start": 16 * k, "end": 16 * k + 16, "donor_start": 16 * k} for k in range(4)]
        higher = struct.pack("<8H", *[1030 + 40 * i for i in range(8)]) * 4
        mixed = struct.pack("<8H", *[1000 + 40 * i + (30 if i % 2 else -30) for i in range(8)]) * 4
        self.assertTrue(tr.hint_of_tune(higher, edits, placed))
        self.assertFalse(tr.hint_of_tune(mixed, edits, placed))
        self.assertFalse(tr.hint_of_tune(ORIGINAL * 4, edits, placed))

    def test_transfer_refuses_on_one_row_below_the_group_size(self):
        """8 to 11 cells in one row are enough; only a group of rows needs ONE_MAP_CELLS."""
        self.assertLess(tr.TOWARD_CELLS, tr.ONE_MAP_CELLS)
        stock = struct.pack("<8H", *[900 + 40 * i for i in range(8)])
        data = struct.pack("<8H", *[1100 + 40 * i for i in range(8)]) + stock * 3
        pair = {"edits": [[16 * k, ORIGINAL.hex(), UP.hex()] for k in range(4)], "tuned_version_name": "Stage 1"}
        placed = [{"how": "omgerekend", "start": 16 * k, "end": 16 * k + 16, "donor_start": 16 * k} for k in range(4)]
        donor = {"original_sha256": "d", "variant_id": "v", "similarity": 0.99}
        with mock.patch.object(tr, "rank_donors", return_value=[donor]),                 mock.patch.object(tr, "choose_known_pair", return_value=pair),                 mock.patch.object(tr, "tuned_presence", return_value=(0.0, 0)),                 mock.patch.object(tr, "place_edits", return_value=(bytearray(data), placed, [])):
            package = mock.Mock(path=None)
            outcome = tr.transfer(data, "stage1", package)
        self.assertEqual(outcome.get("code"), "possibly_tuned")

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
