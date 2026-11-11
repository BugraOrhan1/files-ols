# Phase 10 status and Phase 11 readiness

## Repository and release

- Repository: [BugraOrhan1/files-ols](https://github.com/BugraOrhan1/files-ols)
- Branch: `main`
- Validated Phase 10 source/test commit: `2a06e88306435a483571f28a69f54ff7a264ba49`
- The Phase 10 source/test commit was pushed to `origin/main` after the full
  suite passed. This status report is a documentation-only follow-up.
- The verified pre-task revision was `23efc6ee6a589c4ac03dc77ba0592b428e3a3ffa`;
  local `main` and `origin/main` matched it before these changes.

## Implemented

- The dashboard accepts raw `.bin` and `.ori` byte streams. It streams the
  upload, calculates SHA-256 over every byte, records the size, and keeps a
  content-addressed Workbench copy. The original file is not modified.
- `.ori` is treated as an original raw binary input, not decoded as an OLS
  project or another address-bearing format. Intel HEX and other formats are
  rejected.
- Reference lookup distinguishes full-binary exact matches, parser-slot
  candidates, and metadata candidates. An exact match does not establish ECU
  compatibility. Parser slot matches remain `INCONCLUSIVE`; a confirmed
  reference additionally requires a confirmed relationship and a passing
  check against the current source hash.
- Two archived binaries can be compared using bounded streaming reads. Reports
  include hashes, sizes, differing-byte counts, half-open changed ranges, and
  provenance. Different sizes are reported as byte differences; semantic
  interpretation remains `INCONCLUSIVE`.
- Reanalysis and comparison history is stored separately at
  `data\ecu-workbench\workbench.sqlite3`; archived copies are in
  `data\ecu-workbench\files`. Workbench API access to the live project index
  uses a read-only SQLite connection.
- Loopback peer, Host, and POST Origin checks reject forged Host headers.
  Input type, size, incomplete uploads, unknown analysis IDs, and missing
  indexed reference files have explicit error paths.
- `.gitignore` now excludes newly untracked `.bin`, `.ori`, and `.ols` files.
  `data/`, SQLite backups, Python caches, and `.venv/` are also ignored.

## Verification

| Area | Result |
|---|---|
| BIN/ORI upload, full-content SHA-256, size and original preservation | Tested with temporary synthetic data |
| Exact, multiple, missing, stale and unconfirmed reference outcomes | Tested |
| Streaming comparison, same/different sizes, changed ranges and exact match | Tested |
| Persistent analysis/comparison history and reload | Tested with temporary SQLite databases and a browser reload |
| Isolation from the project index | Tested: index schema version and table row counts did not change during a temporary API upload |
| Forged Host, invalid extension, unknown analysis, missing reference path | Rejected/tested |
| Interactive dashboard flow | Tested in a browser using temporary synthetic `.ori` and `.bin` files; import, comparison and history after reload succeeded |
| Full test suite | 80 tests: 78 passed, 0 failed, 2 skipped |
| Full test process exit | `0`, captured after the command using CMD delayed expansion |
| PowerShell tool result for that command | `1`, despite the child test process reporting `TEST_EXIT=0` and unittest reporting `OK`; no later PowerShell check ran as part of the child test command |
| `python -m compileall -q ols_platform deployment tests` | Exit `0` |
| `node --check ols_platform\dashboard.js` | Exit `0` |
| `git diff --check` | Exit `0` |

The two skipped tests are real-OLS parser integration tests guarded by
`PY_OLS_PATH`. That environment variable was unset, so actual `py-ols` parser
execution was not part of this verification. The suite also prints expected
inventory fixture statuses such as `scan_warnings` and `unavailable`; these
were not test failures.

All Workbench, dashboard, and browser validation used temporary databases and
synthetic bytes. No customer BIN/ORI was imported, and no full source scan or
hash run was started.

## Limitations and preserved state

- A `.bin` or `.ori` extension does not prove that the bytes are a valid ECU
  image. Automatic ECU identification, HEX parsing, address translation,
  checksum interpretation/correction, map-value decoding, and tuning output
  are not implemented.
- No production/customer BIN/ORI has been validated through the Workbench.
  Interactive validation used only the temporary browser fixtures.
- The live SQLite database was not integrity-scanned during this task because
  the hashing worker was active. Workbench tests used isolated databases; the
  active worker's database and queue were not opened for writing or migrated.
- PID `45712` was verified running after the implementation commit and after
  the final test run. Its command still targeted the existing
  `data\ols-index.sqlite3` queue with two readers. The worker was not stopped,
  restarted, or reconfigured.
- The repository baseline already tracks eight `.ols` samples and six `.bin`
  exports. Their status as customer data is not established here. None were
  added or changed by this follow-up; ignore rules do not untrack files already
  committed. No SQLite database, backup, corpus, cache, or virtual environment
  was included in the Phase 10 source/test commit.

## Local startup

From the repository root, start the local dashboard with:

```powershell
python -m ols_platform.cli serve
```

Open `http://127.0.0.1:8765/`. The Workbench stores its persistent history
beside the configured index, in the separate `ecu-workbench` directory.

## Phase 11 preparation

Phase 11 has **not** started. Phase 10's remaining evidence boundary is clear:
raw-byte identity and comparisons are available, but format-aware ECU meaning
is not. Before implementing Phase 11, define and validate a narrow, supported
input/import format and an independent reference set. Potential work items to
scope are:

1. A validated ECU identity/import adapter with explicit format and address
   semantics; do not infer identifiers from arbitrary bytes.
2. Independently verified links between a selected binary, project version,
   map/axis definitions, and decoded byte ranges.
3. Additional provenance, ambiguity, and missing-reference workflows based on
   customer-independent fixtures.
4. A separate acceptance decision before any future feature could write or
   modify a tuning image; no such feature is present or enabled now.

Treat these as preparation topics, not implemented Phase 11 functionality.
