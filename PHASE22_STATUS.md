# Phase 22 — Temporary database and source recovery

## Status

**Recovery coverage added and verified on temporary databases and files.**
The live database, queue, worker, and source corpus were not opened.

## Reproduced defect and fix

The existing recovery command asked its operator to confirm that no hashing
process was active, but `recover_hash_run()` itself did not check the
recorded worker PID before requeueing `HASHING` jobs and resetting worker
state. It also returned the control row's previous run ID even when that
run had already completed.

Recovery now performs a read-only liveness check on the recorded worker PID
inside the database transaction and refuses when the process appears alive,
the PID is invalid, or the worker is marked active without a PID. It marks a
run `INTERRUPTED` and reports its ID only when the stored run is actually
`RUNNING`. Stale `HASHING` jobs are requeued as `RETRY_PENDING`; completed
jobs/runs are not reported as recovered.

## Regression cases

- Simulated interrupted run, dead PID, close/reopen of a temporary SQLite
  database, recovery of the queued job, successful one-file rehash, and
  repeated recovery with no duplicate work.
- Live-worker PID case uses the test process PID. Recovery raises explicitly
  and transaction rollback preserves the run, control, and worker state.
- Existing source disconnect/reconnect, importer resume, worker pause/resume,
  retry, changed-file, and queue persistence tests were rerun.

## Verification

- Targeted recovery tests: 2 passed, 0 failed; Python child exit code `0`,
  PowerShell wrapper exit code `0`.
- Source/hash-worker and importer regression modules: 33 passed, 0 failed;
  Python child exit code `0`, PowerShell wrapper exit code `0`.
- Pylance diagnostics for `ols_platform/database.py` and
  `tests/test_sources_and_search.py`: no diagnostics.
- No production database, live queue, actual worker PID, corpus, or protected
  fixture was accessed by the tests.

## Limitations

The test simulates a process crash by persisting a non-existent PID in a
temporary database. It does not execute recovery on production state and
does not establish the health or progress of the user's live worker. A
human must still confirm that no worker process is active before invoking
the documented recovery command.
