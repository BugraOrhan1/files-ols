import struct
import unittest

from ols_platform.tuning.practice import placement_score


def image(values):
    return b"".join(struct.pack("<H", v) for v in values)


class PlacementScoreTests(unittest.TestCase):
    def test_other_tuner_same_maps_same_direction_counts_as_placed(self):
        original = image([100] * 8)
        real = image([120, 120, 100, 100, 100, 100, 100, 100])     # tuner A raises cells 0-1
        ours = image([110, 115, 100, 100, 100, 100, 100, 100])     # tuner B: other values, same map
        s = placement_score(original, ours, real)
        self.assertEqual(s["placement_precision"], 1.0)
        self.assertEqual(s["direction_agreement"], 1.0)

    def test_misplaced_map_is_not_placed(self):
        original = image([100] * 8)
        real = image([120, 120, 100, 100, 100, 100, 100, 100])
        ours = image([100, 100, 100, 100, 90, 90, 100, 100])       # wrote somewhere else
        s = placement_score(original, ours, real)
        self.assertEqual(s["placement_precision"], 0.0)
        self.assertEqual(s["direction_agreement"], 0.0)

    def test_opposite_direction_is_detected(self):
        original = image([100] * 4)
        real = image([120, 120, 100, 100])
        ours = image([80, 80, 100, 100])
        self.assertEqual(placement_score(original, ours, real)["direction_agreement"], 0.0)


if __name__ == "__main__":
    unittest.main()
