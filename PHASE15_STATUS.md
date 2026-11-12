# Phase 15 — Dashboard accessibility

## Status

**The comparison-history keyboard-accessibility defect is fixed and verified
in the targeted local scope.**

## Finding and root cause

Each comparison-history report was rendered as an `<article>` with
`tabIndex=0` and `role="button"`. Its only activation handler listened for
`click`, so adding a tab stop and button role did not provide the native
Enter/Space activation behavior of a button. The analysis-history cards were
not affected: they contain actual native buttons for their two actions.

## Implementation

- Comparison-history cards are now native `<button type="button">` elements.
- The existing click handler and report-opening behavior are unchanged;
  native button activation dispatches the same click event once.
- Removed the redundant custom role, tab index, and generic aria-label. The
  button's accessible name now comes from its visible report filenames,
  difference summary, and `INCONCLUSIVE` status.
- Added button-card styling, hover feedback, and a visible `:focus-visible`
  outline. The cards contain no nested interactive controls.
- Added a focused Python regression check that guards the semantic native
  button construction and the existing click handler. Real key activation
  was additionally exercised in the browser as described below.

## Verification

| Check | Result |
|---|---|
| `python -m unittest tests.test_dashboard -v` | 5 passed, 0 failed |
| Python child exit code | `0` |
| PowerShell wrapper exit code | `0` |
| `node --check ols_platform\dashboard.js` | Exit `0` |
| Real-browser keyboard/mouse interaction | Passed; temporary dashboard and Workbench database only |

The browser check imported two four-byte synthetic byte sequences into a
temporary Workbench, created one byte-comparison report, and then used actual
Playwright keyboard input against the rendered dashboard. Starting at the
comparison-submit button, five Tab presses traversed the four analysis-card
actions and focused the comparison-history button. Its visible focus outline
was `solid`; its accessible text included both filenames and the
`INCONCLUSIVE` state; it had no nested interactive controls. Enter caused
exactly one click and displayed the report; Space caused exactly one
additional click; a mouse click caused exactly one further click. Setting the
button disabled prevented focus and all subsequent Enter, Space, and click
attempts from activating it. The comparison remained a byte-level
`INCONCLUSIVE` result and was not treated as tuning or ECU evidence.

The test server used a temporary index under the session files directory.
No tracked binary fixture, configured source root, production database,
queue, or live worker was used by this browser test.

## Scope and limitations

Neighboring analysis-history controls are already native buttons. The
comparison history currently renders only reports returned by the local
history API; there is no separate unavailable-report card state. Native
disabled behavior was checked on the actual rendered button, but no
unavailable state was fabricated. This is a targeted fix, not a full
accessibility audit of every dashboard page.
