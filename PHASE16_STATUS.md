# Phase 16 — Integrated release-readiness verification

## Executive decision

**`GO_WITH_LIMITATIONS` for the tested local application scope.** The
integrated local test suite, static checks, targeted dashboard browser
interaction, and fixture-integrity checks passed. This is not a claim that
OLS parsing is independently correct, that parser-derived map structures
match WinOLS, that any binary is compatible with an ECU, or that the
application is ready for production-scale operation over the 6-TB corpus.

Phase 11 remains in progress. **Independent WinOLS validation:
`NOT_TESTED`. ECU compatibility: `NOT_TESTED`.**

## Scope and repository state

- Work was performed on the isolated `phase12-16-release-prep` branch,
  starting from Phase 11G commit
  `4d464d73a44ffa3005ac25cf78fbe7ab00b8b56b`.
- Phase 12–14 checkpoint commits already on this branch:
  - `020ede5e04d2e1ef75c32644418b9472041a896a` — Phase 12 report.
  - `368780b38d87fa4338078efdecc2dc9f2c28520d` — Phase 13 search ranking fix.
  - `bc10786c5c418f60ad7a462b8851bb218e6b6e24` — Phase 14 comparison report.
- Phase 15 was isolated in commit
  `df3681abca4d0020b6a6ac8e8d4eae912f4d98ff`.
- At the start of this continuation, the original
  `phase11e-audit` worktree had seven unrelated modifications in
  `PHASE11_STATUS.md`, `README.md`, `ols_platform/cli.py`,
  `ols_platform/database.py`, `ols_platform/reference_package.py`,
  `tests/test_compare.py`, and `tests/test_reference_package.py`. They
  remain unchanged and unstaged.
- No change was made to `main`; no PR was created or merged.
- The feature branch was based on the fetched remote history; remote
  `origin/main` was `4ae04f1d2c4b54f866ddfd46486f878c8a299a3b`, an ancestor of
  the isolated branch.

## Phase status

| Phase | Result | Scope boundary |
|---|---|---|
| 12 — parser reliability | Complete for reported local tests | Parser observations remain unvalidated against WinOLS exports. |
| 13 — indexing/search | Complete for reported local tests | Synthetic scale check only; no production corpus or throughput claim. |
| 14 — comparison/workbench | Complete for reported local tests | Byte-level results remain semantically `INCONCLUSIVE`. |
| 15 — dashboard accessibility | Complete for the targeted defect | Native comparison-history button; not a full accessibility audit. |
| 16 — integrated readiness | Complete for local verification below | `GO_WITH_LIMITATIONS`; not a production or ECU-validation release. |
| 11 — independent evidence | In progress | Independent WinOLS validation and ECU compatibility are `NOT_TESTED`. |

## Phase 15 integrated behavior

Comparison-history entries were focusable `<article>` elements advertised
as buttons, but only had a click handler. They were replaced by native
`<button type="button">` controls using the same click action, with visible
keyboard focus styling and no nested controls. Actual browser input confirmed
that Tab reaches the card in DOM order, Enter and Space each activate it once,
mouse click still activates it once, the report opens, and a disabled button
cannot be activated.

The focused automated regression check verifies native-button construction
and preservation of the click handler. The real-browser keyboard interaction
is separately described in [PHASE15_STATUS.md](./PHASE15_STATUS.md); it used
only a temporary database and four-byte synthetic input sequences.

## Integrated tests and static checks

Commands were run from the isolated worktree. The external parser path was
set only for the test process; the parser checkout was not modified.

| Command/check | Result |
|---|---|
| `$env:PY_OLS_PATH = "C:\Users\Bugra\Desktop\py-ols-study"; python -m unittest discover -s tests -v` | 119 run, 119 passed, 0 failed, 0 skipped; included both real-parser integration tests. |
| Python unittest child exit code | `0` |
| Outer PowerShell wrapper exit code | `0` |
| `python -m unittest tests.test_dashboard -v` | 5 passed, 0 failed. |
| Focused Python child / PowerShell wrapper exit codes | `0` / `0` |
| Playwright keyboard/mouse dashboard interaction | Passed: one activation each for Enter, Space, and mouse; disabled control produced no activation. |
| `python -m compileall -q ols_platform deployment tests` | Exit `0` |
| `node --check ols_platform\dashboard.js` | Exit `0` |
| `git diff --check` and staged-diff check | Exit `0` |

All automated tests use temporary inputs and temporary databases except the
real-parser integration tests, which read the eight tracked OLS samples and
write their observations only to a temporary SQLite database. No tests
configured or accessed a production index, production queue, network-root
source, or worker lease. No complete source-corpus scan, hash, or indexing
run was started.

### GitHub Actions evidence

The repository worktree contains no `.github/workflows` files. An
unauthenticated read of the public GitHub Actions workflow endpoint returned
`total_count: 0`; therefore there was no hosted workflow or run result to
compare with the local test output. `gh workflow list` and `gh run list`
could not authenticate in this environment (`gh` exit `4`). No CI result is
claimed.

## Protected worker and production state

PID `45712` was inspected read-only at multiple points. It was present as
`python.exe`, started `2026-11-11 16:17:49`, and its command line matched a
hash-worker label. Cumulative CPU time increased between observations. This
shows the process remained present and active during the checks; it does not
prove that it is making correct progress or that its queue is healthy.

The process was not signalled, stopped, restarted, paused, or reconfigured.
The production SQLite index, live hashing queue, lease/state, and corpus
roots were not opened or modified. Their contents and end-to-end status
remain deliberately unverified.

## Fixture integrity — before and after the full suite

For each tracked fixture, SHA-256 and exact byte size were captured
immediately before testing and recomputed after the full suite. **All 14 of
14 matched exactly.** No fixture was written, renamed, deleted, staged, or
changed.

| Fixture | Bytes | SHA-256 |
|---|---:|---|
| `ols files als ols/100004.ols` | 23,081,851 | `053aa96ea3021a6e3d71b11fe8023bab31650205a41196e8ecc39968b006167c` |
| `ols files als ols/100007.ols` | 8,538,495 | `726179f17d56922d3667e18f87af739e49f59732df963332195c0249dad6a178` |
| `ols files als ols/100008.ols` | 12,869,126 | `7234cdacd4509455724b9a2ec318c5ce7edc17cdb3210ef2f3691e290f32b4cf` |
| `ols files als ols/100009.ols` | 6,067,756 | `ef55e64d62358f1da11c36631ea0ba572383faa803188b556821ab5bcd3956ee` |
| `ols files als ols/100010.ols` | 12,886,631 | `4f20f13b6fc0c7a1606073b00a392ab8051db1c5689eff0fcc704bc594c8bd16` |
| `ols files als ols/10001004.ols` | 13,198,494 | `2e22340c61b1c9b44710501775669e8d75d0592d6bb936245ca80fd899db0326` |
| `ols files als ols/10001005.ols` | 26,187,364 | `4a117e3549cf457c1a8977f5e1da497517e7f3c21e80bf138ca72dd108cce2d3` |
| `ols files als ols/100010054.ols` | 17,457,135 | `eda083e60ad20c8b4f286b289bb6760f7e5a9666b496e7e1ce2ef16b978bc635` |
| `ols files geextracteert/WinOLS (BMW 7er (Original) - B57D30S0-G1).bin` | 2,621,440 | `ab794b422a71c7211230372c1aeceda613955443fdb93e18cfda3f5f7c0b9093` |
| `ols files geextracteert/WinOLS (BMW 7er (Stage 1) - B57D30S0-G1).bin` | 2,621,440 | `1e6aa221b88bf1b90bfdfb19eed4cd1e472c99ebcbda6c9d94fb6907322810ae` |
| `ols files geextracteert/WinOLS (Peugeot 307 (EGR OFF) - PO30052000000).bin` | 2,097,152 | `e662016a19ea37464b43ddfda40a5999563a8b053a6190a04270ffd0b76a918f` |
| `ols files geextracteert/WinOLS (Peugeot 307 (Original) - PO30052000000).bin` | 2,097,152 | `74ba24da1fca6583090c9dd4c6cdbdd889ab4df5ba60d2b617b08267bcb911c6` |
| `ols files geextracteert/WinOLS (Peugeot 307 (Stage 2 + EGR OFF) - PO30052000000).bin` | 2,097,152 | `c32ce984b47032d008735a5f82f07cf93305928e06b9c70459a9e9f9f8cd2d51` |
| `ols files geextracteert/WinOLS (Peugeot 307 (Stage 2) - PO30052000000).bin` | 2,097,152 | `0e153715dbbd54a1386102239cead4341057ff98bb32e88e987e6187334f29a8` |

The before-test baseline was kept outside the repository in session files;
the after-test comparison found zero mismatches.

## Release checklist

### GO for this bounded local scope

- [x] Phase 12–14 checkpoint reports and code are present on the isolated
  feature branch.
- [x] Comparison-history keyboard defect is fixed and covered by focused
  regression plus real-browser key/mouse activation checks.
- [x] Full available unittest suite passes with both real-parser integration
  tests enabled.
- [x] Python compilation, JavaScript syntax, and Git whitespace checks pass.
- [x] All 8 OLS and 6 BIN fixture sizes and SHA-256 values match the captured
  pre-test baseline.
- [x] Original dirty worktree and unrelated modifications remain preserved.
- [x] Hashworker PID `45712` was observed read-only and not controlled.
- [x] No changes to `main`, automatic tuning, ECU writing, flashing,
  checksum-correction, live database, live queue, or full-corpus run.

### NO-GO for broader claims or unrestricted production release

- [ ] Independent WinOLS exports with verified project/version/map
  relationships are still required. Independent parser agreement is not a
  substitute.
- [ ] ECU compatibility is still `NOT_TESTED`; no compatibility or tuning
  correctness claim is permitted.
- [ ] GPL-3.0 distribution obligations for `py-ols` require legal review
  before a distributed product release.
- [ ] Production-scale behavior across the 6-TB corpus and five network
  locations, including queue recovery and throughput, has not been measured.
- [ ] Live worker queue health was intentionally not inspected.
- [ ] Hosted GitHub Actions results are unavailable because the repository
  exposes no workflows; local test evidence is the only run evidence in this
  report.

## Remaining actions

1. Keep Phase 11 and independent WinOLS/ECU validation open until attributable
   WinOLS exports and exact project/version/map relationships are supplied
   and reviewed.
2. Obtain license/legal review before packaging or distributing the
   GPL-dependent parser integration.
3. Before any full-corpus rollout, plan a staged, bounded run with explicit
   storage, queue, worker, resume, and resource monitoring; do not infer
   throughput from the synthetic Phase 13 benchmark.
4. Review and merge the feature branch only through the user's normal review
   process. This work does not merge or modify `main`.
