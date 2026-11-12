# Phase 11F — Deep OLS structure and version relationship testing

## Status

**Tested scope complete on `phase11f-deep-validation`; Phase 11 remains in
progress. Independent WinOLS validation is `NOT_TESTED`.**

This work started from `77f68e338f9c8aaaa1a51cba4db8e206abd76f66`, the verified
remote `phase11e-audit` commit. The branch and clean worktree were isolated
from the original worktree, whose pre-existing local changes were not touched.
No live index, queue, source OLS/BIN/ORI file, or full corpus was accessed.

Labels in this report distinguish **observed** parser output, **measured**
byte properties, and **unresolved** interpretations. Synthetic fixtures
exercise invariants only; they are not evidence of WinOLS correctness.

## Changes and findings

### Version record normalization

The adapter previously returned multiple named version records for one slot
unchanged. The database enforces one `project_versions` row per project/slot,
so even exact duplicate parser records could fail an import. When conflicting
records named one slot, slot enumeration reported a conflict but retained the
first record's name.

The adapter now:

- collapses exact duplicate records for storage and reports their count;
- omits a slot's canonical version row if records for that slot disagree;
- leaves such a physically enumerable slot unnamed with
  `name_status="conflicting_records"`;
- retains all observed version records, including duplicates and conflicts,
  under `data_structure.version_record_observations`;
- records raw, duplicate, conflicting, and unique-unambiguous counts
  separately.

The database schema and existing diagnostic/report schema versions are
unchanged. The added `raw_counts` and `data_structure` fields are additive.
Ordinary single-record slots are unchanged. The importer continues to mark
imports with diagnostic issues as partial rather than representing them as
fully verified.

### Regression coverage

Synthetic temporary inputs now test:

- valid slot extents, out-of-file slot bounds, and inconsistent fixed-stride
  layouts;
- exact duplicate and contradictory records, including successful temporary
  database import without choosing a conflicting label;
- partial parser state followed by an exception: the adapter reports the
  parser failure and does not promote partial maps or versions;
- format values `503`, `597`, and unknown `9999`; the parser adapter exposes
  only its parser-reported format field, so agreement with a separate
  independently declared on-disk format field cannot be tested;
- deterministic repeated reports, stable source size/SHA-256, partial map
  record counts, incomplete structures, and extremely large dimensions;
- empty/equal and empty/non-empty binaries, changed ranges crossing chunk
  boundaries, chunk-size-invariant results, and read requests bounded by the
  configured chunk size;
- metadata-only and parser-slot candidate evidence remaining distinct from
  exact references and ECU identity confirmation.

Existing tests continue to cover conflicting/missing metadata, multiple
ambiguous slots, matching and stale hashes, and parser-derived payload
similarity without independent confirmation. No original/tuned relationship,
map semantics, or ECU compatibility is inferred from filenames, metadata,
similarity, or parser agreement.

## Real sample run

The existing eight OLS samples were opened read-only using the separately
obtained `py-ols` checkout. The real integration tests imported them through
`PyOlsReader` into a temporary SQLite database and compared stored structures
with adapter snapshots. Both real-parser integration tests passed. This
confirms that the adapter changes preserve the existing observations for
these samples; it does not independently validate the parser's interpretations.

Before and after the test run, each sample's file size and SHA-256 matched the
captured baseline:

| Sample set | Result |
|---|---|
| Eight named OLS fixtures | All eight sizes and SHA-256 values unchanged |
| Temporary database | Used for the real import integration test |
| Independent WinOLS exports and confirmed project/version relationships | None available; `NOT_TESTED` |

No sample was altered or added to Git. The repository's full suite does not
include those external sample files in this isolated worktree, so its two
real-sample tests are skipped there; they were run separately against the
existing read-only sample directory.

## Verification

| Check | Result |
|---|---|
| Focused diagnostics, binary comparison, and workbench tests | 32 passed, 0 failed |
| Separate real-parser sample integration tests | 2 passed, 0 failed |
| Full unittest suite | 111 run: 109 passed, 0 failed, 2 skipped |
| Full-suite skips | Real-sample tests require external sample files and parser configuration in the worktree |
| Python child exit code | `0` for focused, real-parser, and full-suite runs |
| PowerShell wrapper exit code | `0` for focused, real-parser, and full-suite runs |
| `python -m compileall -q ols_platform deployment tests` | Exit `0` |
| `node --check ols_platform\dashboard.js` | Exit `0` |
| `git diff --check` | Exit `0` |

The parser, index, and evidence tests use synthetic or temporary inputs and
temporary databases. No full-corpus scan, rehash, or production database
operation was run.

## Remaining limitations

- Independent WinOLS exports and their project/version/map relationships
  remain unavailable. Independent content validation and ECU compatibility
  remain `NOT_TESTED`.
- The parser reports one format-version value; the adapter cannot compare it
  against a second independent declaration in the file.
- A parser exception discards partial in-memory parser output. It is reported
  as a parser failure rather than partially imported as if complete.
- Parser-reported slot extents, payload hashes, map definitions, and axes are
  structural observations only. They do not prove export boundaries, map
  decoding, values, or version association.
- The real sample set demonstrates formats 503 and 597 only; other format
  generations remain untested.
- No additional parser was used as a correctness oracle; parser agreement is
  not independent WinOLS validation.

Phase 11 remains in progress. Do not start automatic tuning generation or
promote parser-derived relationships to validated evidence on the basis of
this phase.
