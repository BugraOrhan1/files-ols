# Phase 26 — Integrated release-readiness verification

## Decision

**NO_GO for product distribution.** The isolated branch is suitable for
continued local development with explicit limitations, and the configured
GitHub Actions workflow passed. Distribution is blocked by the missing
repository license decision and legal review of the optional GPL-3.0
`py-ols` dependency. Independent WinOLS validation and ECU compatibility
also remain `NOT_TESTED`.

This report distinguishes passing engineering checks from unproven OLS
semantics, production-index health, and ECU applicability. A passing test
suite is not a substitute for independent WinOLS exports or compatibility
evidence.

## Scope and branch isolation

- Repository: `BugraOrhan1/files-ols`.
- Branch: `phase17-26-release-hardening`, based on the verified isolated
  feature branch; `main` was not changed.
- Phase 26 code fix commit:
  `c42bcc2149c5efa191808171f43c03b74412d061`.
- The fix was pushed only to the feature branch. No pull request was created
  or merged, and no force push was used.
- Worktree changes were isolated and reviewed before commit. No unrelated
  local changes were present at commit time.

## Phase 15 defect and integrated phases

The known Phase 15 defect was that comparison-history cards advertised
button interaction but only handled mouse clicks. The dashboard now uses
native buttons and its browser regression exercises Tab, Enter, Space, mouse
activation, exactly-once action dispatch, visible focus, and disabled
behavior. The targeted dashboard suite passed. Details are in
[PHASE15_STATUS.md](./PHASE15_STATUS.md).

Phases 12–16 have their own status records. The Phase 16 report's
`GO_WITH_LIMITATIONS` result is limited to its tested local application
scope; it does not supersede this Phase 26 `NO_GO` distribution decision.

| Phase | Result | Evidence and remaining limit |
|---|---|---|
| 17 — External parser/licensing review | Complete as a bounded review | Eight repositories were reviewed at pinned commits; no parser was approved as an authoritative reader. GPL-3.0 and provenance concerns remain. See [PHASE17_STATUS.md](./PHASE17_STATUS.md) and [reports/external_repo_review.md](./reports/external_repo_review.md). |
| 18 — Offline evidence workflow | Implemented and tested | Read-only SQLite audit, no-store comparison, exclusive report output, and manifest validation use temporary data. See [PHASE18_STATUS.md](./PHASE18_STATUS.md). |
| 19 — C parser experiment | Complete as research only | Pinned 0BSD parser built and ran on eight samples plus five malformed-header cases. It reported zero maps while py-ols reported maps for two samples; disagreement remains unresolved. It was not integrated. See [PHASE19_STATUS.md](./PHASE19_STATUS.md). |
| 20 — CI | Hosted workflow passed after fix | First Ubuntu run exposed the POSIX separator regression; the follow-up run passed all workflow steps. See [PHASE20_STATUS.md](./PHASE20_STATUS.md). |
| 21 — Bounded security/UX review | Complete within stated scope | No actionable findings were reported for the reviewed changes; this is not a full application security or accessibility certification. See [PHASE21_STATUS.md](./PHASE21_STATUS.md). |
| 22 — Hash-run recovery | Implemented and regression-tested | Recovery guards were tested with temporary databases and process-liveness checks. Production recovery was not run. See [PHASE22_STATUS.md](./PHASE22_STATUS.md). |
| 23 — Streaming comparison experiment | Complete for small synthetic inputs | Two 4 MiB files were compared at two chunk sizes. A 32 MiB `tracemalloc` trial exceeded its time limit and was stopped; no large-file memory profile exists. See [PHASE23_STATUS.md](./PHASE23_STATUS.md). |
| 24 — Dashboard UX review | Complete within stated scope | Loading/error/unavailable states and key interactions were reviewed; no screen-reader, WCAG, or cross-browser certification was performed. See [PHASE24_STATUS.md](./PHASE24_STATUS.md). |
| 25 — Dependency/license review | Findings documented; human decision required | No repository license or dependency manifest is present. Legal review is required before distribution. See [PHASE25_STATUS.md](./PHASE25_STATUS.md). |
| 26 — Integrated release readiness | Verification complete; distribution NO_GO | Local and hosted checks passed, but license, independent WinOLS, ECU, worker-health, and corpus-scale evidence gates remain open. |

## Phase 26 code finding

The first hosted CI run failed on Ubuntu in
`test_source_root_relocation_rebases_queue_paths_atomically`. The database
relocation SQL recognized Windows backslashes but not POSIX forward slashes.
The source-root relocation could therefore leave indexed paths unchanged on
POSIX systems.

The surgical fix in [ols_platform/database.py](./ols_platform/database.py)
recognizes both separator styles in the affected indexed path tables and
`source_files`, while retaining the existing exact-root match. The existing
regression test passed locally on Windows, where backslash paths are used,
and in GitHub Actions on Ubuntu, where forward-slash paths are used. No
application behavior outside source-root relocation was changed.

## Test evidence

### Local Windows worktree

| Command/check | Outcome | Exit evidence |
|---|---|---|
| `python -m unittest tests.test_sources_and_search.SourceAndSearchTests.test_source_root_relocation_rebases_queue_paths_atomically -v` | 1 passed | Python child `0`; PowerShell wrapper `0` |
| `python -m unittest discover -s tests -v` | 127 run; 125 passed, 0 failed, 2 skipped | Python child `0`; PowerShell wrapper `0` |
| `python -m compileall -q ols_platform tests deployment` | Passed | `0` |
| `node --check ols_platform/dashboard.js` | Passed | `0` |
| `node --check ols_platform/diagnostics.js` | Passed | `0` |
| `git diff --check` | Passed | `0` |
| Pylance diagnostics for `ols_platform/database.py` | No diagnostics | Empty diagnostics result |
| Post-suite protected-fixture size/SHA-256 check | 14/14 matched baseline; 0 mismatches | Python child `0`; PowerShell wrapper `0` |

The two skipped tests in the final default full-suite run require
`PY_OLS_PATH` and the separate parser checkout. Separate real-parser
integration tests had earlier passed 2/2 while importing the eight sample
projects into a temporary database. Those results are parser execution
evidence only; they do not establish WinOLS correctness.

### GitHub Actions

- Run [43146917704](https://github.com/BugraOrhan1/files-ols/actions/runs/43146917704)
  on commit `b203cd26e4d2843926c376b330907a1fbbf4d330` failed on the
  POSIX-path relocation regression described above.
- After the fix, run
  [43147415557](https://github.com/BugraOrhan1/files-ols/actions/runs/43147415557)
  on commit `c42bcc2149c5efa191808171f43c03b74412d061` completed with
  conclusion `success`. Its Ubuntu job ran 127 tests in 7.338 seconds;
  2 were skipped and none failed. Python compilation and both JavaScript
  syntax-check steps also succeeded.
- The runner emitted a non-failing warning that actions targeting Node.js 20
  are being forced to Node.js 24. This is a CI-maintenance item, not a test
  failure.

### Prior targeted evidence included in the release review

- Real-parser integration: 2 passed, 0 failed, using all eight OLS samples
  and a temporary database; the parser checkout is external and optional.
- Dashboard regressions: 5 passed, 0 failed; a separate browser exercise
  verified actual Tab, Enter, Space, mouse, disabled-state, and
  exactly-once behavior.
- Hash-worker/source/importer regression modules: 33 passed, 0 failed,
  using temporary databases and files.
- Bounded comparison experiment: two 4 MiB synthetic-file cases passed at
  64 KiB and 1 MiB chunks; each found the three known changes. The longer
  32 MiB `tracemalloc` experiment did not complete and is not counted as
  passed.

## Protected data and worker observations

- All eight tracked OLS samples and six tracked BIN exports were checked
  read-only after the final local suite. Their sizes and SHA-256 hashes
  matched the captured baseline: 14/14 unchanged.
- No production SQLite database, live hash queue/state, or 6-TB corpus was
  opened, modified, scanned, rehashed, or rebuilt.
- PID `45712` was queried read-only and remained present as `python.exe`.
  CPU-time readings increased between observations, but this does not prove
  that the process is healthy, consuming the intended queue, or making
  indexing progress. The process was not signaled, paused, stopped, or
  restarted.

## Release checklist

| Gate | Result | Required evidence/action |
|---|---|---|
| Isolated feature branch; no `main` changes | PASS | Branch push verified; no merge or PR was made. |
| Local full test suite and static checks | PASS | 127 tests, 125 passed, 2 optional parser tests skipped; compile and both Node checks passed. |
| Hosted CI | PASS | Ubuntu workflow run 43147415557 succeeded on the code-fix commit. |
| Protected sample integrity | PASS | All 14 tracked OLS/BIN fixtures matched size and SHA-256 baseline. |
| Phase 15 keyboard activation | PASS within tested scope | Native button behavior and actual browser keyboard activation regressions passed. |
| External parser correctness against WinOLS | NOT_TESTED | Obtain independent WinOLS exports and verify project/version/map relationships and binary hashes. |
| ECU compatibility | NOT_TESTED | Requires genuine ECU/software/hardware evidence; parser agreement is insufficient. |
| Repository licensing/distribution rights | BLOCKED | Owner must select/add repository terms and obtain legal review for GPL-3.0 parser use. |
| Production worker/database/queue health | NOT_TESTED | Requires an approved read-only operational health check; this work intentionally did not open live state. |
| 6-TB/five-location throughput and resource limits | NOT_TESTED | Requires a separately approved bounded production-scale plan; no corpus scan or full rehash was run. |

## Remaining release blockers and human actions

1. Select the repository's license or explicitly restrict distribution, and
   obtain legal review of the intended relationship with `py-ols` GPL-3.0.
2. Supply independently verifiable WinOLS exports for the same projects,
   including active and non-active versions, and confirm their project,
   version, map, and binary relationships. Do not promote parser-derived
   structures to verified WinOLS semantics before this comparison.
3. Provide genuine ECU compatibility evidence before making compatibility
   claims.
4. Arrange an approved, non-disruptive health check for the live worker and
   queue. This report does not claim that PID presence proves progress.
5. Define and approve resource limits, resumability, and throughput targets
   before any full-corpus indexing. The 6-TB corpus was deliberately not
   scanned.

## Final status

- Phase 15 accessibility defect: fixed and regression-tested.
- Phase 16 integrated local verification: completed with limitations as
  recorded in [PHASE16_STATUS.md](./PHASE16_STATUS.md).
- Phases 17–25: status is as described above; Phase 19 remains an
  experimental parser comparison only.
- Phase 26 release-readiness checks: completed for the bounded local and CI
  scope.
- Independent WinOLS validation: **NOT_TESTED**.
- ECU compatibility: **NOT_TESTED**.
- Distribution decision: **NO_GO** until the licensing and independent
  validation blockers are resolved.
