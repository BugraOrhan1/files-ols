"""Proposal from independent tunes of the same ECU, without a similar original."""

import json
import random
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ols_platform.tuning import consensus as c

X = [800, 1200, 1600, 2000, 2600, 3200, 4000, 5000]
Y = [10, 20, 40, 60, 80, 100]
CELLS = [400 + 7 * i for i in range(len(X) * len(Y))]


def words(v):
    return struct.pack(f">{len(v)}H", *v)


def image(seed, at, scale=1.0):
    rng = random.Random(seed)
    data = bytearray(rng.randrange(1, 256) for _ in range(0x4000))
    block = words([len(X), len(Y)]) + words(X) + words(Y)
    data[at:at + len(block)] = block
    start = at + len(block)
    cells = [round(v * scale) for v in CELLS]
    data[start:start + 2 * len(cells)] = words(cells)
    return data, start


def example(seed, ratio=1.2, scale=1.0):
    original, start = image(seed, 0x400 + 0x80 * seed, scale)
    tuned = bytearray(original)
    tuned[start:start + 2 * len(CELLS)] = words([round(round(v * scale) * ratio) for v in CELLS])
    records = c.structural_records(bytes(original), bytes(tuned))
    return {"original": f"o{seed}", "tuned": f"t{seed}", "profile": "stage1", "variant": f"v{seed}",
            "project": f"p{seed}", "records": records}


class ConsensusTests(unittest.TestCase):
    def test_table_is_recognised_by_its_structure(self):
        records = example(1)["records"]
        self.assertEqual([(r["columns"], r["rows"], r["endian"]) for r in records], [(8, 6, "big")])

    def test_neighbouring_tables_and_curves_are_all_found(self):
        original = bytearray(random.Random(9).randrange(1, 256) for _ in range(0x2000))
        table = words([len(X), len(Y)]) + words(X) + words(Y) + words(CELLS)
        curve = words([len(X)]) + struct.pack(">8h", *range(-40, 40, 10)) + words(range(500, 580, 10))
        block = table + table + curve
        original[0x400:0x400 + len(block)] = block
        tuned = bytearray(original)
        head = 2 * (2 + len(X) + len(Y))
        starts = [0x400 + head, 0x400 + len(table) + head, 0x400 + 2 * len(table) + 2 + 16]
        for start, count in zip(starts, (len(CELLS), len(CELLS), 8)):
            values = struct.unpack_from(f">{count}H", tuned, start)
            struct.pack_into(f">{count}H", tuned, start, *(v + 50 for v in values))
        records = c.structural_records(bytes(original), bytes(tuned))
        self.assertEqual(sorted(r["start"] for r in records), starts)

    def test_agreement_is_applied_at_the_new_place(self):
        data, start = image(50, 0x2344, 1.05)
        result = c.propose(bytes(data), [example(i) for i in range(1, 6)], "stage1")
        self.assertEqual([m["start"] for m in result["maps"]], [start])
        self.assertAlmostEqual(result["maps"][0]["median_change"], 0.2, delta=0.01)
        self.assertEqual(result["output"][:start], bytes(data[:start]))

    def test_too_few_or_disagreeing_examples_change_nothing(self):
        data, _ = image(50, 0x2344)
        few = [example(i) for i in range(1, 4)]
        self.assertEqual(c.propose(bytes(data), few, "stage1")["maps"], [])
        split = [example(i, 1.2 if i % 2 else 0.8) for i in range(1, 7)]
        self.assertEqual(c.propose(bytes(data), split, "stage1")["maps"], [])

    def test_several_tunes_of_one_original_are_one_vote(self):
        data, _ = image(50, 0x2344)
        self.assertEqual(c.propose(bytes(data), [example(1)] * 6, "stage1")["maps"], [])

    def test_already_tuned_table_is_left_alone(self):
        data, _ = image(50, 0x2344, 1.2)
        self.assertEqual(c.propose(bytes(data), [example(i) for i in range(1, 6)], "stage1")["maps"], [])

    def test_other_map_with_same_axes_is_left_alone(self):
        data, _ = image(50, 0x2344, 3.0)
        self.assertEqual(c.propose(bytes(data), [example(i) for i in range(1, 6)], "stage1")["maps"], [])

    def test_report_only_and_no_emission_or_unnamed_profiles(self):
        data, _ = image(50, 0x2344)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c._write_gz(c._index_path(root, "TESTECU17"), {"label": "TESTECU17",
                                                           "entries": [example(i) for i in range(1, 6)]})
            with self.assertRaises(ValueError):  # a single map is too little to rely on
                c.write_proposal(bytes(data), root, "stage1", root / "uit" / "z.x", label="testecu17")
            with mock.patch.object(c, "MIN_MAPS", 1):
                report = c.write_proposal(bytes(data), root, "stage1+vmax", root / "uit" / "x.x", label="testecu17")
            self.assertEqual(len(report["kaarten"]), 1)
            self.assertEqual(sorted(p.suffix for p in (root / "uit").iterdir()), [".csv", ".json"])
            self.assertIn("niet flashen", json.loads(Path(report["json"]).read_text(encoding="utf-8"))["soort"])
            self.assertEqual(c.detect_labels(root, bytes(data), "VW_Golf_Bosch_TestECU17_OBD_VR.bin"), ["TESTECU17"])
            self.assertEqual(c.detect_labels(root, bytes(data), "VW_Golf.bin"), [])
            for profile in ("dpf_off", "stage1+egr_off", "other"):
                with self.assertRaises(ValueError):
                    c.write_proposal(bytes(data), root, profile, root / "uit" / "y.x", label="testecu17")


if __name__ == "__main__":
    unittest.main()
