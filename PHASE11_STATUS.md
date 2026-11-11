# Phase 11 status — initial ECU identity evidence

## Scope and publication

- Repository: [BugraOrhan1/files-ols](https://github.com/BugraOrhan1/files-ols)
- Branch: `main`
- Verified base revision before these changes: `4bcb141f293e13e70adc20bd65e930a8d87c8fef`
- Validated implementation commit: `bc0012c322edf4d72654daa8fcbbb9ffac05ad7d`
  (implementation, tests, and README).
- Published Phase 11 revision: [`a93af53557d516459580b698e4c4c7a2ac8db129`](https://github.com/BugraOrhan1/files-ols/commit/a93af53557d516459580b698e4c4c7a2ac8db129).
- Remote `main` and the published code, tests, README, and status-document blob
  IDs were verified against the local Git objects after push.
- This is the **initial ECU identity and reference-evidence scope**, not a
  completion claim for all of Phase 11.

## Implemented behavior

The Workbench accepts raw `.bin` and `.ori` inputs only. The adapter labels
their format from the filename extension, streams and hashes every byte, and
records the full SHA-256 and size. `format_status=extension_only` and
`parser_status=not_run_no_validated_raw_binary_decoder` make clear that neither
the extension nor the hash validates the file as an ECU image. No ECU fields
are decoded from arbitrary raw bytes.

Identity and reference evidence remain distinct:

- Raw inputs without identity evidence have `UNKNOWN` identity fields.
- User-entered fields are marked `UNVERIFIED_USER_INPUT` when there is no
  supporting project evidence; they are not parser-extracted values.
- A field can be `DERIVED` only from direct parser metadata associated with one
  individually validated exact full-binary reference. Its evidence retains
  the parser name/version, format version and status, project metadata source
  and status, and project/version reference.
- Parser metadata or slot candidates that do not establish that unique
  relationship remain `INCONCLUSIVE`. A stale parser snapshot cannot promote
  a metadata candidate to `LIKELY`.
- Different candidate values or a conflict with user input are
  `CONFLICTING`; no identity value is selected in that case.
- `EXACT_CONTENT_MATCH` describes full-file byte identity (SHA-256 and size),
  not ECU compatibility. `CONFIRMED_REFERENCE` additionally requires a
  confirmed relation, `full_binary` representation, a passing latest check,
  and a check source hash equal to the current indexed OLS hash.
- More than one exact reference for the same bytes is ambiguous: the overall
  project/version assignment stays `INCONCLUSIVE`, while exact content
  identity is still exposed separately.
- `NO_MATCH` continues to mean no matching evidence was found in the available
  index, not that no reference exists.

No automatic tuning generation, checksum correction, map-value transfer, or
write to ECU files is implemented.

## Verification

Tests use synthetic bytes and temporary databases. No customer BIN/ORI,
live-index migration, production scan, or production hashing was used.

| Check | Result |
|---|---|
| Raw `.bin` and `.ori` identity remains unknown without evidence | Passed |
| Parser provenance and direct metadata status are retained | Passed |
| Exact bytes distinguished from ECU compatibility | Passed |
| Multiple exact references and conflicting metadata remain inconclusive/conflicting | Passed |
| Stale parser snapshot does not promote metadata candidates | Passed |
| Phase 10 import, comparison, history, dashboard/API regression coverage | Passed in full suite |
| Full unittest suite | 82 run: 80 passed, 0 failed, 2 skipped |
| Skips | Both real-OLS parser integration tests; `PY_OLS_PATH` and the configured eight-sample parser setup were not provided |
| Python child test exit | `0`; unittest ended with `OK (skipped=2)` |
| PowerShell/tool wrapper result | Reported `1` for the same test invocation despite the recorded Python exit `0`; this is a runner exit-propagation discrepancy, not a unittest failure |
| `python -m compileall -q ols_platform deployment tests` | Exit `0` |
| `node --check ols_platform\dashboard.js` | Exit `0` |
| `git diff --check` | Exit `0` |

The two skipped tests mean this validation does not establish how the optional
external `py-ols` parser behaves on the eight repository sample projects.

## Preserved worker and data

PID `45712` was verified running after the tests as
`python -m ols_platform.cli --database data\ols-index.sqlite3 hash-worker
--continuous --workers 2 ...`. It was not stopped, restarted, paused, or
reconfigured. Tests used temporary databases; the live SQLite index, queue,
source files, and hashing settings were not changed or queried for data.

The repository already tracks eight `.ols` samples and six `.bin` exports.
They were not added, modified, or removed. `.gitignore` excludes newly
untracked `*.bin`, `*.ori`, and `*.ols` files, but does not untrack existing
files. No database, backup, cache, or virtual environment is part of this
change.

## Limits and status

- No raw-BIN/ORI ECU decoder or validated ECU-specific input format is
  available. Intel HEX and other formats remain unsupported.
- Parser metadata describes the indexed OLS parser result; it is not an
  independent decode of the uploaded BIN/ORI.
- A single exact full-binary reference can provide a project/version link only
  under the validation conditions above; it does not by itself establish
  vehicle compatibility.
- No live WinOLS comparison was run as part of this scope.
- Full Phase 11 remains **in progress**. Only this initial identity evidence
  and safe reference-linking layer has been implemented and tested.

The final documentation follow-up revision is reported after its remote
verification.
