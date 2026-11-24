"""Images that are an original in one project and a tune in another."""

import unittest

from ols_platform.tuning.library import contaminated_pairs


def row(pair, project, original, tuned):
    return {"pair_id": pair, "project_sha256": project, "original_sha256": original, "tuned_sha256": tuned}


class ContaminatedTests(unittest.TestCase):
    def test_clean_pairs_are_kept(self):
        self.assertEqual(contaminated_pairs([row("a", "P1", "o", "t1"), row("b", "P2", "o", "t2")]), set())

    def test_swapped_versions_exclude_both(self):
        self.assertEqual(contaminated_pairs([row("a", "P1", "x", "y"), row("b", "P2", "y", "x")]), {"a", "b"})

    def test_back_to_stock_is_no_tune(self):
        rows = [row("a", "P1", "stock", "t1"), row("b", "P2", "stock", "t2"), row("c", "P3", "read", "stock")]
        self.assertEqual(contaminated_pairs(rows), {"c"})

    def test_tuned_read_stored_as_original_is_excluded(self):
        rows = [row("a", "P1", "stock", "tune"), row("b", "P2", "stock", "tune"), row("c", "P3", "tune", "more")]
        self.assertEqual(contaminated_pairs(rows), {"c"})


if __name__ == "__main__":
    unittest.main()
