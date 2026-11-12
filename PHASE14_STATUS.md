# Phase 14 — ECU workbench, comparison, and reporting

## Status

**Tested local scope complete. No automatic tuning, map writing, checksum
correction, ECU-file writing, or flashing was added.**

## Reviewed behavior

- Binary comparison reads bounded chunks, reports SHA-256, sizes, differing
  byte counts, and deterministic half-open changed ranges. It treats a
  different size as a byte difference and stops with an explicit error when
  the configured changed-range cap is exceeded.
- The ECU Workbench streams uploaded `.bin`/`.ori` bytes into a separate
  content-addressed store. Extensions identify only the raw input adapter;
  they do not prove the file format or decode ECU identity.
- Reference bytes are re-read and hash/size checked before archival.
  Stale, multiple, incomplete, or contradictory reference evidence is not
  promoted to a confirmed project/version association.
- Workbench comparisons are explicitly byte-level and return
  `semantic_status="INCONCLUSIVE"`. They do not decode map values, normalize
  addresses, correct checksums, or infer tuning intent.
- Existing reference-package workflows require reviewed project, version,
  export representation, and provenance evidence. Map comparisons require
  compatible address representations and reviewed parser ordinals; incomplete
  parser axes do not validate against a reference.

## Tests and verification

| Check | Result |
|---|---|
| `python -m unittest tests.test_compare tests.test_ecu_workbench tests.test_binary_reference_analysis tests.test_reference_package -v` | 39 passed, 0 failed |
| Python child exit code | `0` |
| PowerShell wrapper exit code | `0` |
| Comparison cases | Identical, changed same-size, changed-size, empty, chunk-boundary changes, multiple chunk sizes, bounded read sizes, invalid limits, and changed-range cap |
| Workbench/reference cases | Unsupported and incomplete input, exact/stale/ambiguous references, metadata conflicts, rehash before archival, append-only reanalysis, and byte-range reporting |
| Fixture/database scope | Synthetic temporary files and temporary SQLite databases only |

No sample BIN/ORI or OLS file was used as a comparison oracle. The tests
confirm implementation behavior, not ECU compatibility, map semantics, or
the meaning of parser-derived byte ranges.

## Limits

No independently sourced WinOLS export with verified project/version/map
relationships was available. Independent WinOLS validation and ECU
compatibility remain `NOT_TESTED`. No real customer binary was tested.
Streaming memory is bounded by the configured chunks plus the capped range
list; no large-corpus benchmark was run.
