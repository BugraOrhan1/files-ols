# Phase 19 — Pinned C parser differential experiment

## Status

**The experiment ran successfully; the C parser is not integrated or approved
as a source of verified WinOLS semantics.** Its output is a second parser
observation only. Independent WinOLS validation remains `NOT_TESTED`.

## Source review and build

- Repository: `jinglinsen/ols-parser`, pinned source
  `c77616e8e4dbe8b4fd6a41828c6c3d63b22cf2c6`.
- License: 0BSD at the pinned root `LICENSE`.
- Source was cloned to a temporary directory, reviewed before compilation,
  and left outside this application repository.
- GCC/MinGW build command:
  `gcc -O2 -Wall -Wextra -std=c99 -D_POSIX_C_SOURCE=200809L ols_dump.c ols.c -o ols_dump.exe`
  — exit code `0`, no compiler warnings.
- The CLI accepts only a source path and display flags; it opens the OLS
  read-only, writes observations to stdout, and has no binary-export option.
  Logs were captured in a temporary directory and are not committed.

The implementation slurps the full OLS file into memory. It also assembles a
separate in-memory ROM buffer per reported version; these sizes can exceed
the source size. For `10001005.ols`, it reported three 70,778,800-byte ROM
buffers (about 202.5 MiB combined) from a 26,187,364-byte OLS input. This
allocation shape and the absence of a process-level memory cap make it
unsuitable for bulk parsing without further resource controls. No full-corpus
or 6-TB operation was run.

## Eight-sample result

Every parser invocation exited `0`, emitted zero warnings, and reported the
same format, version count, map count, and total segment count as the prior
captured run for this exact parser revision in the Phase 11E audit. This is a
reproducibility check against the same parser, not independent validation.
Machine-readable observations, including the eight baseline source hashes,
are recorded in [reports/phase19_c_parser_smoke.json](./reports/phase19_c_parser_smoke.json).

| Sample | Format | C-reported versions | Segments | Maps | Printed axes | Assembled ROM bytes by version |
|---|---:|---:|---:|---:|---:|---|
| `100004.ols` | 597 | 1 | 4 | 0 | 0 | 23,068,672 |
| `100007.ols` | 597 | 1 | 4 | 0 | 0 | 8,388,608 |
| `100008.ols` | 597 | 1 | 3 | 0 | 0 | 12,582,912 |
| `100009.ols` | 597 | 1 | 2 | 0 | 0 | 5,242,880 |
| `100010.ols` | 597 | 1 | 3 | 0 | 0 | 12,582,912 |
| `10001004.ols` | 503 | 1 | 3 | 0 | 0 | 507,824 |
| `10001005.ols` | 503 | 3 | 24 | 0 | 0 | 70,778,800 × 3 |
| `100010054.ols` | 503 | 4 | 32 | 0 | 0 | 4,194,224 × 4 |

The C parser reports zero maps for all eight samples. The `py-ols` adapter
reported 4 maps for `100009.ols` and 12 for `100010054.ols` in Phase 11E.
The discrepancy is preserved; no parser is selected as the winner. The C
parser's version grouping is also not directly comparable to `py-ols` named
version records or the application's physical slot enumeration.

All eight source SHA-256 values and sizes matched the captured fixture
baseline before the experiment. The final suite will repeat the 14-fixture
hash check after all release-hardening tests.

## Malformed-header smoke tests

Five temporary synthetic files were passed to the same executable; no
repository fixture was changed:

| Case | Expected observed behavior | Result |
|---|---|---|
| 23-byte truncated header | Exit `1`, “file too small for OLS header” | Passed |
| Bad magic | Exit `1`, “bad magic” | Passed |
| Bad signature | Exit `1`, “signature mismatch” | Passed |
| Unknown format value `9999` | Exit `0` with metadata warning; no unsupported-format error | Passed; weakness recorded |
| Declared size 25, actual size 24 | Exit `0` with size-mismatch warning | Passed; warning is non-fatal |

These tests establish only the observed header/error behavior, not OLS
correctness.

## Verification and limitations

- C parser build: GCC exit code `0`.
- Eight OLS invocations: all exit code `0`; Python runner exit code `0`;
  PowerShell wrapper exit code `0`.
- Five malformed-header assertions: all passed; Python runner exit code `0`;
  PowerShell wrapper exit code `0`.
- No C parser binary, extracted ROM, parser source, or synthetic binary
  fixture was added to Git.
- The parser has no upstream test suite or real-sample fixtures. Its
  reverse-engineered schema gates, map-search window, silent/soft omissions,
  and allocation behavior remain unresolved.
- No version payload hash was independently compared to a confirmed WinOLS
  export. The parser does not expose a SHA-256 export of its assembled ROM.
- No map values, original/tuned relation, ECU compatibility, or WinOLS
  semantics were inferred from parser agreement.

## Decision

Keep the C parser as a pinned, reproducible research comparator only. Do not
make it the default parser or store its version/map outputs as verified
evidence. Further integration requires bounded-memory changes or isolation,
regression/fuzz tests, and exact independent WinOLS exports and relationships.
