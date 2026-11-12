# Phase 13 — Indexing, database, and search

## Status

**Tested local scope complete. No production index, queue, worker, or corpus
was accessed.**

Work continues on the isolated `phase12-16-release-prep` branch. Phase 12 is
checkpointed at `020ede5e04d2e1ef75c32644418b9472041a896a`.

## Reproduced defect and fix

Before the fix, project search applied SQL `LIMIT`/`OFFSET` using only
reference/map/processing status and descending project ID. It computed the
documented evidence rank afterward in Python and sorted only that page.
Consequently, a lower-scoring newer project could occupy page one while a
higher-scoring match was omitted, despite the response labeling the ranking
scope `whole_filtered_result_set`.

The defect was reproduced with two synthetic projects in a temporary SQLite
database and `metadata_match_mode="any"`: project ID 2, matching one of two
identifier filters, was returned at rank 11; project ID 1, matching both,
should rank higher but was not in the page. The implementation now adds the
default rank score to the SQL query, orders the entire filtered result set by
that score before applying pagination, and uses ascending project ID for
stable ties. The Python ranker ignores blank identifier filters, consistent
with their existing no-op filter behavior. No schema or migration changed.

## Regression and workflow coverage

The new test verifies that:

- the stronger of two `any`-mode identifier matches appears on the first
  page;
- the weaker match appears on the next page;
- returned scores follow the page order and the response's global-ranking
  scope;
- blank metadata filters do not add identifier rank points.

The existing isolated tests also cover idempotent imports and retries,
partial failures, missing/unavailable source roots, exact-content duplicate
locations, interrupted/resumed directory scans, bounded hash batches, worker
leases and pause/resume, queue recovery, and parser-fingerprint reprocessing.
Tests create temporary directories and SQLite files. Worker tests run only
against their temporary fixture database; none targets the known live worker
or queue.

## Verification and measurement

| Check | Result |
|---|---|
| `python -m unittest tests.test_sources_and_search tests.test_importer tests.test_inventory_and_export_queue -v` | 35 passed, 0 failed |
| Python child exit code | `0` |
| PowerShell wrapper exit code | `0` |
| Search reproduction before the fix | Returned lower-score project ID 2 (rank 11) ahead of the stronger ID 1 |
| Synthetic search measurement | 250 temporary projects; 25 matched both identifiers and 225 matched one; first 20 results were all strong matches |
| Search elapsed measurements | Five runs: 0.001429, 0.000906, 0.000784, 0.000758, 0.001208 seconds; median 0.000906 seconds |
| Synthetic import duration | 1.9569 seconds for 250 tiny synthetic files |
| Environment | Python 3.11.9, SQLite 3.45.1, Windows 10 build 26200 |

These are local measurements on a small synthetic dataset, not a 6-TB
performance forecast. No broad performance tuning was attempted.

## Limits

The search fix is covered by deterministic temporary-database tests and the
local synthetic measurement. Production-scale query plans, live queue state,
network-root availability, and end-to-end throughput remain unmeasured.
Independent WinOLS validation remains `NOT_TESTED`; indexing/search evidence
does not validate parser semantics, ECU identity, or tuning relationships.
