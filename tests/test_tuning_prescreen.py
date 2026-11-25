"""The right donor is found however many others the library holds."""
import base64
import random
import unittest
from unittest import mock

from ols_platform.tuning import transfer as tr
from ols_platform.tuning.package import anchors, fingerprint


def image(seed, size=0x40000):
    return random.Random(seed).randbytes(size)


def entry(name, data):
    return {"original_sha256": name, "variant_id": name, "size": len(data), "profiles": ["stage1"], "family_key": "F",
            "fp": base64.b64encode(fingerprint(data)).decode(), "anchors": base64.b64encode(anchors(data)).decode()}


class Package:
    package_id = "prescreen"

    def __init__(self, entries):
        self._entries = entries

    def fingerprints(self):
        return self._entries

    def variants(self):
        return []


class PrescreenTests(unittest.TestCase):
    def setUp(self):
        tr._RANK_CACHE.clear()
        self.data = image(1)

    def rank(self, entries):
        with mock.patch.object(tr, "CROSS_SIZE_CHECKS", 2), mock.patch.object(tr, "MAX_SHIFT_CHECKS", 2):
            return tr.rank_donors(Package(entries), self.data, "", "stage1")

    def test_share_per_donor(self):
        pkg = Package([entry("a", self.data), entry("b", image(2))])
        self.assertEqual(tr.prescreen(pkg, self.data), [1.0, 0.0])
        self.assertEqual(tr.prescreen(pkg, b"abc" + self.data)[0], 1.0)  # any alignment

    def test_other_size_behind_many_unrelated_ones(self):
        others = [entry(f"{i:03d}", image(10 + i, 0x50000)) for i in range(12)]
        ranked = self.rank(others + [entry("zz", image(3, 0x10000) + self.data)])
        self.assertEqual([e["original_sha256"] for e in ranked], ["zz"])

    def test_same_size_moved(self):
        moved = image(4, 0x2000) + self.data[:-0x2000]
        ranked = self.rank([entry(f"{i:03d}", image(30 + i)) for i in range(12)] + [entry("zz", moved)])
        self.assertEqual([e["original_sha256"] for e in ranked], ["zz"])

    def test_same_read_form_comes_first(self):
        other = entry("aa", self.data + image(5, 0x10000))
        ranked = self.rank([other, entry("zz", self.data)])
        self.assertEqual([e["original_sha256"] for e in ranked], ["zz", "aa"])


if __name__ == "__main__":
    unittest.main()
