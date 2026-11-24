"""Combinations joined from separate tunes."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ols_platform.tuning import generate as g  # noqa: E402
from ols_platform.tuning.compose import join, offered, parts_of  # noqa: E402

DATA = bytes(range(64))


def result(output, ready=True, ranges=((0, 64),)):
    return g.GenerationResult(output=output, family_key="X|64", variant_key="v", profile="p", package_id="k",
                              input_sha256="i", output_sha256="o", changes=[], allowed_ranges=list(ranges),
                              identity={"family": "X"}, evidence_level="KNOWN_PAIR",
                              integrity={"ready_for_use": ready, "contract": "x"}, notes=["n"])


def with_bytes(changes):
    out = bytearray(DATA)
    for at, value in changes.items():
        out[at] = value
    return bytes(out)


class ComposeTests(unittest.TestCase):
    def test_only_independent_performance_parts(self):
        self.assertEqual(parts_of("stage1+vmax"), ["stage1", "vmax"])
        for profile in ("stage1", "stage1+dpf_off", "stage1+egr_off+vmax", "stage1+stage2", "stage1+cat_off",
                        "stage1+dtc_off", "vmax+vmax"):
            self.assertEqual(parts_of(profile), [], profile)

    def test_join_keeps_both_and_reports_contradictions(self):
        a, b = with_bytes({4: 99}), with_bytes({20: 77})
        joined, conflicts = join(DATA, [a, b])
        self.assertEqual((joined, conflicts), (with_bytes({4: 99, 20: 77}), []))
        self.assertEqual(join(DATA, [a, a]), (a, []))
        self.assertEqual(join(DATA, [a, with_bytes({4: 98})])[1], [4])
        self.assertEqual(join(DATA, [a, with_bytes({5: 98})])[1], [4])  # other half of the same cell

    def test_combination_is_offered_only_without_an_example(self):
        self.assertEqual(offered(["stage1", "vmax", "stage1+vmax"]), [])
        self.assertIn("stage1+pop_bang+vmax", offered(["stage1", "vmax", "pop_bang"]))
        self.assertEqual(offered(["stage1", "dpf_off"]), [])

    def run_generate(self, answers, profile="stage1+vmax"):
        def single(data, name, package, contracts=None, **_):
            answer = answers[name]
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch.object(g, "_generate_single", side_effect=single):
            return g.generate(DATA, profile, None)

    def test_joined_when_no_example_exists(self):
        missing = g.GenerationRejected("no_similar_original", "geen")
        out = self.run_generate({"stage1+vmax": missing, "stage1": result(with_bytes({4: 9})),
                                 "vmax": result(with_bytes({20: 8}))})
        self.assertEqual(out.output, with_bytes({4: 9, 20: 8}))
        self.assertEqual(out.evidence_level, "COMBINED")

    def test_example_wins_and_conflicts_or_tuned_input_are_refused(self):
        direct = result(with_bytes({1: 1}))
        self.assertIs(self.run_generate({"stage1+vmax": direct}), direct)
        missing = g.GenerationRejected("no_similar_original", "geen")
        with self.assertRaises(g.GenerationRejected) as ctx:
            self.run_generate({"stage1+vmax": missing, "stage1": result(with_bytes({4: 9})),
                               "vmax": result(with_bytes({4: 8}))})
        self.assertEqual(ctx.exception.code, "combination_conflict")
        with self.assertRaises(g.GenerationRejected) as ctx:
            self.run_generate({"stage1+vmax": g.GenerationRejected("already_tuned", "al")})
        self.assertEqual(ctx.exception.code, "already_tuned")

    def test_export_stays_blocked_when_one_part_has_no_checksum_rule(self):
        missing = g.GenerationRejected("no_similar_original", "geen")
        out = self.run_generate({"stage1+vmax": missing, "stage1": result(with_bytes({4: 9})),
                                 "vmax": result(with_bytes({20: 8}), ready=False)})
        self.assertFalse(out.integrity["ready_for_use"])


if __name__ == "__main__":
    unittest.main()
