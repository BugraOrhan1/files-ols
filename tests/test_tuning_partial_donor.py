"""Partial 'already tuned' evidence only counts from a same-software, same-size donor."""

import unittest
from unittest import mock

from ols_platform.tuning import transfer as tr


class _Package:
    def load_variant(self, variant_id):
        return {"variant_id": variant_id}


def _run(donors, present_for, how="zelfde plek"):
    """Run transfer() with donors; donors whose id is in present_for report a present 8-byte map."""

    calls = []

    def choose(blob, profile, sha):
        calls.append(blob["variant_id"])
        return {"edits": [(i * 16, "00" * 8) for i in range(4)], "tuned_version_name": f"tune {blob['variant_id']}", "project_name": "p"}

    def place_edits(data, edits):
        vid = calls[-1]
        if vid in present_for:
            return data, [], [{"start": 0, "length": 8, "present": True}]
        return data, [{"how": how}], []

    with mock.patch.object(tr, "rank_donors", return_value=donors), \
            mock.patch.object(tr, "_prefer_closest_values", side_effect=lambda d, p, pk, ds: ds), \
            mock.patch.object(tr, "choose_known_pair", side_effect=choose), \
            mock.patch.object(tr, "tuned_presence", return_value=(0.0, False)), \
            mock.patch.object(tr, "place_edits", side_effect=place_edits):
        return tr.transfer(bytes(16), "stage2", _Package())


def _donor(vid, similarity, cross_size=False):
    return {"variant_id": vid, "original_sha256": vid, "similarity": similarity, "cross_size": cross_size}


class PartialDonorTests(unittest.TestCase):
    def test_same_software_same_size_is_conclusive(self):
        result = _run([_donor("a", 1.0), _donor("b", 0.9)], present_for={"a"})
        self.assertEqual(result["code"], "partially_tuned")

    def test_other_read_form_skips_to_next_donor(self):
        result = _run([_donor("a", 1.0, cross_size=True), _donor("b", 0.9)], present_for={"a"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["donor"]["variant_id"], "b")

    def test_after_an_alarm_recalculated_maps_are_not_accepted(self):
        for how in ("omgerekend", "kaart via assen"):
            result = _run([_donor("a", 0.95), _donor("b", 0.9)], present_for={"a"}, how=how)
            self.assertEqual(result["code"], "no_reliable_donor")

    def test_number_donors_stay_candidates_behind_many_similar_ones(self):
        similar = [_donor(f"s{i}", 0.99 - i / 1000) for i in range(tr.MAX_DONORS_TRIED)]
        number = dict(_donor("n", 0.02), identity_confirmed="full", exact_required=True)
        result = _run([*similar, number], present_for={d["variant_id"] for d in similar})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["donor"]["variant_id"], "n")

    def test_only_weak_donors_report_no_reliable_donor(self):
        result = _run([_donor("a", 0.95), _donor("b", 1.0, cross_size=True)], present_for={"a", "b"})
        self.assertEqual(result["code"], "no_reliable_donor")


if __name__ == "__main__":
    unittest.main()
