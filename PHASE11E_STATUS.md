# Phase 11E — Independent OLS parser and binary evidence audit

## Status and scope

**Scoped result: complete for the experiments documented below. Phase 11 as a
whole remains in progress. Independent WinOLS validation is `NOT_TESTED`.**

This audit compares parser observations; it does not designate either parser
as ground truth. No WinOLS exports, map-list exports, or independently
confirmed project/version relations were available. No parser agreement,
hash, address, filename, or ECU metadata was promoted to project identity,
version relationship, tune relationship, or ECU compatibility evidence.

The work was performed on the repository's `main` branch at starting HEAD
`4ae04f1d2c4b54f866ddfd46486f878c8a299a3b`. The worktree already contained
uncommitted changes from the preceding offline reference-audit task. Those
changes were preserved. No commit or push was made.

Evidence labels used here:

- **Observed** — a parser or repository returned/reported the value.
- **Measured** — computed from captured bytes or parser output (for example
  SHA-256, byte count, or an in-bounds arithmetic check).
- **Hypothesis** — a plausible interpretation not confirmed by WinOLS.
- **Unknown** — the available sources do not establish the fact.
- **Not tested** — the required independent reference or experiment was not
  available or was intentionally not run.

## Method

1. Reviewed the application adapter, real-sample tests, diagnostics,
   reference-package gates, comparison/index code, and the project status and
   integration documents.
2. Read-only parsed the eight tracked OLS samples with `py-ols` through the
   application's `PyOlsReader` adapter. Wrote the fresh JSON diagnostics to a
   temporary directory, not into the project or sample directory.
3. Compared those observations with the previously captured read-only
   `ols_dump` runs from the separately checked-out C parser. The source sample
   SHA-256 and size recorded by both runs agree. The C parser's captured run
   exited successfully for all eight files and emitted no warnings.
4. Reviewed source, README, license, test coverage, and build/runtime
   requirements in all eight requested public repositories. No third-party
   code was copied or vendored.
5. Added deterministic adapter regressions and a machine-readable record of
   the real parser observations. Synthetic fixtures are used only for
   structural edge cases; they are not evidence of OLS correctness.

The detailed, machine-readable observation record is
[reports/phase11e_parser_audit.json](./reports/phase11e_parser_audit.json).
It includes source hashes and sizes, parser identity/revision/license,
metadata field states, map/axis structures, all parser-A slot offsets/sizes
and payload SHA-256 values, parser-B version/segment offsets and framing
values, reported 32-bit segment hashes, and per-sample differences. It omits
local absolute paths and raw version-description text.

## Parser versions, licenses, and experiment

| Label | Repository / pinned revision | License | Experiment and limits |
|---|---|---|---|
| Parser A | [TheFlashBold/py-ols](https://github.com/TheFlashBold/py-ols), `4c283a80161e130b03eb8743c752a689ecda6b78` | GPL-3.0 | Ran through the application's `OLSReader` adapter on all eight samples. The adapter fingerprint is recorded in the JSON report. The parser reads the whole source into memory. The sample set exercises only formats 503 and 597. |
| Parser B | [jinglinsen/ols-parser](https://github.com/jinglinsen/ols-parser), `c77616e8e4dbe8b4fd6a41828c6c3d63b22cf2c6` | 0BSD | A separately obtained C99 build was run read-only on all eight samples. It reads the whole file, reports segment layouts and 32-bit stored hashes, and allocates a ROM span when assembling versions. It does not expose a SHA-256 of extracted payloads. No upstream test directory/suite was found. |
| Extractor candidate | [ArendJanKramer/WinOls-.bin-extractor-from-.ols](https://github.com/ArendJanKramer/WinOls-.bin-extractor-from-.ols), `1ba792b32f92ed51be96f9adf915afca23c792c1` | Apache-2.0 | Source audit only. `rustc` and `cargo` are unavailable, so it was not built or run. It reads the entire file, searches for `(Windows)\0` and separator patterns to infer an ECU size, then selects trailing bytes. It does not validate OLS format, segment/version boundaries, markers, or checksums; `File::create` can overwrite an output. |

### Parser A/B sample observations

Counts are parser outputs, not validated project inventories. “A named /
slots” distinguishes the one named `BinaryVersion` record from the adapter's
physical slot enumeration. “B versions / segments” is the C parser's reported
version and segment count; those concepts are not assumed to be equivalent.
The exact SHA-256 hashes, offsets, version records, slots, and segments are in
the JSON report.

| Sample | Format | A named / slots | A maps / axes | A missing metadata values | B versions / segments | B maps |
|---|---:|---:|---:|---|---:|---:|
| `100004.ols` | 597 | 1 / 4 | 0 / 0 | none | 1 / 4 | 0 |
| `100007.ols` | 597 | 1 / 4 | 0 / 0 | gearbox, ECU type, SW and HW number | 1 / 4 | 0 |
| `100008.ols` | 597 | 1 / 3 | 0 / 0 | make, model, engine, year, fuel, displacement, power, gearbox, SW/HW number, HW code | 1 / 3 | 0 |
| `100009.ols` | 597 | 1 / 2 | 4 / 8 | year, gearbox | 1 / 2 | 0 |
| `100010.ols` | 597 | 1 / 3 | 0 / 0 | none | 1 / 3 | 0 |
| `10001004.ols` | 503 | 1 / 3 | 0 / 0 | none | 1 / 3 | 0 |
| `10001005.ols` | 503 | 1 / 3 | 0 / 0 | HW number | 3 / 24 | 0 |
| `100010054.ols` | 503 | 1 / 4 | 12 / 23 | none | 4 / 32 | 0 |

Parser A returned eight named records total, each named `Original`, while the
adapter enumerated 26 slots; 18 slot names remain unresolved. All 26 reads
were non-empty and complete according to the adapter, but are marked
`present_nonempty_unverified`. All named records point to one slot per sample;
the other slot names and relationships are not established. Six of the eight
returned descriptions contain import-history text; two are empty. Description
text is not a WinOLS-confirmed version description.

Parser A returned 16 map definitions in two files: four `MAP` records in
`100009.ols` and eleven `MAP` plus one `CURVE` record in `100010054.ols`. All
16 map names are empty. Their dimensions, data types, offsets, factors,
conversion offsets, and axis structures are preserved in the JSON report.
All eight axes in `100009.ols` and all 23 axes in `100010054.ols` lack at least
one of unit/identifier. Parser-positive dimensions are marked `NOT_TESTED`
for full structural correctness: they do not prove data extents, encoding,
map values, or version association. The six cases where both parsers reported
zero maps do not prove those projects contain no maps.

All 15 expected metadata attributes were present on the Parser A parser
object; 18 field values were empty across the eight samples. “Present” and
“empty” describe the parser object only and do not establish that the source
project contains or lacks a field.

### Differences and bounds

- The parsers disagree on returned map-definition count for `100009.ols`
  (Parser A 4, Parser B 0) and `100010054.ols` (Parser A 12, Parser B 0).
  There is no independent evidence to decide which output is complete or
  correct. The captured report test locks in the disagreement without
  assigning a winner.
- Parser A reports one named record per file; Parser B reports three records
  for `10001005.ols` and four for `100010054.ols`. Parser A's named records,
  Parser A's enumerated slots, and Parser B's assembled version records are
  different parser concepts. Their counts are explicitly `NOT_COMPARABLE`.
- Parser A's extracted post-marker slot payload sizes are four bytes smaller
  than each recorded blob size. The JSON includes every source offset, marker,
  payload size, and payload SHA-256. These hashes identify parser-produced
  bytes only; they are not WinOLS export matches.
- Parser B reports 32-bit segment hashes, source offsets, framing lengths,
  logical flash addresses, and sizes. Its source computes payload start as
  `data_start + framing_bytes`, and subtracts framing bytes from the logical
  segment size before assembly. The reported segment extents are within the
  source file for this sample set. This arithmetic check is not a validation
  of the segment interpretation or assembled binary.
- For `10001005.ols`, Parser B reports 70,778,800 bytes per assembled ROM
  versus a 26,187,364-byte OLS source. Source inspection shows that this is a
  logical flash span assembled from segment address ranges, not the source
  file length. It is not an exported binary-size comparison and is not
  independent evidence.
- Neither parser run measured elapsed time or peak memory. Source inspection
  confirms whole-file input allocation in both candidates; Parser B also
  allocates the assembled logical ROM span. No memory optimization was
  attempted because output correctness is still unproven.
- All eight files were recognized by both runs with the same format number
  per file. Only formats 503 and 597 were observed; other format generations
  remain `NOT_TESTED`.

## Related repository review

Pinned revisions below are the checked-out commits inspected for this audit.
The review is architectural/source research; these projects were not treated
as validated OLS decoders or ECU references.

| Repository / revision | License finding | Relevant finding |
|---|---|---|
| [v-arapidis/openremap-core](https://github.com/v-arapidis/openremap-core), `6c6ba8526b9f8040916b3a617035a085eabfccab` | MIT | Raw binary analysis and map/diff workflows, not OLS project parsing. Its tests and explicit unknown/skip and changed-region reporting are useful design references. Heuristic map matching does not validate OLS project/version relations or compatibility. No apply engine or tune-generation logic was adopted. |
| [dallashudson/tunecomparison](https://github.com/dallashudson/tunecomparison), `a4f223876a12f5634c9f16161953026fa5af3e9a` | No root license found | Consumes manually copied WinOLS difference text; no test suite found. `mod/comparison.py` appends every tune-1 entry to the tune-1-only list even if it also found a match, so shared entries can be misreported. Not used. |
| [2CRPerformance/LinOLS](https://github.com/2CRPerformance/LinOLS), `2bc16a79b200f0982dd8dc9064b6bb48566edd5f` | GPL-3.0 | A raw-binary mapping/editor application, not an OLS project parser. No upstream test directory was found. No code was reused. |
| [reproteq/phpOLS](https://github.com/reproteq/phpOLS), `e8d57be71c64cd3ab3259d14f3b78bf9b83a8512` | MIT | Consumes a WinOLS-exported map-list CSV and a binary; it does not read OLS projects. Its binary access includes read/write (`r+b`) behavior, so it is not used in the read-only audit. No upstream test suite was found. |
| [NXT-Tronic/winols-mcp](https://github.com/NXT-Tronic/winols-mcp), `7bd849df923396b801f884b4c8c190c69422a75d` | MIT | Bridge based on WinOLS's official External Control Lua plugin/manual. Its README says it has not been run against live WinOLS. It was not run or used to automate WinOLS. |
| [liushengjia-AAL/shengjia](https://github.com/liushengjia-AAL/shengjia), `cdcbe28ca5f977a1688c81b9467801d87a7b5431` | No root license found | Contains a nested heuristic OLS parser. Its documentation describes compressed projects as unsupported and address assumptions as validated for a limited JLR case. It was not run; output was not treated as independent evidence. |

No third-party implementation was copied. Review each repository's current
license and attribution requirements and obtain legal advice before shipping
any dependency or derived work. In particular, both py-ols and LinOLS are
GPL-3.0; this audit does not determine the licensing consequences of
distribution with this application.

## Application changes and regression coverage

- `PyOlsReader` now compares the parser buffer length with the actual source
  size and uses the source size for slot bounds. It records out-of-bounds
  slots, invalid named-slot indexes, short reads, and actual bytes read rather
  than relying on parser-declared size alone.
- Structural diagnostics report negative/zero/impossible map dimensions,
  negative map offsets, and negative or impossible axis bounds. A positive
  shape does not imply a valid offset, encoding, or map-value decode.
- The sample report labels observed format IDs as sample-set evidence, not
  general parser support. It records per-map structural status and keeps
  independent reference validation at `NOT_TESTED`.
- Synthetic regressions cover malformed headers, source-size mismatch,
  invalid slot bounds, truncated slot reads, invalid/zero/oversized map
  dimensions and offsets, unknown format IDs, incomplete axis metadata, and
  partial map-record output. A separate report test checks the captured
  parser disagreement, candidate segment extents, and non-comparable version
  counts.
- The existing canonical reference-package validation remains the only path
  to independently reviewed WinOLS comparisons. No second reference system
  was added.

## Toolchain and test results

Available: Python 3.11, Node.js, GCC, and Git. Unavailable: `rustc` and
`cargo`, which blocked a safe isolated build of the Rust extractor. GitHub CLI
was present but not authenticated, so public repository clones and their
checked-out source were used for this review.

Full-suite verification exposed a Windows-specific worker-lease bug: the
existing active-worker check used `os.kill(pid, 0)`, which terminated the
temporary test process when it checked its own PID. The check now uses
`OpenProcess` and a zero-timeout `WaitForSingleObject` on Windows, while
retaining the non-signalling `os.kill(pid, 0)` probe on POSIX. The existing
pause/resume and single-lease regression test now completes; no live worker
PID was probed or controlled during this verification.

| Check | Result |
|---|---|
| Focused adapter diagnostics tests | 8 passed, 0 failed |
| Captured parser-audit report regression tests | 4 passed, 0 failed |
| Importer tests using temporary database fixtures | 5 passed, 0 failed |
| Source/search module, including the worker lease regression | 25 passed, 0 failed |
| Full test suite | 105 run: 103 passed, 0 failed, 2 skipped |
| Full-suite Python child exit / PowerShell wrapper exit | `0` / `0` |
| Real OLS parser integration tests with `PY_OLS_PATH` | 2 passed, 0 failed |
| Real-sample Python child exit / PowerShell wrapper exit | `0` / `0` |
| Eight sample hashes before / after real-sample integration | Identical; all eight |
| `python -m compileall -q ols_platform deployment tests` | Exit `0` |
| `node --check ols_platform\dashboard.js` | Exit `0` |
| `git diff --check` | Exit `0` |

The two tests skipped in the full-suite invocation require `PY_OLS_PATH`; both
were run separately with the external parser configured and passed. Their
temporary database imported all eight samples, and the per-sample SHA-256 and
size measurements matched before and after the integration run.

No live SQLite index, queue, 6-TB corpus, or reported hashworker was opened,
changed, or controlled. Tests use temporary databases. The eight tracked OLS
files were read-only and remained hash-identical through the diagnostic and
targeted integration runs. No BIN/ORI, backup, database, or tuning outputs
were added or changed.

## Remaining blockers and prioritized next steps

1. Obtain attributable, independent WinOLS exports for the exact OLS hashes:
   project metadata, map list with order/index and address representation,
   map/axis details where applicable, and a raw export for every version
   actually confirmed in WinOLS. Record WinOLS version, export method, exact
   project/version relationships, and reviewer evidence in the existing
   canonical reference-package manifest.
2. Run the existing reference-package inspection and field-level comparison
   gates against those real exports. Keep absent or ambiguous fields
   `NOT_TESTED`, `NOT_COMPARABLE`, or `INCONCLUSIVE`; do not infer missing
   WinOLS versions from either parser.
3. Independently validate the Parser A/B slot and segment byte ranges,
   marker/framing treatment, payload size, checksums, and hashes against those
   exports. Do not store candidate C-parser assembly as validated binary data
   without this comparison.
4. Once reference evidence exists, evaluate supported OLS format coverage and
   resource use on representative files. Keep the optional parser boundary;
   do not introduce a mandatory native compiler.
5. Review the GPL-3.0 distribution implications with counsel before product
   distribution.

Phase 11 remains **in progress** until parser structure and independent
WinOLS validation are resolved. No automatic tuning generation or tune
application was started.
