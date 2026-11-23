"""Map knowledge: a tuned map is found in other software through its axes."""

import random
import struct
import tempfile
import unittest
from pathlib import Path

from ols_platform.tuning.maps import load_records, locate, map_change, map_records, save_records
from ols_platform.tuning.transfer import place_by_maps

RPM = [800, 1000, 1250, 1500, 2000, 2500, 3000, 3500, 4000, 5000]
LOAD = [10, 20, 40, 60, 80, 100, 120, 140]


def _words(values):
    return struct.pack(f">{len(values)}H", *values)


def _image(seed: int, at: int, map_values, size: int = 0x8000) -> bytearray:
    rng = random.Random(seed)
    image = bytearray(rng.randrange(256) for _ in range(size))
    block = _words(RPM) + _words(LOAD) + _words(map_values)
    image[at:at + len(block)] = block
    return image


def _map_def(at: int) -> dict:
    data = at + 2 * (len(RPM) + len(LOAD))
    return {"description": "Torque limiter", "data_type": "UWORD", "columns": len(RPM), "rows": len(LOAD),
            "data_offset": data, "axes": [
                {"role": "x", "points": len(RPM), "address": at, "data_type": "UWORD"},
                {"role": "y", "points": len(LOAD), "address": at + 2 * len(RPM), "data_type": "UWORD"}]}


BASE = [300 + 5 * i for i in range(len(RPM) * len(LOAD))]


class MapKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.original = _image(1, 0x1000, BASE)
        self.tuned = bytearray(self.original)
        tuned_values = [round(v * 1.2) for v in BASE]
        start = 0x1000 + 2 * (len(RPM) + len(LOAD))
        self.tuned[start:start + 2 * len(BASE)] = _words(tuned_values)
        self.records = map_records([_map_def(0x1000)], bytes(self.original), bytes(self.tuned))

    def test_record_of_changed_map(self):
        self.assertEqual(len(self.records), 1)
        record = self.records[0]
        self.assertEqual(record["name"], "Torque limiter")
        self.assertEqual(len(record["axes"]), 2)

    def test_unchanged_map_has_no_record(self):
        self.assertEqual(map_records([_map_def(0x1000)], bytes(self.original), bytes(self.original)), [])

    def test_map_found_in_other_software_and_scaled(self):
        # Other software: map 0x2400 further, other surroundings, stock values 3 % higher.
        other_values = [round(v * 1.03) for v in BASE]
        other = _image(2, 0x3400, other_values)
        record = self.records[0]
        expected_start = 0x3400 + 2 * (len(RPM) + len(LOAD))
        self.assertEqual(locate(bytes(other), record, near=None), expected_start)
        missing = [{"start": record["start"], "length": len(record["orig"]) // 2}]
        output, placed, still = place_by_maps(bytes(other), bytearray(other), [], missing, self.records)
        self.assertEqual(still, [])
        self.assertEqual(placed[0]["how"], "kaart via assen")
        got = struct.unpack(f">{len(BASE)}H", output[expected_start:expected_start + 2 * len(BASE)])
        for value, stock in zip(got, other_values):
            self.assertAlmostEqual(value, stock * 1.2, delta=1.5)

    def test_already_tuned_map_is_reported_not_tuned_twice(self):
        tuned_other = _image(3, 0x3400, [round(v * 1.2) for v in BASE])
        record = self.records[0]
        missing = [{"start": record["start"], "length": len(record["orig"]) // 2}]
        output, placed, still = place_by_maps(bytes(tuned_other), bytearray(tuned_other), [], missing, self.records)
        self.assertEqual(placed, [])
        self.assertTrue(all(m.get("present") for m in still))
        self.assertEqual(bytes(output), bytes(tuned_other))

    def test_no_axes_no_placement(self):
        other = bytearray(random.Random(4).randrange(256) for _ in range(0x8000))
        record = self.records[0]
        missing = [{"start": record["start"], "length": len(record["orig"]) // 2}]
        _output, placed, still = place_by_maps(bytes(other), bytearray(other), [], missing, self.records)
        self.assertEqual(placed, [])
        self.assertEqual(len(still), 1)

    def test_rule_reads_change_in_right_byte_order(self):
        change = map_change(self.records[0])
        self.assertAlmostEqual(change["median"], 1.2, places=2)
        self.assertFalse(change["to_max"])

    def test_records_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_records(Path(tmp), "ab" * 32, "cd" * 32, self.records)
            self.assertEqual(load_records(Path(tmp), "ab" * 32, "cd" * 32), self.records)
            self.assertEqual(load_records(Path(tmp), "ab" * 32, "ef" * 32), [])


if __name__ == "__main__":
    unittest.main()
