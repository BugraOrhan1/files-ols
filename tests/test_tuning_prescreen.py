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

    def test_a_size_that_occurs_often_is_a_read_form(self):
        full = entry("zz", self.data + image(6, 0x8000))  # 0x48000: no whole 64 KB sectors
        self.assertEqual(self.rank([full]), [])  # on its own it may be a cut-off read
        others = [entry(f"{i:03d}", image(50 + i, 0x48000)) for i in range(tr.READ_FORM_COUNT - 1)]
        tr._RANK_CACHE.clear()
        self.assertEqual([e["original_sha256"] for e in self.rank(others + [full])], ["zz"])

    def test_a_cut_off_file_is_never_a_read_form(self):
        entries = [entry(f"{i:03d}", image(70 + i)) for i in range(6)] + [entry("zz", self.data)]
        cut = self.data[:-0x1000]
        self.assertEqual(tr.rank_donors(Package(entries), cut, "", "stage1"), [])

    def test_closest_values_stay_within_the_read_form(self):
        same = dict(entry("s", self.data), similarity=0.96)
        other = dict(entry("o", self.data + image(8, 0x10000)), similarity=0.962, cross_size=True)
        with mock.patch.object(tr, "choose_known_pair", return_value=None):
            chosen = tr._prefer_closest_values(self.data, "stage1", mock.Mock(), [same, other])
        self.assertEqual([d["original_sha256"] for d in chosen], ["s", "o"])


if __name__ == "__main__":
    unittest.main()
