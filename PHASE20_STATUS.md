# Phase 20 — Continuous integration

## Status

**Hosted CI passed on the isolated feature branch after a POSIX path
relocation defect was fixed.** This validates the configured tests and
static checks on GitHub Actions; it does not validate OLS semantics,
production indexing, or ECU compatibility.

## Workflow

The GitHub Actions workflow at `.github/workflows/ci.yml` runs on pushes,
pull requests, and manual dispatch with `contents: read` permission:

- Python 3.11: `python -m unittest discover -s tests -v`
- Python compilation: `python -m compileall -q ols_platform tests deployment`
- Node.js syntax checks for `ols_platform/dashboard.js` and
  `ols_platform/diagnostics.js`
- Job timeout: 20 minutes.

It does not start indexing, open production databases or queues, access the
corpus, modify fixtures, or run the external C parser.

## Hosted runs

1. Run [43146917704](https://github.com/BugraOrhan1/files-ols/actions/runs/43146917704)
   on `b203cd26e4d2843926c376b330907a1fbbf4d330` failed on Ubuntu in
   `test_source_root_relocation_rebases_queue_paths_atomically`. The
   relocation SQL recognized Windows `\` separators but not POSIX `/`
   separators. The failure was reproduced from the hosted test result.
2. The fix in
   [ols_platform/database.py](./ols_platform/database.py) recognizes both
   separator styles for indexed path tables and `source_files`. Windows
   targeted regression: 1 passed. The full local suite also passed after
   the change.
3. Run [43147415557](https://github.com/BugraOrhan1/files-ols/actions/runs/43147415557)
   on `c42bcc2149c5efa191808171f43c03b74412d061` completed successfully.
   The Ubuntu job ran 127 tests in 7.338 seconds, with 2 skipped and no
   failures. Python compilation and both Node syntax checks also succeeded.
   The source-root relocation regression passed.

## Latest local verification

On the Windows feature worktree, after the path fix:

- `python -m unittest discover -s tests -v`: 127 run, 125 passed, 0 failed,
  2 skipped; Python child exit code `0`, PowerShell wrapper exit code `0`.
- Both skipped tests require `PY_OLS_PATH` and the external parser checkout.
  Separate real-parser integration tests were run earlier against the eight
  samples and a temporary database: 2 passed, 0 failed; child and wrapper
  exit codes `0`.
- `python -m compileall -q ols_platform tests deployment`: exit code `0`.
- `node --check ols_platform/dashboard.js`: exit code `0`.
- `node --check ols_platform/diagnostics.js`: exit code `0`.
- `git diff --check`: exit code `0`.
- Pylance diagnostics for the changed Python module were empty.
- A read-only post-test integrity check compared all 14 protected OLS/BIN
  fixture sizes and SHA-256 values with the captured baseline: 14 matched,
  0 mismatches; Python child and PowerShell wrapper exit codes `0`.

## Operational checks and limits

- PID `45712` was observed read-only as a running `python.exe` process. No
  signal, pause, restart, or other control action was sent. Process presence
  and CPU-time observations do not establish worker health or indexing
  progress.
- The production SQLite index, live hash queue/state, corpus, and protected
  fixtures were not opened for application work or modified.
- The workflow emits a non-failing GitHub runner warning that actions
  targeting Node.js 20 are being forced to Node.js 24. Review action runtime
  compatibility during routine CI maintenance.

## Decision

Hosted CI acceptance for the configured workflow is **PASS** on the
successful run above. Independent WinOLS validation and ECU compatibility
remain `NOT_TESTED`; CI success is not parser or product release
certification.
