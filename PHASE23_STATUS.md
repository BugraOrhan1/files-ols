# Phase 23 — Bounded streaming comparison experiment

## Status

**Small synthetic comparison checks passed.** No production binaries, OLS
fixtures, corpus paths, or database were involved.

## Method and observations

Two temporary 4 MiB files were generated. They had equal length and exactly
three known changed bytes in three separate ranges. The existing streaming
comparison returned the exact changed-byte/range counts at both tested
chunk sizes:

| Chunk size | Elapsed | Observed throughput | Result |
|---:|---:|---:|---|
| 64 KiB | 0.4492 s | 8.90 MiB/s | 3 changed bytes, 3 ranges |
| 1 MiB | 0.4340 s | 9.22 MiB/s | 3 changed bytes, 3 ranges |

These are local synthetic measurements, not throughput guarantees. The
existing regression `test_streaming_reads_never_request_more_than_chunk_size`
also verifies that read requests are bounded by the configured chunk size;
the full suite passed that test.

An exploratory 32 MiB-per-file, three-chunk-size run with `tracemalloc`
exceeded the 120-second command window and was stopped. It produced no
measurement and is not counted as a pass. Its two temporary files were
verified by name and size and removed. Instrumented per-byte comparison can
be substantially slower than the uninstrumented operation.

## Verification

- 4 MiB synthetic run: 2 chunk-size cases passed; Python child exit code
  `0`, PowerShell wrapper exit code `0`.
- Full suite before the Phase 22 addition: the streaming-bound regression
  passed as part of 125 tests (123 passed, 2 skipped, 0 failed).
- No process-level memory profile was obtained for a large binary. Bounded
  read sizes are established by the regression test and source inspection;
  no claim is made about arbitrarily large individual files or aggregate
  6-TB processing performance.

## Decision

Keep comparison as an explicitly user-invoked, read-only streaming operation.
Do not use these small-file timings to promise full-corpus performance or
semantic/ECU compatibility.
