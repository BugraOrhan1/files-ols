# Phase 12 — Parser validation and format reliability

## Status

**Local regression scope complete. Independent WinOLS validation remains
`NOT_TESTED`; Phase 11 remains in progress.**

Work was performed on the isolated `phase12-16-release-prep` branch from
Phase 11G commit `4d464d73a44ffa3005ac25cf78fbe7ab00b8b56b`. The original
`phase11e-audit` worktree and its pre-existing modifications were left
untouched.

## Existing implementation reviewed

- `PyOlsReader` is behind the `OlsProjectReader` protocol. It retains
  parser/version provenance and reports parser failures as structured
  snapshots instead of promoting partial parser state.
- Version observations are retained separately from canonical version rows.
  Exact duplicates are counted; conflicting records do not choose a slot
  name. Phase 11G's geometry guard rejects invalid records before slot
  enumeration.
- Slot offsets, file bounds, short reads, format observations, map/axis
  dimensions, skipped map records, and reference-validation status are
  reported without asserting that parser output is WinOLS ground truth.
- The canonical reference-package workflow already provides a template and
  checklist for source hashes, WinOLS project identity, export provenance,
  version/slot relations, representations, coverage, reviewer evidence, and
  independently reviewed fields. No parallel intake format was added.

## Validation performed

All automated tests used synthetic temporary inputs and temporary SQLite
databases, except for read-only access to the eight tracked OLS fixtures by
the real-parser integration tests.

| Check | Result |
|---|---|
| `python -m unittest tests.test_ols_sample_diagnostics tests.test_phase11e_parser_audit -v` | 24 passed, 0 failed |
| `PY_OLS_PATH=C:\Users\Bugra\Desktop\py-ols-study python -m unittest tests.test_ols_samples -v` | 2 passed, 0 failed; imported all eight samples into a temporary database |
| Parser source fingerprint | SHA-256 `02dacf610120f5eb4971aa0cd606efd29821b13a9e32db0165e6d5be49832771`, matching the previously reviewed adapter fingerprint |
| Python child exit code | `0` for both commands |
| PowerShell wrapper exit code | `0` for both commands |
| Tracked OLS/BIN fixture integrity | 14 fixtures checked after real-parser tests; 0 size or SHA-256 mismatches |

Coverage includes malformed headers; supported and unknown format
observations; invalid, conflicting, duplicate, inconsistent, oversized and
out-of-bounds version records; partial parser output; incomplete maps/axes;
unreasonable dimensions; deterministic diagnostics; parser disagreement
reporting; and safe temporary-database import. The existing regressions
exercised these cases without modifying parser or sample files.

## Findings and limits

No additional parser defect was reproduced in this phase. The Phase 11G
malformed version-geometry defect and its regression tests were present in the
verified base and passed. Avoiding a speculative parser change preserves
current behavior and keeps parser-derived interpretations explicitly
unvalidated.

The real-parser tests establish only repeatability of the adapter/database
observations for the current eight fixtures. Formats 503 and 597 are the only
observed sample formats. Slot boundaries, map meanings, map-to-version
relationships, extracted payload correctness, parser completeness, and
WinOLS export equivalence are not established by these tests.

No attributable independent WinOLS exports with verified project/version/map
relationships were available. Therefore **independent WinOLS validation is
`NOT_TESTED`**. Parser agreement, captured audit data, hashes, and synthetic
fixtures are not substitutes for that evidence.

No live database, hash queue, worker process, corpus root, or production
indexing operation was accessed. No source fixture was written or modified.
