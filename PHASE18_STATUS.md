# Phase 18 — Offline evidence intake

## Status

**Implementation complete for an offline, single-packet audit path.** The
existing reference-package gates remain authoritative; this phase does not
raise any evidence status or claim independent WinOLS validation.

## Changes

- Added the draft 2020-12 JSON Schema for reference-package manifest version
  1 at `schemas/winols-reference-package-v1.schema.json`. Unknown/additive
  fields remain permitted for forward compatibility. The schema requires
  source identity, WinOLS/project relation, review state, version inventory,
  and hashed artifact metadata.
- Added `IndexDatabase(read_only=True)`, which opens an existing SQLite file
  with URI `mode=ro`, enables SQLite `query_only`, and skips directory
  creation, WAL setup, and schema initialization. Existing writable callers
  are unchanged.
- Added `reference-package-audit`, which runs the existing package
  inspection, version matching, metadata comparison, and map comparison
  gates without writing audit history. It returns an explicit audit version
  and `database_writes: false`. A positive result requires all four checks
  to pass and declared complete version coverage; incomplete/unknown
  evidence remains `INCONCLUSIVE` or `NOT_TESTED`.
- Added `compare --no-store` for streaming binary comparison without opening
  or creating a SQLite index. Optional JSON reports use exclusive creation,
  reject existing outputs, and reject paths resolving to inputs or the
  configured database/SQLite sidecars.
- Documented the commands and schema in the README.

## Verification

- Focused binary-comparison and reference-package tests: 30 passed, 0
  failed; Python child exit code `0`, PowerShell wrapper exit code `0`.
- Regressions verify: no index creation for `compare --no-store`; protected
  inputs are not overwritten; read-only SQLite rejects writes and does not
  create a missing database; offline audit does not append database history;
  incomplete coverage cannot pass; and schema JSON parses and matches the
  generated manifest's required keys and version/type constants.
- Real-parser integration against the separate `py-ols` checkout: 2 passed,
  0 failed; Python child exit code `0`, PowerShell wrapper exit code `0`.
- All 14 tracked OLS/BIN fixtures matched the captured pre-test size and
  SHA-256 baseline after the integration run; hash-check exit code `0`.
- `git diff --check`: exit code `0`.
- Tests use temporary directories and temporary SQLite databases only.
  Production index, hash queue, source tree, and worker were not opened.

## Limitations

- No JSON Schema validator is installed or added as a runtime/test
  dependency. The schema is parsed and its core contract is checked by tests;
  exhaustive draft-2020-12 validation was not run.
- Offline audit is per project packet, not a bulk-corpus operation. Existing
  comparison routines may stream/re-hash the same source during separate
  gates; do not point it at a corpus root.
- A `PASS` describes the existing evidence gates only. It does not establish
  that parser output is correct, that a version is an original or tuned
  binary, or that a file is ECU-compatible.
- Independent WinOLS validation and ECU compatibility remain `NOT_TESTED`.
