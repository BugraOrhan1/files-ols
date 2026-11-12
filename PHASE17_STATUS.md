# Phase 17 — External repository and provenance review

## Status

**Review complete with limitations. No external source code was copied or
vendored, and no new parser was approved as an authoritative reader.** The
application continues to use its existing `py-ols` adapter and marks parser
output as unvalidated without independent WinOLS evidence.

All eight candidate repositories were reviewed read-only at the pinned
commits recorded in [reports/external_repo_review.md](./reports/external_repo_review.md).
The review considered repository purpose, implementation shape, license,
available tests/CI, public issue tracker state, safety-relevant behavior, and
whether the project is suitable for the existing read-only indexer.

## Key outcomes

- `ols-parser` is the strongest alternative for a later isolated experiment,
  but has no upstream test suite or real-sample corpus and contains
  reverse-engineered version gates and fail-soft omissions. It is not yet a
  correctness oracle.
- `py-ols` issue #1 reports `hex(float)` failure in its command-line axis
  display. The application adapter stores axis address and floating-point
  conversion offset separately; a regression test now protects that adapter
  behavior. This does not fix or certify the external parser's CLI.
- GPL-3.0 components (`py-ols`, LinOLS) require legal review before
  distribution. Repositories without a discoverable license are excluded
  from source reuse. No legal clearance is claimed.
- WinOLS MCP is explicitly untested against a live WinOLS instance and has
  high-risk write and outbound-HTTP tools. It is not adopted or executed.
- The open GitHub items found during this check are listed in the research
  report. They do not establish parser correctness.

## Verification

- Baseline suite before Phase 17 changes: 119 tests run, 117 passed, 0
  failed, 2 skipped; Python child exit code `0`, PowerShell wrapper exit code
  `0`.
- Focused parser/diagnostic regressions after the Phase 17 change: 21 passed,
  0 failed; Python child exit code `0`, PowerShell wrapper exit code `0`.
- The two skipped tests require `PY_OLS_PATH`; they are scheduled for an
  isolated real-sample run in later release verification.
- At the pinned Phase 16 base commit, GitHub exposed no Actions workflows or
  check runs. Local test results are not represented as hosted CI evidence.
- Independent WinOLS export validation and ECU compatibility remain
  `NOT_TESTED`.

## Remaining work

The C parser remains eligible only for a separate, temporary-directory
experiment after its source and build inputs are reviewed. No external parser
results are promoted to verified project, version, map, or ECU evidence.
