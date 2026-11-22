# Phase 17 — External repository review

## Scope and method

Eight public repositories were examined at the exact pinned commits below.
The source/code review was read-only; external executables were not run and
no source was copied into this application. GitHub source, repository trees,
license files, README files, and available issue/PR pages were used as
evidence. The public GitHub Issues API showed the items listed in this report
at review time; `gh issue list` could not run because this environment has no
GitHub CLI authentication, so public GitHub pages/API were used instead.

The review distinguishes implementation observations from independently
verified WinOLS semantics. None of the reviewed repositories supplied
independently attributable WinOLS exports and exact project/version/map
relationships for this application's samples.

## Candidate comparison

| Repository and pinned revision | License | What it actually does | Review decision |
|---|---|---|---|
| [jinglinsen/ols-parser](https://github.com/jinglinsen/ols-parser/tree/c77616e8e4dbe8b4fd6a41828c6c3d63b22cf2c6) — `c77616e8e4dbe8b4fd6a41828c6c3d63b22cf2c6` | 0BSD ([LICENSE](https://github.com/jinglinsen/ols-parser/blob/c77616e8e4dbe8b4fd6a41828c6c3d63b22cf2c6/LICENSE)) | C99 parser for headers, metadata, version/segment records, maps, and axes. Uses bounds-checked cursor reads, but heuristically searches for map data only through a 64-KiB window, has reverse-engineered format thresholds, reads the full file in the CLI, and can assemble large ROM spans. Structural anomalies may become warnings or silently omitted records. | Best alternative for a bounded experiment; not an authority. No upstream tests, CI, or sample corpus were found at this revision. Do not integrate until differential tests and memory limits pass. |
| [ArendJanKramer/WinOls-.bin-extractor-from-.ols](https://github.com/ArendJanKramer/WinOls-.bin-extractor-from-.ols/tree/1ba792b32f92ed51be96f9adf915afca23c792c1) — `1ba792b32f92ed51be96f9adf915afca23c792c1` | Apache-2.0 ([LICENSE](https://github.com/ArendJanKramer/WinOls-.bin-extractor-from-.ols/blob/1ba792b32f92ed51be96f9adf915afca23c792c1/LICENSE)) | Rust CLI that searches for `(Windows)\0` and separator patterns, infers one ECU-size value, and copies a trailing binary. It does not parse project metadata, map definitions, or all versions; the release profile aborts on panic. | Not a general parser or safe fallback. Do not adopt as the project extractor. Rust toolchain was unavailable, so no build/run was claimed. |
| [v-arapidis/openremap-core](https://github.com/v-arapidis/openremap-core/tree/6c6ba8526b9f8040916b3a617035a085eabfccab) — `6c6ba8526b9f8040916b3a617035a085eabfccab` | MIT in root `LICENSE`; `THIRD_PARTY.md` lists GPL/GPLv2-derived checksum sources and asserts clean-room reimplementation. | Broad offline ECU analysis/tuning project with parser, checksum, decoder, CLI/TUI, and write/apply functionality. The clean-room license claim for checksum modules was not independently audited against upstream code. | No code reuse or execution. Its scope includes writing/checksum behavior excluded from this read-only indexer; a provenance audit and legal review would be needed before any checksum-specific consideration. |
| [NXT-Tronic/winols-mcp](https://github.com/NXT-Tronic/winols-mcp/tree/7bd849df923396b801f884b4c8c190c69422a75d) — `7bd849df923396b801f884b4c8c190c69422a75d` | MIT ([LICENSE](https://github.com/NXT-Tronic/winols-mcp/blob/7bd849df923396b801f884b4c8c190c69422a75d/LICENSE)) | MCP server queues JSON tickets for a Lua bridge using WinOLS External Control. README/server explicitly say it has not been tested with a live WinOLS instance. The catalog includes high-risk project write and outbound HTTP tools. | Not adopted or run. Live WinOLS automation and write capabilities are outside scope; its own validation disclaimer is a release blocker for relying on its behavior. |
| [2CRPerformance/LinOLS](https://github.com/2CRPerformance/LinOLS/tree/2bc16a79b200f0982dd8dc9064b6bb48566edd5f) — `2bc16a79b200f0982dd8dc9064b6bb48566edd5f` | GPL-3.0 ([LICENSE](https://github.com/2CRPerformance/LinOLS/blob/2bc16a79b200f0982dd8dc9064b6bb48566edd5f/LICENSE)) | Tk/customtkinter binary editor. Its difference view compares positional values in equal-length arrays and synchronizes a list with text/canvas navigation; it is UI-coupled and not a general binary-diff engine. | No code reuse due GPL distribution implications and unrelated editing scope. General interaction ideas only; no implementation copied. |
| [dallashudson/tunecomparison](https://github.com/dallashudson/tunecomparison/tree/a4f223876a12f5634c9f16161953026fa5af3e9a) — `a4f223876a12f5634c9f16161953026fa5af3e9a` | No license file or source-license grant found. | PyQt workflow compares pre-exported WinOLS text differences; source is incomplete and its comparison routine is a nested-loop list comparison. | No code reuse absent explicit permission. Not a binary parser or validated comparison engine. |
| [reproteq/phpOLS](https://github.com/reproteq/phpOLS/tree/e8d57be71c64cd3ab3259d14f3b78bf9b83a8512) — `e8d57be71c64cd3ab3259d14f3b78bf9b83a8512` | MIT ([LICENSE](https://github.com/reproteq/phpOLS/blob/e8d57be71c64cd3ab3259d14f3b78bf9b83a8512/LICENSE)) | Reads a WinOLS-exported DAMOS CSV plus a raw BIN; it does not parse native OLS/KP files. Its binary handler opens in `r+b` and exposes direct writes without bounds/checksum/atomicity safeguards. | Not a native project reader and not safe for this read-only product. No code reused. |
| [liushengjia-AAL/shengjia](https://github.com/liushengjia-AAL/shengjia/tree/cdcbe28ca5f977a1688c81b9467801d87a7b5431) — `cdcbe28ca5f977a1688c81b9467801d87a7b5431` | No repository or parser-skill license found. | Repository is an AI calibration-skills collection. `winols-ols-parser` scans length-prefixed strings and guessed address windows; the skill says compressed bodies are unsupported and map names/scaling need independent confirmation. | No code reuse absent a license. Heuristic outputs are not suitable as authoritative map/version evidence. |

### Source evidence

The following links point to the reviewed source locations. Line references
are pinned to immutable commits so future upstream edits do not silently
change the evidence.

- `ols-parser`: [header/schema structs](https://github.com/jinglinsen/ols-parser/blob/c77616e8e4dbe8b4fd6a41828c6c3d63b22cf2c6/ols.h#L1-L110), [format detection and metadata gates](https://github.com/jinglinsen/ols-parser/blob/c77616e8e4dbe8b4fd6a41828c6c3d63b22cf2c6/ols.c#L1117-L1135), [map-region search](https://github.com/jinglinsen/ols-parser/blob/c77616e8e4dbe8b4fd6a41828c6c3d63b22cf2c6/ols.c#L1039-L1062), [format notes](https://github.com/jinglinsen/ols-parser/blob/c77616e8e4dbe8b4fd6a41828c6c3d63b22cf2c6/README.md).
- Rust extractor: [pattern scan and extraction](https://github.com/ArendJanKramer/WinOls-.bin-extractor-from-.ols/blob/1ba792b32f92ed51be96f9adf915afca23c792c1/src/main.rs#L17-L120), [release profile](https://github.com/ArendJanKramer/WinOls-.bin-extractor-from-.ols/blob/1ba792b32f92ed51be96f9adf915afca23c792c1/Cargo.toml#L1-L13).
- OpenRemap: [third-party provenance disclosures](https://github.com/v-arapidis/openremap-core/blob/6c6ba8526b9f8040916b3a617035a085eabfccab/THIRD_PARTY.md#L1-L34), [license](https://github.com/v-arapidis/openremap-core/blob/6c6ba8526b9f8040916b3a617035a085eabfccab/LICENSE).
- WinOLS MCP: [untested-live-instance disclaimer](https://github.com/NXT-Tronic/winols-mcp/blob/7bd849df923396b801f884b4c8c190c69422a75d/README.md), [server disclaimer and tool handling](https://github.com/NXT-Tronic/winols-mcp/blob/7bd849df923396b801f884b4c8c190c69422a75d/server/winols_mcp_server.py#L1-L80).
- LinOLS: [difference visualization](https://github.com/2CRPerformance/LinOLS/blob/2bc16a79b200f0982dd8dc9064b6bb48566edd5f/difference_dialog.py#L1-L71), [positional value comparison](https://github.com/2CRPerformance/LinOLS/blob/2bc16a79b200f0982dd8dc9064b6bb48566edd5f/Utilities.py#L70-L125).
- tunecomparison: [comparison routine](https://github.com/dallashudson/tunecomparison/blob/a4f223876a12f5634c9f16161953026fa5af3e9a/mod/comparison.py#L1-L46), [GUI stub](https://github.com/dallashudson/tunecomparison/blob/a4f223876a12f5634c9f16161953026fa5af3e9a/gui/main_widget.py#L1-L120).
- phpOLS: [DAMOS CSV parser](https://github.com/reproteq/phpOLS/blob/e8d57be71c64cd3ab3259d14f3b78bf9b83a8512/src/Service/Damos.php#L26-L41), [read/write file access](https://github.com/reproteq/phpOLS/blob/e8d57be71c64cd3ab3259d14f3b78bf9b83a8512/src/Service/BinaryFileHandler.php#L12-L38), [license](https://github.com/reproteq/phpOLS/blob/e8d57be71c64cd3ab3259d14f3b78bf9b83a8512/LICENSE).
- shengjia: [parser methodology and limitations](https://github.com/liushengjia-AAL/shengjia/blob/cdcbe28ca5f977a1688c81b9467801d87a7b5431/winols-ols-parser/SKILL.md), [heuristic parser source](https://github.com/liushengjia-AAL/shengjia/blob/cdcbe28ca5f977a1688c81b9467801d87a7b5431/winols-ols-parser/scripts/parse_ols.py#L33-L150), [repository purpose](https://github.com/liushengjia-AAL/shengjia/blob/cdcbe28ca5f977a1688c81b9467801d87a7b5431/README.txt#L1-L34).

## Upstream issues and pull requests

This is a point-in-time check, not an endorsement of issue-free software.

- **TheFlashBold/py-ols** (existing parser dependency; inspected commit
  `4c283a80161e130b03eb8743c752a689ecda6b78`) had two open items when
  checked: [#1 “Object Type Error”](https://github.com/TheFlashBold/py-ols/issues/1)
  reports a `TypeError` when the CLI calls `hex()` on a floating-point
  `AxisInfo.offset`; [#2 “dependencies ?”](https://github.com/TheFlashBold/py-ols/issues/2)
  was listed open, but its body was not available in the unauthenticated page
  response. The float-offset issue concerns the external CLI display path;
  this application's adapter stores integer `axis.address` separately from
  floating-point `axis.offset`. A regression test covers that boundary; it
  does not alter the external parser.
- `jinglinsen/ols-parser`: one open PR, [#1](https://github.com/jinglinsen/ols-parser/pull/1),
  proposing automation output, portability, and build checks; not merged at
  the reviewed base SHA. It adds engineering checks rather than changing the
  parser core, according to the PR description.
- `2CRPerformance/LinOLS`: one open PR,
  [#1](https://github.com/2CRPerformance/LinOLS/pull/1), adding a Spanish
  README; unrelated to parser correctness.
- `liushengjia-AAL/shengjia`: open
  [#1 “thankyou”](https://github.com/liushengjia-AAL/shengjia/issues/1);
  not a parser defect report.
- No open issues were returned for `openremap-core`, the Rust extractor,
  `winols-mcp`, `tunecomparison`, or `phpOLS` at check time.

## Existing parser issue regression

The application adapter already normalizes axis values at the boundary:
`address` is an integer location, while `offset` is a floating-point
conversion value. The new regression test supplies an axis with address
`0x200` and offset `1.25` and verifies both are retained independently. The
adapter does not call `hex()` on the conversion offset. The upstream issue is
still recorded as open; no claim is made that this fixes the external
repository or reproduces its attached OLS file.

## Overall decision

No candidate is being made the primary parser by this review. A later
isolated experiment may evaluate the 0BSD C parser against the protected
samples, but the absence of upstream tests and independent WinOLS references
must remain explicit. Third-party license observations are not legal advice;
GPL distribution obligations and the provenance of any derived algorithm
require legal review before product distribution.
