# Phase 11G — Independent code review and release readiness

## Executive verdict

**`PASS_WITH_LIMITATIONS` for the reviewed Phase 11E/11F scope. Phase 11 is
not ready to be marked complete.** One medium-severity reader defect was
reproduced and fixed with regression tests. The parser audit, binary
comparison, and ECU evidence paths preserve their stated uncertainty
boundaries in the reviewed cases. Independent WinOLS validation and ECU
compatibility remain `NOT_TESTED`.

This review establishes behavior of the application adapter and tests. It
does not establish that either OLS parser correctly represents WinOLS
projects, that extracted bytes are correct WinOLS exports, or that a binary
is compatible with an ECU.

## Starting state and isolation

- Original worktree: branch `phase11e-audit`, HEAD
  `77f68e338f9c8aaaa1a51cba4db8e206abd76f66`.
- Verified remote `phase11e-audit`: same SHA as the requested Phase 11E
  commit.
- Verified remote `phase11f-deep-validation`:
  `4febefe7b70c6e5448cc143996db6d9cea6b9448`, matching the expected Phase
  11F commit.
- Remote `main` remained at `4ae04f1d2c4b54f866ddfd46486f878c8a299a3b`.
- Created a separate worktree at
  `C:\Users\Bugra\AppData\Local\Temp\files-ols-phase11g-review` on branch
  `phase11g-review`, starting exactly from the verified Phase 11F SHA.
- The original worktree already had unstaged changes in `PHASE11_STATUS.md`,
  `README.md`, `ols_platform/cli.py`, `ols_platform/database.py`,
  `ols_platform/reference_package.py`, `tests/test_compare.py`, and
  `tests/test_reference_package.py`. Those changes were not edited, staged,
  or discarded.
- No PR was opened or merged. No production database, live queue, hashworker,
  or corpus root was accessed.

## Findings

### Medium — malformed version geometry could label a slot incorrectly (fixed)

**Affected code:** `PyOlsReader._enumerate_binary_slots` in
`ols_platform/reader.py`.

**Evidence and reproduction:** the Phase 11F code built its candidate layout
only from records with positive `blob_size` and `declared_slot_count`, but
later considered every record when assigning names to physical slots. On a
16-byte synthetic file, these records:

```text
slot 0: count=2, offset=0, size=8, name="First"
slot 1: count=0, offset=8, size=0, name="InvalidSecond"
```

previously yielded two enumerated slots and labeled slot 1
`InvalidSecond` without an issue. The record's invalid geometry had been
excluded from layout inference but not from slot labeling. This could promote
an inconsistent parser record into a misleading slot/version association.

**Fix:** validate every version record's basic geometry (nonnegative slot
index and source offset, positive declared count, and blob size greater than
four bytes) before deriving a layout. If any record has invalid basic
geometry, slot enumeration stops with `invalid_binary_version_record` and
`binary_layout.status="invalid_records"`. The original parser observations
remain in the snapshot; the import is stored as partial without any inferred
slot records. Out-of-range nonnegative indexes receive the specific
`version_slot_index_out_of_bounds` issue and do not label a valid slot.

**Regression coverage:** tests cover zero/negative counts, zero/negative
sizes, negative offsets and indexes, oversized counts, out-of-range indexes,
inconsistent layouts, multiple conflicting records in different orders, and
temporary database import status.

### Informational — Parser B execution evidence is captured, not replayed here

The Phase 11E report and machine-readable JSON identify Parser B's repository,
revision, license, per-sample observations, and bounds flags. The review
recomputed the reported segment extents against source sizes and cross-checked
the sample hashes and counts. The captured Parser B build/log files are not
part of this repository, so this review did not rerun that C parser. Its
output remains a recorded parser observation, not independent validation.

### No additional confirmed defects in the reviewed scope

- Parser outputs, metadata, payload hashes, addresses, map structures, and
  parser agreement are not treated as WinOLS ground truth.
- The reviewed binary comparison uses bounded chunk reads, hashes both
  streams, reports half-open differing ranges, and gives deterministic
  results across tested chunk sizes. Errors opening or reading files
  propagate; no successful fallback is fabricated.
- ECU workbench candidates are separated from exact full-file references.
  Stale or multiple references remain inconclusive; metadata-only candidates
  remain candidates; user input is unverified absent qualifying evidence.
  A matching hash is described as byte identity, not ECU compatibility.
- The application has no automatic tuning, map-writing, checksum-correction,
  or flashing behavior in the reviewed paths.
- `PHASE11E_STATUS.md` records GPL-3.0 for `py-ols`, and the README/status
  materials do not claim independent WinOLS validation. The license notes
  are not legal advice or a distribution clearance.

## Phase 11E report consistency

The JSON report was parsed and its aggregate claims checked against its eight
sample records. The read-only `py-ols` inspection of the same tracked samples
was used to confirm version-description presence and the reported import
text. Results:

| Report claim | Cross-check |
|---|---|
| Eight sample entries, formats 503 and 597 | Confirmed; Parser A and B format values agree per sample |
| Parser A version records / enumerated slots | 8 / 26 |
| Parser A maps / axes | 16 / 31 |
| Empty Parser A metadata values | 18 |
| Empty map names / incomplete axes | 16 / 31 |
| Empty version descriptions | 2; the other 6 contain import text in the read-only parser output |
| Parser A/B map count disagreements | `100009.ols`: 4 vs 0; `100010054.ols`: 12 vs 0 |
| Parser B segment bounds | All 75 recorded extents are within their source-file sizes when recalculated from the reported source offset, framing length, and payload byte count |
| Independent WinOLS validation | `NOT_TESTED` globally and for every parser result |

These are consistency checks of parser observations and captured report data.
They do not determine which parser is correct. Parser B observations were not
rerun during this review.

## Phase 11F reader and persistence review

- Exact duplicate `ParsedVersion` records are collapsed only for canonical
  database storage; raw observations and duplicate counts are retained.
- Conflicting records for a slot do not produce a canonical project-version
  row or an arbitrary slot label. The conflict is diagnostic, and the slot
  name status stays `conflicting_records`.
- The new malformed-geometry guard closes the case where a zero/negative
  count, size, offset, or index could be ignored during layout construction
  but later influence labeling.
- Inconsistent positive layouts are rejected. Declared slot counts above
  the configured observed bound are rejected without enumerating slots.
  Slot extents outside the actual source size and short reads are reported.
- Parser exceptions after partial state return a parser-failure snapshot;
  partial maps/versions are not promoted as a successful parse.
- The stored schema and report schema versions were not changed. Added
  snapshot observations and counts are additive; no historical database was
  read or migrated.

The adapter still relies on the documented, parser-derived fixed-stride
interpretation for candidate slot enumeration. That interpretation and the
contents of each payload remain unvalidated against WinOLS exports.

## Binary comparison and ECU workbench review

Synthetic temporary-file tests cover identical content, empty files, unequal
sizes, changed runs spanning chunk boundaries, deterministic output across
chunk sizes, differing-byte counts, and read sizes no greater than the
configured chunk size. No full-size corpus comparison or benchmark was run.

Workbench tests cover raw `.bin`/`.ori` identification limits, unsupported or
incomplete inputs, exact content references, stale source hashes, duplicate
and ambiguous references, metadata candidate conflicts, parser-slot
candidates, and failed/missing reference paths. Results preserve the
distinction between exact content identity, project/version relationship,
metadata candidate evidence, and compatibility.

## Verification

All commands ran in the isolated review worktree. Unless stated otherwise,
PowerShell wrapper exit and Python child exit were both `0`.

| Command | Result |
|---|---|
| `python -m unittest tests.test_ols_sample_diagnostics -v` | 20 passed, 0 failed |
| `python -m unittest tests.test_phase11e_parser_audit -v` | 4 passed, 0 failed |
| `python -m unittest tests.test_compare -v` | 6 passed, 0 failed |
| `python -m unittest tests.test_ecu_workbench -v` | 11 passed, 0 failed |
| `$env:PY_OLS_PATH='C:\Users\Bugra\Desktop\py-ols-study'; python -m unittest tests.test_ols_samples -v` | 2 passed, 0 failed; all eight samples imported into a temporary database |
| `$env:PY_OLS_PATH='C:\Users\Bugra\Desktop\py-ols-study'; python -m unittest discover -s tests -v` | 116 run, 116 passed, 0 failed, 0 skipped |
| `python -m compileall -q ols_platform deployment tests` | Exit `0` |
| `node --check ols_platform\dashboard.js` | Exit `0` |
| `git diff --check` | Exit `0` after final documentation edits |

The Phase 11E JSON consistency check and separate version-description check
both exited `0`. The full suite used the documented external parser checkout
and the tracked sample directory; no environment-dependent tests were
skipped.

### Fixture integrity

Before testing, SHA-256 and sizes were captured for all 14 tracked binary
fixtures: eight OLS samples and six BIN exports. After the test runs, every
file matched its original size and SHA-256. No fixture was staged or changed.

## Independent WinOLS validation

**`NOT_TESTED`.** No genuine independent WinOLS exports with verified
project/version/map relationships were available and reviewed in this phase.
Parser A/B agreement or disagreement, file hashes, parser-derived offsets,
map definitions, and synthetic fixtures do not satisfy that requirement.
No ECU compatibility claim is made.

## Remaining risks and release-readiness decision

1. Independent WinOLS validation remains absent; Phase 11 cannot be closed
   as a validated OLS import or ECU-compatibility workflow.
2. The fixed-stride slot interpretation and parser-derived payload boundaries
   remain hypotheses until checked against attributable WinOLS exports.
3. Parser B's recorded run cannot be independently replayed from repository
   contents alone; preserve its command/build logs with future audit updates.
4. Only sample-set format generations 503 and 597 were observed. Other format
   generations remain untested.
5. The GPL-3.0 parser's distribution implications require a separate legal
   review before packaging or distribution.

**Release decision:** the reviewed code changes and regression scope are
`PASS_WITH_LIMITATIONS`; do not mark Phase 11 complete, claim WinOLS semantic
validation, or claim ECU compatibility. Keep Phase 11 in progress until
independent exports and their relationships are supplied and checked.
