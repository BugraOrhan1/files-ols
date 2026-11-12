# Phase 20 — Continuous integration

## Status

The repository now has a minimal GitHub Actions workflow at
`.github/workflows/ci.yml`. The workflow is configured for pushes, pull
requests, and manual dispatch, and grants only `contents: read`.

## Workflow checks

- Python 3.11: `python -m unittest discover -s tests -v`
- Python compilation: `python -m compileall -q ols_platform tests deployment`
- Node.js 20 syntax checks for `ols_platform/dashboard.js` and
  `ols_platform/diagnostics.js`
- Job timeout: 20 minutes.

The workflow does not start indexing, open production databases or queues,
access the corpus, modify fixtures, or run the external C parser.

## Local verification

On the Windows feature worktree, the exact workflow test and static-check
commands completed:

- Unit suite: 125 tests run, 123 passed, 0 failed, 2 skipped; Python child
  exit code `0`, PowerShell wrapper exit code `0`.
- Both skipped tests are the py-ols real-sample tests requiring `PY_OLS_PATH`
  and workspace OLS samples. The separate real-parser integration run
  completed 2 tests, 2 passed, 0 failed; child and wrapper exit codes `0`.
- `compileall`: exit code `0`.
- `node --check ols_platform/dashboard.js`: exit code `0`.
- `node --check ols_platform/diagnostics.js`: exit code `0`.
- `git diff --check`: exit code `0`.
- After the suite, all 14 protected OLS/BIN fixture sizes and SHA-256 values
  matched the captured baseline; hash-check exit code `0`, wrapper exit code
  `0`.
- Pylance diagnostics were empty for the five changed Python code/test files.

## Hosted status and operational checks

- No hosted GitHub Actions run is verified yet. The workflow file has not
  been pushed to the remote feature branch.
- `gh auth status` returned exit code `1` and reported no authenticated
  GitHub host. `actionlint` and a Python YAML parser are not installed, so
  this local run does not establish that GitHub has accepted the workflow.
- Process PID `45712` was observed read-only as a running `python` process.
  No command, signal, pause, restart, or other control action was sent.
- The production SQLite index, live hash queue/state, corpus, and protected
  fixtures were not opened for application work or modified.

## Decision

Local checks pass. Hosted CI acceptance remains **NOT_TESTED** until this
branch can be pushed and the corresponding GitHub Actions run is inspected.
Do not treat local test success as parser validation or ECU compatibility;
both remain `NOT_TESTED`.
