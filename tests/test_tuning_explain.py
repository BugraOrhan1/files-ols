"""Explainable matching, ECU identity, evidence graph and streaming index."""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tuning_fixtures import ID_OFFSETS, Variant, make_original, make_pair  # noqa: E402

from ols_platform.tuning.ecu_fingerprint import (  # noqa: E402
    FingerprintIndex, block_digests, compare_blocks, compare_ids, extract_identifiers, normalize_id, stream_file,
)
from ols_platform.tuning.evidence import EvidenceGraph  # noqa: E402
from ols_platform.tuning.explain import evaluate, explain, format_report  # noqa: E402
from ols_platform.tuning.library import save_pair  # noqa: E402
from ols_platform.tuning.package import PackageStore, build_package  # noqa: E402

VAG = Variant(ecu="EDC17C46", hw="0281017946", sw="03L906018JL")


def replace_at(data: bytes, offset: int, text: bytes) -> bytes:
    out = bytearray(data)
    out[offset:offset + len(text)] = text
    return bytes(out)


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


class NormalizationTests(unittest.TestCase):
    def test_separators_only_and_revisions_stay_visible(self):
        self.assertEqual(normalize_id("03L 906 018 JL"), "03L906018JL")
        self.assertEqual(normalize_id("03l-906.018_jl"), "03L906018JL")
        self.assertEqual(compare_ids("03L906018JL", "03L 906 018 JL"), "exact")
        self.assertEqual(compare_ids("03L906018JL", "03L906018JM"), "same_base_other_revision")
        self.assertEqual(compare_ids("03L906018J", "03L906018JL"), "same_base_other_revision")
        self.assertEqual(compare_ids("03L906018JL", "03L906022JL"), "different")
        self.assertEqual(compare_ids("1037512345", "1037512346"), "different")

    def test_extraction_keeps_raw_value_offset_and_rule(self):
        data = replace_at(make_original(1, VAG), 0x300, b"03L 906 018 JL\x00MED17.5.2\x00")
        ids = {(i.rule, i.raw): i for i in extract_identifiers(data)}
        part = ids[("vag_part_number", "03L 906 018 JL")]
        self.assertEqual(part.offset, 0x300)
        self.assertEqual(part.normalized, "03L906018JL")
        self.assertEqual(part.status, "pattern_unvalidated")
        self.assertEqual(ids[("bosch_hw_0281", "0281017946")].offset, ID_OFFSETS["hw_number"])
        self.assertIn(("ecu_family_name", "EDC17C46"), ids)
        self.assertIn(("ecu_family_name", "MED17.5.2"), ids)


class ExplainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.pairs = [make_pair(n, VAG) for n in (1, 2, 3)]
        for pair in cls.pairs:
            save_pair(cls.root, pair)
        cls.package = build_package(cls.pairs)
        cls.original = cls.pairs[0].original

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_explain(self, data, **kw):
        return explain(data, self.package, store_root=self.root, **kw)

    def test_identical_binary(self):
        report = self.run_explain(self.original)
        best = report["candidates"][0]
        self.assertEqual(best["identity_status"], "IDENTICAL_CONTENT")
        self.assertEqual(best["compatibility_status"], "EXACT_VARIANT_KNOWN_PAIR")
        self.assertEqual(best["reference_status"], "PROJECT_PAIR")
        self.assertTrue(best["binary"]["full_binary_match"])
        self.assertEqual(report["verdict"]["status"], "DECIDED")
        self.assertFalse(best["provenance"]["parser_offsets_used_as_identity"])

    def test_same_hardware_other_software_revision_is_explained_not_promoted(self):
        data = replace_at(self.original, ID_OFFSETS["sw_number"], b"03L906018JM")
        report = self.run_explain(data)
        best = report["candidates"][0]
        self.assertGreater(best["similarity_score"], 0.95)
        self.assertEqual(best["identity_status"], "SAME_FAMILY_OTHER_REVISION")
        self.assertEqual(best["category"], "alternative_revision")
        self.assertEqual(best["revision_differences"][0]["reference"], "03L906018JL")
        self.assertEqual(best["revision_differences"][0]["input"], "03L906018JM")
        self.assertEqual(best["compatibility_status"], "NOT_CHECKED")
        self.assertEqual(report["verdict"]["status"], "UNDECIDED")
        self.assertIn("03L906018JM", best["next_step"])
        # the targeted check gathers evidence: every donor edit lands on identical original bytes
        checked = self.run_explain(data, check=True)["candidates"][0]
        self.assertEqual(checked["compatibility_status"], "TRANSFER_CHECK_IDENTICAL_BYTES")
        self.assertEqual(checked["identity_status"], "SAME_FAMILY_OTHER_REVISION")  # identity is not promoted
        self.assertIn("revisie", format_report(self.run_explain(data)))

    def test_already_tuned_file_is_never_decided_as_exact_variant(self):
        report = self.run_explain(self.pairs[0].tuned)
        best = report["candidates"][0]
        self.assertEqual(best["identity_status"], "IDENTITY_CONFIRMED")
        self.assertEqual(best["compatibility_status"], "ALREADY_TUNED")
        self.assertEqual(report["verdict"]["status"], "UNDECIDED")
        self.assertIn("originele", best["next_step"])

    def test_missing_software_identification(self):
        data = replace_at(self.original, ID_OFFSETS["sw_number"], b"\x00" * 11)
        best = self.run_explain(data)["candidates"][0]
        self.assertEqual([m["reference"] for m in best["missing_fields"]], ["03L906018JL"])
        self.assertEqual(best["identity_status"], "SAME_FAMILY_OTHER_IDS")

    def test_other_bosch_software_number_is_a_conflicting_field(self):
        variant = Variant(ecu="EDC17C46", hw="0281017946", sw="1037512345")
        pair = make_pair(4, variant)
        package = build_package([pair])
        data = replace_at(pair.original, ID_OFFSETS["sw_number"], b"1037599999")
        best = explain(data, package)["candidates"][0]
        self.assertEqual(best["conflicting_fields"][0]["field"], "sw_number")
        self.assertFalse(best["conflicting_fields"][0]["hard"])
        self.assertEqual(best["identity_status"], "SAME_FAMILY_OTHER_IDS")

    def test_conflicting_ecu_identity_blocks_despite_high_similarity(self):
        data = replace_at(self.original, ID_OFFSETS["ecu_type"], b"MED17.5.2")
        report = self.run_explain(data, check=True)
        best = report["candidates"][0]
        self.assertGreater(best["similarity_score"], 0.95)
        self.assertEqual(best["identity_status"], "CONFLICT")
        self.assertEqual(best["compatibility_status"], "BLOCKED")
        self.assertEqual(report["verdict"]["status"], "BLOCKED")

    def test_partial_binary_match_is_never_a_full_match(self):
        data = bytearray(self.original)
        data[0x4000:0x4100] = bytes(256)
        best = self.run_explain(bytes(data))["candidates"][0]
        binary = best["binary"]
        self.assertFalse(binary["full_binary_match"])
        self.assertGreaterEqual(binary["different"], 1)
        self.assertLess(binary["proven_identical_share"], 1.0)
        self.assertEqual(binary["different_ranges"][0][0], 0x4000)

    def test_unknown_input_has_no_decided_candidate(self):
        import random

        rng = random.Random(9)
        noise = bytes(rng.randrange(256) for _ in range(len(self.original)))
        report = self.run_explain(noise[:4096] + bytes(len(self.original) - 4096))
        self.assertNotEqual(report["verdict"]["status"], "DECIDED")

    def test_duplicate_references_are_listed_once_per_original(self):
        package = build_package(self.pairs + [self.pairs[0]])
        report = explain(self.original, package, top=10)
        shas = [c["reference"]["original_sha256"] for c in report["candidates"]]
        self.assertEqual(len(shas), len(set(shas)))

    def test_matching_is_read_only(self):
        data = replace_at(self.original, ID_OFFSETS["sw_number"], b"03L906018JM")
        before = snapshot(self.root)
        graph = EvidenceGraph(self.root / "graph")
        self.run_explain(data, check=True, graph=graph)
        graph.close()
        after = {k: v for k, v in snapshot(self.root).items() if "graph" not in k}
        self.assertEqual(before, after)

    def test_evaluation_reports_counts_and_rates(self):
        result = evaluate(self.package, self.root)
        self.assertEqual(result["tested"], 3)
        self.assertEqual(result["false_positive"], 0)
        self.assertEqual(result["precision"], 1.0)
        self.assertIn("undecided_share", result)

    def test_cli_explain_and_review_flow(self):
        from ols_platform.tuning.__main__ import main

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            store = PackageStore(store_root)
            store.install(self.package)
            for pair in self.pairs:
                save_pair(store_root, pair)
            original = Path(tmp) / "invoer.bin"
            original.write_bytes(replace_at(self.original, ID_OFFSETS["sw_number"], b"03L906018JM"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--store", str(store_root), "explain", str(original), "--json", "--check", "--propose"])
            self.assertEqual(code, 0)
            report = json.loads(out.getvalue())
            relation_id = report["proposed_relation"]
            graph = EvidenceGraph(store_root)
            relation = graph.get(relation_id)
            self.assertEqual(relation["kind"], "related_revision")
            # the dry-run check is reproducible independent evidence on this exact hash
            self.assertEqual(relation["status"], "validated")
            graph.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["--store", str(store_root), "review", str(relation_id), "reject",
                      "--reason", "andere motorcode", "--reviewer", "tuner"])
            self.assertEqual(json.loads(out.getvalue())["relation"]["status"], "rejected")
            with contextlib.redirect_stdout(io.StringIO()):
                main(["--store", str(store_root), "explain", str(original), "--json"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["--store", str(store_root), "explain", str(original), "--json"])
            best = json.loads(out.getvalue())["candidates"][0]
            self.assertEqual(best["reference_status"], "REJECTED_BY_REVIEW")
            self.assertEqual(best["compatibility_status"], "BLOCKED")


class EvidenceGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graph = EvidenceGraph(Path(self.tmp.name))

    def tearDown(self):
        self.graph.close()
        self.tmp.cleanup()

    def propose(self, sha="a" * 64):
        return self.graph.propose("related_revision", f"file:{sha}", "file:" + "b" * 64,
                                  evidence=[{"kind": "explain_report", "source_sha256": sha}],
                                  source_sha256=sha, algorithm="test/1", original_values={"sw": "03L906018JM"})

    def test_review_is_not_independent_evidence(self):
        rid = self.propose()
        self.assertEqual(self.graph.get(rid)["status"], "proposed")
        self.assertEqual(self.graph.review(rid, "confirm", "lijkt goed", "tuner")["status"], "user_confirmed")
        self.assertEqual(self.graph.reusable("file:" + "a" * 64, "file:" + "b" * 64), [])
        with self.assertRaises(ValueError):
            self.graph.review(rid, "confirm", "", "tuner")

    def test_only_independent_evidence_on_the_same_hash_validates(self):
        rid = self.propose()
        row = self.graph.add_evidence(rid, {"kind": "practice_reproduced_real_tune", "source_sha256": "c" * 64})
        self.assertEqual(row["status"], "proposed")  # measured on another file
        row = self.graph.add_evidence(rid, {"kind": "practice_reproduced_real_tune", "source_sha256": "a" * 64})
        self.assertEqual(row["status"], "validated")
        self.assertEqual(len(self.graph.reusable("file:" + "b" * 64, "file:" + "a" * 64)), 1)
        self.assertEqual([h["new_status"] for h in self.graph.history(rid)], ["validated"])

    def test_changed_source_makes_relation_stale_and_history_is_kept(self):
        rid = self.propose()
        row = self.graph.review(rid, "confirm", "ok", "tuner", current_source_sha256="d" * 64)
        self.assertEqual(row["status"], "stale")
        rid2 = self.propose()
        self.assertEqual(self.graph.mark_stale("file:" + "a" * 64, "e" * 64), 1)
        self.assertEqual(self.graph.get(rid2)["status"], "stale")
        self.assertEqual(len(self.graph.relations()), 2)  # nothing deleted
        self.assertEqual(self.graph.get(rid)["original_values"], {"sw": "03L906018JM"})


class StreamingIndexTests(unittest.TestCase):
    def test_stream_matches_in_memory_and_finds_ids_on_chunk_border(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.bin"
            data = bytearray(os.urandom(3 * 1024 * 1024))
            border = 1024 * 1024 - 5
            data[border - 1:border + 11] = b"\x001037512345\x00"
            path.write_bytes(bytes(data))
            sha, size, digests, ids = stream_file(path)
            self.assertEqual(sha, hashlib.sha256(data).hexdigest())
            self.assertEqual(size, len(data))
            self.assertEqual(digests, block_digests(bytes(data)))
            hit = [i for i in ids if i.raw == "1037512345"]
            self.assertEqual(hit[0].offset, border)

    def test_index_resumes_skips_unchanged_and_detects_stale_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "db"
            folder.mkdir()
            for n in range(3):
                (folder / f"f{n}.bin").write_bytes(make_original(n, VAG))
            index = FingerprintIndex(Path(tmp) / "fp.sqlite")
            calls = {"n": 0}

            def stop_after_one():
                calls["n"] += 1
                return calls["n"] > 1

            first = index.index_tree(folder, stop=stop_after_one, checkpoint_every=1)
            self.assertEqual(first["indexed"], 1)
            index.close()
            index = FingerprintIndex(Path(tmp) / "fp.sqlite")  # resume after "interruption"
            second = index.index_tree(folder)
            self.assertEqual((second["indexed"], second["skipped_unchanged"]), (2, 1))
            target = folder / "f0.bin"
            self.assertEqual(index.verify(target)["status"], "current")
            target.write_bytes(make_original(9, VAG))
            self.assertEqual(index.verify(target)["status"], "stale")
            self.assertEqual(index.index_tree(folder)["indexed"], 1)
            digests = block_digests(make_original(1, VAG))
            best = index.candidates_by_blocks(digests)[0][0]
            self.assertEqual(best, hashlib.sha256(make_original(1, VAG)).hexdigest())
            index.close()

    def test_block_comparison_reports_moved_and_missing(self):
        a = make_original(1, VAG)
        b = a[4096:] + bytes(4096)
        result = compare_blocks(block_digests(a), block_digests(b[:len(b) // 2]))
        self.assertGreater(result["identical_moved"], 0)
        self.assertGreater(result["missing_in_reference"], 0)
        self.assertFalse(result["full_binary_match"])


if __name__ == "__main__":
    unittest.main()
