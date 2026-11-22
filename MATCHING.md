# Explainable ECU matching

Branch `feature/ecu-evidence-matching`. Modules in `ols_platform/tuning/`:

| Module | Purpose |
| --- | --- |
| `ecu_fingerprint.py` | Identifiers from the bytes, with raw value, offset and rule (`pattern_unvalidated`); separator-only normalization; VAG revision comparison; 4 KiB block digests; streaming, resumable SQLite fingerprint index (`FingerprintIndex`) |
| `evidence.py` | Relations between files, revisions and references with evidence, source hash, algorithm version and a review history |
| `explain.py` | Ranked candidates with four separate values, plus a leave-one-out measurement |

## Phase 1 findings (where 95 % stayed 95 %)

* `generate._select_variant` needs `variant["size"] == len(data)` and literal token presence. `8P0907115A` vs
  `8P0907115B` counted as "missing", with no hint that only the revision index differs.
* The input's own SW/HW numbers were never read, so a refusal could not say which software the file has.
* `transfer._rank_donors` gives one number (`similarity`) and nothing else; there was no separate identity,
  compatibility or reference status, and the refusal named one variant without a next step.
* Commit `e22f6ee` already allows donors of another sector-aligned size. This branch explains the result.

## Statuses

* `similarity_score`: measured share (same-offset samples or anchor snippets). It is not a probability.
* `identity_status`: `IDENTICAL_CONTENT`, `IDENTITY_CONFIRMED`, `SAME_FAMILY_OTHER_REVISION`,
  `SAME_FAMILY_OTHER_IDS`, `INSUFFICIENT_EVIDENCE` or `CONFLICT`.
* `compatibility_status`: `EXACT_VARIANT_KNOWN_PAIR`, `TRANSFER_CHECK_IDENTICAL_BYTES`,
  `TRANSFER_CHECK_WITH_RECALCULATION`, `TRANSFER_CHECK_INCOMPLETE`, `ALREADY_TUNED`, `NOT_CHECKED` or `BLOCKED`.
* `reference_status`: `INDEPENDENT_EXPORT_MATCH`, `PROJECT_PAIR`, `VALIDATED_RELATION`,
  `USER_REVIEWED_NOT_INDEPENDENT`, `REJECTED_BY_REVIEW` or `NO_REFERENCE`.

Statuses rise only through reproducible checks:

* every token literally present;
* equal SHA-256;
* `--check`, where all donor edits land on identical original bytes, in memory only;
* an independent export hash;
* a relation in the `validated` state.

A hard conflict (another ECU family name) blocks the candidate whatever its similarity. A reviewer's
confirmation gives `user_confirmed`, never `validated`.

## Commands

```
OLSTuner.exe explain origineel.bin [--check] [--filter alternative_revision] [--json] [--propose]
OLSTuner.exe review <id> confirm|reject|conflict|reopen --reason "..." --reviewer naam
OLSTuner.exe relations [--file x.bin]
OLSTuner.exe fingerprint-index D:\Database4 [--index pad.sqlite]
OLSTuner.exe evaluate-matching [--limit N]
```

In the app, "Uitleg: welke referentie past?" shows the same report with filters.

## Measured results

* Synthetic test cases: 20 explain/evidence/index tests, covering identical files, another revision, missing
  identification, conflicts, partial matches, already tuned files, stale sources, resume and read-only behaviour.
* Real repository samples: 7 originals, each from its own ECU family. Leave-one-out leaves no peer from the same
  family, so precision and recall are **not measurable** on this set. All 7 were undecided and there were no false
  positives. `explain` on the real BMW and VW originals gives `IDENTICAL_CONTENT` with an independent export match.
  The real BMW Stage 1 export gives `ALREADY_TUNED` and `UNDECIDED`.
* Next step: run `evaluate-matching` on the full library (Database 1–4) for representative numbers.

## Limitations

* The extraction rules are generic patterns, not validated per ECU. Offsets from the OLS parser are not used as
  identity.
* There is no machine-learning ranker yet. Practice results (`practice_stats.json`) are the only learned signal
  and are shown as context; they never promote a status.
* The `fingerprint-index` command indexes and verifies files. Candidate search in the app still uses the model
  package, not this index.
