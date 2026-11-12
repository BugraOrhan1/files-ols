# WinOLS project indexer

This is a new, local-first application in this workspace. The first release
provides a Python CLI and local dashboard, a SQLite project index, a
parser adapter for a separately installed/cloned `py-ols`, and a read-only
streaming binary comparison command. It does not generate or write ECU tunes.

## Requirements

- Python 3.11 or newer.
- A separately obtained `py-ols` checkout to parse `.ols` project files.
- The parser is GPL-3.0. Review licensing implications before distributing an
  application that depends on it. The adapter keeps the parser outside this
  source tree, but separation alone does not determine legal obligations.

Python was selected for the initial indexing engine because Python 3.11 is
available in this environment, while the .NET SDK needed to build a new WPF
application is not installed. The dashboard is an interim local interface,
not the requested WPF desktop client. The SQLite schema is intentionally local
and small; PostgreSQL, DuckDB/Parquet, and a WPF shell remain future
scaling/UI work, not current capabilities. Re-indexing is invalidated when the
source content, parser fingerprint or application version changes.

## Commands

Initialize the database:

```powershell
python -m ols_platform.cli init-db
```

Index one OLS file or recursively scan one or more source folders. Files are
processed serially; imports are idempotent by absolute path, content hash,
size, and modification time. Re-running the command resumes by skipping
unchanged inputs. Source content, parser fingerprint and application version
must all match for an input to be skipped. Use `--max-file-bytes` to reject
large files before parsing.

```powershell
python -m ols_platform.cli import `
  --py-ols-path C:\path\to\py-ols `
  "C:\path\to\one.ols" "D:\WinOLS-projects"
```

Dry-run a filesystem inventory without changing the source files or starting
OLS parsing. The scanner records file metadata in a temporary on-disk SQLite
database, compares paths against the current index, and reports missing paths,
same-size duplicate candidates, inaccessible entries, and aggregate size.
By default, filesystem size/time matches are explicitly **not** treated as
content proof and no full hashes are computed.

```powershell
python -m ols_platform.cli inventory `
  "D:\WinOLS-projects" `
  --file-list data\inventory-reports\files.jsonl `
  --report data\inventory-reports\summary.json
```

Add `--hash-duplicate-candidates` to hash only files in repeated-size groups
and confirm exact duplicates. `--verify-indexed-hashes` reads and hashes every
indexed OLS file under the selected roots; this can require reading terabytes
and is deliberately opt-in. The output separates path discovery, stat
comparison, full hashing, parser analysis, and independent reference checks.
Do not pass the full 6-TB corpus until its configured roots and storage
availability have been confirmed. Each summary is retained in the append-only
inventory-run history and shown on the local dashboard; the per-file JSONL
remains an optional separate report.

Generate or list the minimal WinOLS export tasks that address known gaps:

```powershell
python -m ols_platform.cli export-queue
python -m ols_platform.cli export-queue --list-only
python -m ols_platform.cli export-batch
```

Task generation is idempotent for a source hash and request. Existing binary
exports are not requested again merely because their version relationship is
unclear; the queue requests provenance for those cases. The queue is visible
on the local Importdiagnose page. `export-batch` writes a manual JSON
instruction batch and checklist; it does not operate WinOLS or create exports.
Record attempts with valid transitions, for example:

```powershell
python -m ols_platform.cli export-task-update 15 in_progress "Started in WinOLS"
python -m ols_platform.cli export-task-update 15 completed "Packet received"
python -m ols_platform.cli export-task-history 15
```

Compare or search for evidence-ranked indexed projects:

```powershell
python -m ols_platform.cli compare-projects 1 2
python -m ols_platform.cli find-projects --project-id 1
```

`CONFIRMED` means only that complete OLS source files have identical hashes.
Shared parser-derived payload hashes and ECU metadata are reported as
`LIKELY` or `INCONCLUSIVE`; they do not prove a tuning relationship or allow
training data to be marked reliable.

Inspect index counts:

```powershell
python -m ols_platform.cli status
```

Start the browser dashboard; it binds to loopback only:

```powershell
python -m ols_platform.cli serve
```

Open `http://127.0.0.1:8765/`. The dashboard exposes indexed metadata on this
computer without uploading source files or serving raw ECU binaries.

List indexed projects or search vehicle/ECU identifiers:

```powershell
python -m ols_platform.cli projects --query SIMOS18
```

Show one project's stored metadata, version inventory, map definitions and
axes (without decoding or modifying map/binary values):

```powershell
python -m ols_platform.cli show-project 1
```

Show the dedicated import-quality report for one indexed project:

```powershell
python -m ols_platform.cli diagnose 1
```

The browser dashboard also includes an **Importdiagnose** page with filters for
partial imports, parser errors and projects that still need independent
reference validation. A failed retry of a changed source is surfaced separately
from the last successfully stored snapshot; the report includes both hashes so
stale indexed data is not mistaken for the failed attempt's contents.

Compare two raw binary files without modifying them. The program streams data
in bounded chunks and stores hashes, differing-byte count and changed ranges
in the local index. This is a byte comparison only; it does not identify maps
or establish that two files are compatible tune/original pairs.

```powershell
python -m ols_platform.cli compare original.bin tuned.bin
```

For WinOLS reference manifests, first review the generated per-project
manifest, then import it and run validation. Binary exports without an explicit
slot mapping can produce candidate matches, but these remain inconclusive if
their representation is unknown.

```powershell
python -m ols_platform.cli reference-import data\reference-manifests\1\manifest.json
python -m ols_platform.cli reference-validate --project-id 1
python -m ols_platform.cli reference-report 1 --output data\reference-reports\1.json
python -m ols_platform.cli reference-summary --output data\reference-reports\summary.json
```

## Reference packet and version matching

For project `100010054.ols` (indexed project ID 8), create a separate packet
directory. It contains a manifest template and an operator checklist, but no
fabricated exports. The original OLS stays at its indexed source path and is
only referenced by path, size, and SHA-256.

```powershell
python -m ols_platform.cli reference-package-template 8
python -m ols_platform.cli reference-package-inspect data\reference-packets\8\manifest.json
```

Place each actual WinOLS raw version export separately in
`data\reference-packets\8\exports\versions\`, the WinOLS map-list CSV/JSON in
`exports\maps\`, project metadata JSON and any additional map details/value
exports in `exports\additional\`. Project metadata JSON uses
`{"metadata": {"field": "value"}}`. In the manifest, list only versions
actually exported. Record the WinOLS project identity and relation to the
exact source OLS hash. For each artifact,
set `artifact_source` to `winols_export` or `winols_manual_observation`, and
record its size, SHA-256, and any project/version/slot relationship. Parser
output is never accepted as an independent artifact source. The manifest-level
WinOLS version, export method, and settings apply to an artifact only when it
has no explicit override. A filename or hash by itself
does not prove export origin or meaning. Record the reviewer, date, method,
evidence, and exact fields checked in each `manual_review` object; leave
unknowns and missing evidence explicit. Do not strip or normalize any bytes.

```powershell
python -m ols_platform.cli reference-package-inspect data\reference-packets\8\manifest.json
python -m ols_platform.cli reference-package-import data\reference-packets\8\manifest.json
python -m ols_platform.cli reference-version-match data\reference-packets\8\manifest.json --output data\reference-reports\project-8-version-match.json
python -m ols_platform.cli reference-metadata-compare data\reference-packets\8\manifest.json --output data\reference-reports\project-8-metadata-compare.json
python -m ols_platform.cli reference-map-compare data\reference-packets\8\manifest.json --output data\reference-reports\project-8-map-compare.json
python -m ols_platform.cli reference-package-report 8 --output data\reference-reports\project-8-package.json
```

Version comparisons stream the export against each source slot, post-marker
payload, and contiguous boundary range. They report full-length equality,
exact partial prefixes, size differences, and changed byte ranges separately.
A prefix or boundary-spanning match is never treated as a full slot match.
An exact binary match is only a byte comparison. A version match is `PASS`
only when the exact OLS source relation, WinOLS project identity, version name,
version-to-slot relation, export-to-version relation, representation, artifact
origin, reviewer evidence, complete version coverage, and full bytes are all
confirmed. Package inspection's `status` reports packet integrity and
completeness; `version_evidence_status` separately reports whether the manual
provenance is adequate for version matching; it is not itself a successful
byte match or a validated ECU-compatibility result.

Metadata and map comparisons emit per-field `PASS`, `FAIL`, or
`NOT_COMPARABLE`. They compare only independently reviewed fields. Project
metadata JSON must use `{"metadata": {"field": "value"}}`. Map rows must carry a
`parser_ordinal` that a reviewer explicitly confirmed; a name alone is not a
join key. Addresses require matching declared representations. Incomplete
metadata, axes, map inventory, project/version relations, or export provenance
cannot produce an overall `PASS`. Map metadata does not validate numeric map
values in a binary. All package inspection, matching, and comparison runs are
append-only. The loopback-only Importdiagnose page includes packet inspection,
parser data, latest package runs, and missing references.
Until a real packet is supplied, project 8 remains `NOT_TESTED`; synthetic
unit fixtures are not WinOLS validation evidence.

The root `project_id` in a packet is the local index ID, not a WinOLS ID.
Record the WinOLS project identity separately in `project_identity`; keep the
OLS-to-project and project-to-version relationships in their respective
evidence fields. `reference-package-template` also creates a `CHECKLIST.txt`
for the producing WinOLS version, export settings, reviewer, evidence, and
remaining gaps. Unknown values stay unknown. A manifest and matching file
hashes establish packet integrity, not export origin, project identity, or
export meaning.

The database defaults to `data\ols-index.sqlite3`; override it with the global
`--database` option before the subcommand, e.g.
`python -m ols_platform.cli --database D:\index.sqlite3 import ...`.
Original source files are never copied or modified.

## Data quality and known limitations

Each import records parser warnings/errors, raw structure counts, stored-record
counts, fingerprints for each enumerated binary slot, and metadata for parser
records it skips. Physical non-empty slot payloads are **not** treated as
independently verified named project versions. The current samples contain 26
enumerated slot payloads but only 8 named version records; the other 18 slots
are marked unresolved rather than assumed missing, empty or reserved. A
project with parser warnings is partial. Parsed map definitions are not bound
to each binary version and are marked unverified; binary extraction is not
enabled as trusted named versions. The supplied binary exports now provide
byte-exact range evidence, but version labels, marker/checksum semantics, and
map-to-version relationships still need independent WinOLS validation.

`py-ols` currently reads the entire input file into memory. This application
processes one project at a time and defaults to rejecting inputs over 256 MiB,
but that is a configurable safety cap, not proof that py-ols is suitable for
unbounded files or a 6-TB corpus. The 6-TB corpus is not loaded into memory;
directory traversal is incremental and the SQLite database persists progress.

The user has supplied ten binary exports for three projects and confirmed the
project identities: `100004.ols` is VW Golf, `100007.ols` is Peugeot 307, and
`100009.ols` is BMW 7er. The WinOLS version and export settings that produced
these files are not recorded, and no independent project-metadata or map
exports have been supplied. Project identity is confirmed; individual
export-to-version relationships and export representation are not.

An unnormalized byte scan found a unique physical-slot candidate for each of
the ten files when comparing the export prefix with each OLS post-marker slot
payload. Every export is exactly four bytes longer than its candidate payload;
those trailing bytes are retained and hashed but their meaning is unknown.
Candidate slots inferred from byte equality alone are:

| OLS project | Export filename label | Candidate slot | Export bytes | Candidate payload bytes |
|---|---|---:|---:|---:|
| `100004.ols` (Golf) | Original | 0 | 5,767,168 | 5,767,164 |
| `100004.ols` (Golf) | P&B | 2 | 5,767,168 | 5,767,164 |
| `100004.ols` (Golf) | Stage 1 + P&B | 3 | 5,767,168 | 5,767,164 |
| `100004.ols` (Golf) | Stage 1 | 1 | 5,767,168 | 5,767,164 |
| `100007.ols` (Peugeot 307) | EGR OFF | 1 | 2,097,152 | 2,097,148 |
| `100007.ols` (Peugeot 307) | Original | 0 | 2,097,152 | 2,097,148 |
| `100007.ols` (Peugeot 307) | Stage 2 + EGR OFF | 3 | 2,097,152 | 2,097,148 |
| `100007.ols` (Peugeot 307) | Stage 2 | 2 | 2,097,152 | 2,097,148 |
| `100009.ols` (BMW 7er) | Original | 0 | 2,621,440 | 2,621,436 |
| `100009.ols` (BMW 7er) | Stage 1 | 1 | 2,621,440 | 2,621,436 |

Fresh, auditable analyses are stored as runs 4–6 in
`data\reference-reports\analysis\`. Across 36 examined slots, all ten exports
have one unique exact contiguous source-range match; none are ambiguous or
unmatched. In every case the four trailing export bytes equal the four bytes
immediately following the parser payload in the OLS file: `ba77dc9e` for Golf
and `ffffffff` for Peugeot and BMW. The bytes are retained; they are not
trimmed or assigned a marker/checksum meaning. This proves byte continuity for
these measured ranges, not the semantic role of the suffix.

The export filename labels still do not establish WinOLS version names. The
parser's named `Original` slots are 3, 3, and 1 for Golf, Peugeot, and BMW,
while the files labelled `Original` match physical slots 0, 0, and 0. No
export-to-version link is confirmed, all ten reference checks remain
`INCONCLUSIVE`, and no map records have independent validation. Exact binary
range matches are useful evidence, but these projects remain partial; the
version/name mismatch must be resolved before version-aware comparisons or
tuning pairs can be trusted.

## Real sample run in this workspace

The eight `.ols` files present in this folder were imported into
`data\ols-index.sqlite3`. All 8 were recognized (five format 597, three format
503) and all were stored as **partial**, not as independently validated
projects. A real-sample integration test re-imports them into a temporary
database and checks that 26 binary slots, 8 named-version records, 16 map
definitions, 31 axes, and 8 skipped hexdump records pass through the adapter
and database without count loss. Each project exposes one named `Original`
record; unnamed slot payloads remain unresolved. Six projects have zero parsed
map definitions because their only map-list record is the parser-filtered
hexdump container. The other two have 4 and 12 definitions; their names and
some axis metadata are empty. No map-value-to-version relationship was
recovered.

The parser layout derives slot payload extents from a binary record's base
offset, fixed blob size, and slot count. For the ten supplied references, the
export bytes exactly match a contiguous range beginning four bytes after the
slot start and extending through the four-byte boundary suffix described
above. Runs 4–6 record exact hashes, offsets, and comparisons; they do not
normalize the data or promote candidates to trusted named versions. This range
evidence does not establish that all 26 slots are valid project versions.
The map-list header says 1, 5 or 13 records per project; the parser returned
each declared record in these samples. It filters one unnamed `Hexdump` record
per file, leaving 0 definitions for six projects, 4 for `100009.ols`, and 12
for `100010054.ols`. Adapter and database map/axis counts match these parser
outputs. Unknown data types suppressed internally by the parser cannot be
reconstructed from its public model and remain an explicit parser limitation.

Per-project diagnostic snapshot from the current index:

| File | Format | File bytes | Slots (non-empty; unlabeled) | Named versions | Payload bytes per slot | Stored map definitions | Axes | Empty metadata fields | Parser errors / warnings |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|
| `100004.ols` | 597 | 23,081,851 | 4 (4; 3) | 1 | 5,767,164 | 0 | 0 | 0 | 0 / 1 |
| `100007.ols` | 597 | 8,538,495 | 4 (4; 3) | 1 | 2,097,148 | 0 | 0 | 4 | 0 / 2 |
| `100008.ols` | 597 | 12,869,126 | 3 (3; 2) | 1 | 4,194,300 | 0 | 0 | 11 | 0 / 2 |
| `100009.ols` | 597 | 6,067,756 | 2 (2; 1) | 1 | 2,621,436 | 4 MAP | 8 | 2 | 0 / 7 |
| `100010.ols` | 597 | 12,886,631 | 3 (3; 2) | 1 | 4,194,300 | 0 | 0 | 0 | 0 / 1 |
| `10001004.ols` | 503 | 13,198,494 | 3 (3; 2) | 1 | 4,194,300 | 0 | 0 | 0 | 0 / 1 |
| `10001005.ols` | 503 | 26,187,364 | 3 (3; 2) | 1 | 8,388,604 | 0 | 0 | 1 | 0 / 2 |
| `100010054.ols` | 503 | 17,457,135 | 4 (4; 3) | 1 | 4,194,300 | 11 MAP, 1 CURVE | 23 | 0 | 0 / 14 |

Every project is partial and needs reference validation. The “non-empty”
counts mean only that bytes exist in the observed fixed-stride slot ranges;
the slot count does not prove that the bytes are valid or addressable project
versions. Per-file SHA-256, exact version/slot names and descriptions,
metadata values, axes, warnings, raw structure counts, and stored counts are
available in `diagnose <project_id>` and the dashboard's Importdiagnose page.

The original OLS samples remain unchanged. The ten reference binaries are
stored separately under `ols files geextracteert`; their SHA-256 hashes and
sizes are recorded in the reference manifests. The other five sample projects
(`100008.ols`, `100010.ols`, `10001004.ols`, `10001005.ols`, and
`100010054.ols`) still have no reference exports. The three projects with
exports remain **inconclusive** until version/slot relationships and
independent metadata/map exports are established. WinOLS
5.80.00 is installed locally and 5.87.00 is present as a demo; these versions
were confirmed from executable file metadata. The installed 5.80 English help
documents **Mass export** from the project-open context menu: marked projects
can be exported with a selected version and format, and filename templates can
put each version in a separate file. Its **Export file** help describes raw
binary data, address ranges, offsets for Intel/Motorola hex, and optional ZIP.
The help also documents WinOLS scripts and a command window with Lua commands
(requires OLS530). This documents GUI/script features, not a supported
programmatic API that can safely select and export each named project version;
therefore no automation adapter is enabled. No official export was run in this
session, and the producing version/settings for the existing ten binaries
remain unknown.

The active SQLite index is schema 10. Migration retained 8 projects, 8 named
version records, 26 physical slots, 16 parsed map definitions, 31 axes, 10
reference artifacts, 10 reference checks, 6 analysis runs, and 72
slot-candidate records. The project-8 packet workflow has three initial audit
runs (inspection, version matching, and map comparison); each remains
`NOT_TESTED` until actual WinOLS exports are supplied.
Repeated validation attempts are retained in the append-only reference-check
history; a regression test guards against duplicating a check when a manifest
is updated.

The Phase 5 dry-run covered only the two known workspace folders, not the
unconfigured 6-TB corpus: 22 files (8 OLS and 14 reference files, about
173.8 MB), with full-hash matches for all 8 indexed OLS sources, no exact
duplicate groups, and no unreadable entries. Its JSON and JSONL reports are in
`data\inventory-reports\`. Export selection generated 16 pending tasks:
5 raw-version exports for projects with no binary references, 3 provenance
requests for existing inconclusive binaries, and 8 map-list requests. The
manual batch is `data\export-batches\`; it contains instructions only, not
WinOLS exports.

## Registered network and external data sources

Register a local, removable, or UNC directory. Registration checks actual
directory access and records unavailable paths without claiming they are
available. For example, a Windows share is passed directly as a UNC path:

```powershell
python -m ols_platform.cli sources add '\\100.117.16.25\d\Database 1'
python -m ols_platform.cli sources list
python -m ols_platform.cli sources test 1
python -m ols_platform.cli sources scan 1
python -m ols_platform.cli sources report 1 `
  --output data\inventory-reports\source-1.json
```

`sources scan` recursively inventories file metadata without modifying source
files or parsing projects. It persists discovered OLS work in SQLite, records
inaccessible entries for retry, and reads at most 16 MiB from the first OLS
file as a serial network-throughput sample (not a full hash). The report keeps
file discovery, indexed records, unverified stat fingerprints, errors, and
the measured read rate separate. A missing share is marked unavailable; no
project records are removed. A later scan after reconnect resumes discovery
and requeues retryable paths. To explicitly enqueue every OLS file for content
hash verification:

```powershell
python -m ols_platform.cli sources scan 1 --recheck
```

A completed item whose size and modification time match an existing indexed
record is retained as `fingerprint_match_unverified` and is not hashed on
every routine inventory. This is a performance optimization, not proof that
the bytes are unchanged; use `--recheck` when that stronger check is required.
New or stat-changed files are queued for a full hash before the parser's
unchanged-file decision. The per-file index queue retains completed and
retryable work; after a disconnect, the next dry-run starts directory
discovery again and the next index batch resumes queued work:

```powershell
python -m ols_platform.cli index-queue-run 1 --batch-size 1 --workers 1 `
  --py-ols-path C:\path\to\py-ols
```

Only one worker is currently accepted because `py-ols` reads whole files into
memory and parallel memory behavior has not been validated. Files above the
configured size limit are marked blocked before parsing. Failed parser reads
remain distinguishable from unavailable network paths; retryable filesystem
errors are not treated as corrupt OLS projects. The dashboard can register a
source, test access, and run a dry-run, but parser indexing remains a bounded
CLI operation. It binds to loopback and never writes to the source files.

The project search can combine exact ECU, hardware, software, calibration and
vehicle metadata; source path/hash/size; parser and reference-validation
status; version names; map names/descriptions and dimensions. Results are
bounded to 100 per page by the dashboard; the service and CLI support
`--limit` (maximum 1000) and `--offset`. Complete SHA-256 values found by the
live file-location hasher are visible to search without a parser re-import.
When both exist, search and project matching prefer that complete live content
hash over the parser-import snapshot and report the hash provenance. If the
hashes differ, parser-derived metadata and binary-slot evidence are flagged as
potentially stale and cannot raise a project comparison to `LIKELY`; missing
live hashes remain explicitly unverified rather than being treated as current.
Text searches are substring scans and may be slower on large indexes; queries
are timed, slow queries are logged locally, and an individual SQLite search
statement is interrupted after five seconds so more filters can be applied.
SQLite lock/busy errors return a retryable dashboard response. The existing
WAL journal mode and busy timeout are reused; search adds no schema migration
or queue writes.

Example:

```powershell
python -m ols_platform.cli search-projects `
  --ecu-type ECU-A --sw-number SW-123 `
  --min-file-size 1000000 --max-file-size 50000000 `
  --parser-status partially_processed --limit 50 --offset 0
```

Results carry matched fields, parser warnings/errors, missing-information
flags, available versions and binary slots, and bounded map/axis summaries.
Project detail also shows the current evidence and differences for comparable
projects. A `CONFIRMED` exact full-file hash means only that OLS file contents
are identical; it does not independently validate metadata, versions or tune
semantics. Exact ECU metadata, parser slot hashes, and matching map definitions
remain evidence with explicit uncertainty. Map names or dimensions do not
prove that map values decode correctly in every binary version.

The deterministic `rank_score` is a retrieval tally, not a probability or
confidence value: +10 per exact ECU/calibration identifier, +100 for a complete
source SHA-256 match, +12 for a parser-reported binary payload hash, +4 for
matching map-name or dimension criteria, +4 for a version-name filter, +5 for independently
validated reference status, +2 when map and axis definitions exist, and +1
when the parser marked a project fully processed. The `CONFIRMED`, `LIKELY`,
and `INCONCLUSIVE` labels retain their separate evidence-based meanings.
`REJECTED` is reserved for an explicit ECU-type conflict or simultaneous
hardware-number and hardware-code conflicts, absent an identical complete
source hash.
`parser-coverage` groups stored parser issues by format and problem; parser
coverage is not independent validation. No embedding provider or tuning model
is configured. Training candidates can be registered with provenance and
validation state, but no training operation exists.

## ECU File Workbench

The dashboard's **ECU File Workbench** accepts raw `.bin` and `.ori` byte
streams. An `.ori` suffix is treated as an original raw binary input; it is not
decoded as a WinOLS project or a different address-bearing format. Select a
file in the browser; it is streamed to the loopback dashboard in bounded
1-MiB reads, SHA-256 hashed over every byte, and stored as a content-addressed
copy under `data\ecu-workbench\files`. The original is never modified. The
upload is limited to 4 GiB. Intel HEX, checksums, address translation, and
other ECU formats are not supported because no validated importer is
configured. A `.bin` extension or accepted byte stream does not establish that
the content is a valid ECU image.

Vehicle/ECU identifiers entered in the form are recorded as
`user_provided`. No identifiers are automatically decoded from BIN data.
Missing identity evidence is reported as `UNKNOWN`; a supplied identifier is
kept explicitly unverified unless independently supported. The raw input
adapter establishes only the filename extension, full-byte SHA-256, and size;
it does not validate the ECU format or decode ECU identity.

The report preserves parser identity, version, format, processing status, and
the source/status of project metadata separately from raw-file evidence. A
parser-reported identity field is `DERIVED` only when there is one individually
validated exact full-binary reference. Parser or metadata candidates without
that unique relationship remain `INCONCLUSIVE`; conflicting candidate values
are `CONFLICTING`. A `CONFIRMED_REFERENCE` is a validated project/version
reference relationship, not proof of vehicle compatibility. Multiple exact
binary references are ambiguous and cannot be reported as a confirmed
project/version assignment; their byte match is still exposed separately as
`EXACT_CONTENT_MATCH`.

The analysis report uses existing `reference_artifacts` only when the stored
artifact is explicitly a
`binary_version` with `full_binary` representation and matching complete hash
and size. It reports `CONFIRMED_REFERENCE` only when the recorded version
relation is confirmed and the latest reference check is `PASS`; otherwise an
unambiguous exact byte match remains `EXACT_CONTENT_MATCH`. A stale parser
snapshot or conflicting identity evidence cannot promote a candidate to a
confirmed reference. Before a known reference is added to the Workbench, the
local file is re-read and verified against its stored full-file hash and size.

Parser-reported slot payload hash/size matches and metadata matches are
candidates, not content proof. A metadata candidate cannot be elevated to
`LIKELY` unless the complete live file-location hash matches the parser-import
snapshot; absent live hashes or stale snapshots remain `INCONCLUSIVE`. The
documented deterministic project-search rank is reused and is not a
probability. `NO_MATCH` means no match was found in the available index, not
that a reference does not exist.

Two imported or indexed reference binaries can be compared from the dashboard.
The report contains their full hashes and sizes, differing-byte count,
half-open changed ranges, origins, and a `semantic_status` of
`INCONCLUSIVE`. It does not interpret map values, offsets across differing
representations, checksums, or tuning intent. Parser map/axis metadata is
available on candidate projects but is not bound to the compared BIN values.
No automatic tuning or remote AI provider is enabled; the provider boundary
is defined but unconfigured, and raw files remain local.

Analysis and comparison history is append-only in a separate
`data\ecu-workbench\workbench.sqlite3` database. Reanalysis creates a new
record linked to its parent. Workbench search opens the project index in
read-only mode and does not migrate or write it, so it can run alongside the
hashing worker without touching its queue or hash records.
See [PHASE10_STATUS.md](./PHASE10_STATUS.md) for Phase 10 coverage and
[PHASE11_STATUS.md](./PHASE11_STATUS.md) for the validated initial Phase 11
scope, test results, and remaining limitations.

Five named roots are configured in the live database and are checked with
Windows directory operations before each inventory run:

1. `\\100.117.16.25\d\Database 1`
2. `\\100.117.16.25\d\Database 2`
3. `\\100.117.16.25\d\Database 3`
4. `\\100.117.16.25\d\Database 4`
5. `\\100.117.16.25\d\Default`

The earlier Database 1 scan found 425,923 paths (425,812 `.ols`, 111 other)
and reported 3,268,853,031,855 physical bytes. Its 58.771 MiB/s measurement
was a single prefix read, not a representative full-file hash benchmark.
That earlier inventory did not scan the other four roots or establish the
unique-content size of the full database. At that time the index retained 8
projects, 8 version records, 26 binary slots, 16 map definitions, 31 axes,
10 reference artifacts/checks, 10 reference history records, 6 analysis runs,
and 72 slot candidates; reference checks remain `INCONCLUSIVE`.

Schema 14 adds a persistent per-directory scan queue and file-level scan
errors. The multi-source scanner processes one directory at a time, saves
bounded batches to SQLite, and resumes incomplete directories. A source that
cannot be reached is reported as unavailable, not empty; only a completely
successful scan may mark unobserved locations absent. Every physical location
is retained separately. Matching filenames and sizes are candidate duplicates
only; only equal full-file SHA-256 hashes establish exact content identity.
No OLS file is moved, overwritten, parsed, or fully hashed during metadata
inventory.

Run the five-source inventory and its capped streaming SHA-256 sample with:

```powershell
python -m ols_platform.cli source-inventory `
  --progress-every 10000 `
  --max-hash-files 25 `
  --max-hash-bytes 2147483648 `
  --report data\inventory-reports\multi-source-inventory.json
```

The hash benchmark is limited to 25 OLS files and 2 GiB total, selected across
available roots and size classes. Its full-dataset time estimate is indicative
only and varies with the network, storage load, caching, and file-size mix.
The live dashboard exposes the last report, per-root and per-extension
physical totals, metadata-only duplicate candidates, scan errors, indexed
locations, and benchmark results. It remains loopback-only.

### Phase 7 live inventory result

The five configured UNC paths were freshly checked with Windows directory
operations and all were readable. A real recursive metadata scan completed
successfully on each source: 1,074,394 physical file paths, including
1,074,266 `.ols` paths. The combined physical size is 6,747,450,653,979 bytes
(about 6.75 TB decimal); the OLS-only physical size is 6,740,143,316,610 bytes.
Physical size counts copies at each location. Exact unique-content size is not
known because only a bounded sample was hashed.

| Source | Paths | OLS paths | Physical bytes |
|---|---:|---:|---:|
| Database 1 | 425,923 | 425,812 | 3,268,853,031,855 |
| Database 2 | 454,337 | 454,333 | 2,418,017,440,754 |
| Database 3 | 87,532 | 87,529 | 398,597,621,176 |
| Database 4 | 105,377 | 105,373 | 652,418,879,302 |
| default | 1,225 | 1,219 | 9,563,680,892 |

The scan recorded zero inaccessible sources, incomplete directories, or
file/directory errors. It found 100 same-name/same-size candidate groups
(100 locations beyond the first); these are not confirmed duplicates. The
20-file SHA-256 benchmark read 914,462,915 bytes in 26.755 seconds at an
overall 32.595 MiB/s, with no failed samples. Each root contributed four
samples: Database 1 31.379 MiB/s, Database 2 32.993 MiB/s, Database 3
34.300 MiB/s, Database 4 33.239 MiB/s, and default 32.688 MiB/s.
The benchmark used less than its 25-file / 2-GiB limit.

At that sample rate, reading all 6,740,143,316,610 OLS bytes would take an
indicative 197,204 seconds (about 54.8 hours). This is an uncertain estimate,
not a completed full hash: network load, caching, file sizes, storage speed,
and retries can materially change it. Twenty sampled contents have stored
SHA-256 identities; 1,074,246 OLS locations remain unhashed and all 1,074,266
remain unindexed. No parser imports or tuning operations were started.
Standalone `.bin` files were found (5); `.a2l`, `.kp`, and `.rar` files were
not present in the extension inventory. Other observed extensions include
`.dir`, `.bdc`, `.dtf`, `.mpc`, `.ini`, and `.lnk`.

The persisted machine-readable report is
`data\inventory-reports\multi-source-inventory.json`. The completed scan did
not change the pre-existing 8 projects, 8 version records, 16 map
definitions, 31 axes, 10 reference checks (all still `INCONCLUSIVE`), 10
reference-history records, or 6 binary-analysis runs.

## Phase 8 resumable full-file hashing

The persistent hash queue reads only the five configured source roots and
streams SHA-256 in bounded chunks with at most two workers. It checks each
location's size and modification time before and after the read, records every
attempt, and leaves incomplete or unavailable files retryable. Full hashes
confirm duplicate content; filenames, sizes, and map/binary similarities do
not. Hash status is separate from parser/index status.

Inspect accessibility and the pending workload without opening source-file
contents, then process a bounded batch:

```powershell
python -m ols_platform.cli hashing plan
python -m ols_platform.cli hashing run --workers 2 --max-files 1000 `
  --report data\hashing-reports\phase8-batch-next.json
python -m ols_platform.cli hashing status
python -m ols_platform.cli hashing duplicates --output data\hashing-reports\duplicates.json
```

The queue is persisted in SQLite. Re-running `hashing run` resumes pending and
retryable work; `hashing pause`, `hashing resume`, `hashing recover`, and
`hashing retry` control or recover tasks. Do not remove historical locations:
a source that is temporarily unavailable is not treated as empty.

### Phase 8 live result

The schema-14 database was backed up to
`data\backups\ols-index-pre-hashing-migration.sqlite3` (integrity check `ok`)
before the migration to schema 15. All five existing UNC roots were checked
and were available; no source paths were changed. The read-only hashing plan
found 1,074,266 OLS locations and 6,740,143,316,610 physical bytes. It
opened no source files. Twenty locations already had hashes from the Phase 7
sample.

The first real queue batch used two workers and hashed 10 files from Database
1 (the selected files totaled 78,554,412 bytes). All ten completed without
errors. The run-wide wall-clock rate, including queue processing, was 9.639
MiB/s; the measured per-source read rate was 23.185 MiB/s. Across all roots,
30 locations (993,017,327 bytes) now have hashes, with 1,074,236 still
pending. No exact duplicate group was confirmed in the hashed sample; this
does not establish that the remaining corpus has no duplicates.

The current extrapolation is about 666,746 seconds (185 hours / 7.7 days) at
the run-wide rate. It is an uncertain estimate: the batch is small, queue and
SQLite overhead affect its wall-clock rate, and SMB load, file sizes, caching,
and retries can change performance substantially. The machine-readable batch
report is `data\hashing-reports\phase8-batch-001.json`. The original OLS files
were read only. The migration and hash run preserved the 8 project records, 8
version records, 16 map definitions, 31 axes, 10 reference checks, 10
reference-history records, and 6 binary-analysis runs.

### Continuous worker

The one-shot `hashing run` command intentionally ends after `--max-files`
(default 1,000; `0` drains one run). For resumable continuous processing, use
the long-running worker. It loops over bounded, persisted batches and uses the
same streaming SHA-256 engine:

```powershell
python -m ols_platform.cli hash-worker --continuous `
  --workers 2 --batch-size 10 --chunk-size 1048576 `
  --batch-delay 1 --retry-limit 5 --retry-delay 60
```

`--retry-limit` is the maximum automatic read attempts per file. A disconnected
source is retried after `--retry-delay`; exhausted or otherwise blocked
locations remain visible and can be manually retried with `hashing retry`.
Each complete hash attempt is committed before the next task. A partial read
is never stored as a complete hash. Active file bytes, worker heartbeats,
throughput, errors and queue progress are shown by the dashboard and
`hashing status`. `hashing pause`, `hashing resume`, and `hashing stop` control
the worker. Stop or Ctrl+C waits for active reads to finish, records any
completed results, and preserves unfinished work for the next launch. A
database lease and a local launcher lock prevent two continuous workers from
starting on the same index.

Hash performance reports distinguish the average since worker start from the
wall-clock rate across the ten most recent completed batches. The ETA divides
the actual remaining OLS bytes by that recent wall-clock rate; it is an
estimate, not a completion guarantee. New hash-run reports also measure time
inside file reads, SHA-256 updates, and SQLite writes. File-read time includes
SMB wait or cache latency and is not a network-only measurement. Per-reader
durations can overlap when two readers run concurrently. A worker already
running when this timing instrumentation is deployed continues unchanged; its
older runs will not have the read-versus-database timing breakdown.

On Windows, the active worker requests that the system stay awake while it
hashes; it does not request that the display stay on and it does not modify
power policy. Hashing is capped at two readers. The live queue is still being
hashed; no run is reported as complete until every present OLS location has a
full-file hash.

### Dell-local worker deployment

The development machine is `BUGRA`, not the OLS host. Its five shares are
reachable as `\\100.117.16.25\d\...`, but the host's local share configuration
and actual local paths cannot be inspected or remotely executed from this
session.
All five IP-based directories were checked from `BUGRA` and were reachable.
Remote `Get-SmbShare`/CIM inspection and WinRM were unavailable, so no
Dell-local drive path has been inferred or claimed.
Therefore no Dell deployment or local-disk hashing is claimed as performed.
The deployment scripts verify the expected host IP locally and resolve paths
from that machine's actual Windows share definition instead of assuming `D:\`.

Create a consistent SQLite backup and a source-only application archive:

```powershell
python deployment\prepare_dell_deployment.py
```

The output directory `data\dell-deployment\` contains:

- `ols-hash-worker.zip` — application source, tests, and deployment scripts;
- `ols-index.sqlite3.backup` — SQLite-backup-API copy of the index, not the live DB;
- `database-manifest.json` — backup SHA-256, integrity snapshot and pre-copy counts.

Transfer these three files to Dell, extract the ZIP outside the five source
directories, then run in PowerShell as the intended Windows user:

```powershell
Expand-Archive .\ols-hash-worker.zip -DestinationPath .\ols-hash-worker-staging
& .\ols-hash-worker-staging\OlsHashWorker\deployment\install_hash_worker.ps1 `
  -DatabaseBackup .\ols-index.sqlite3.backup `
  -DatabaseManifest .\database-manifest.json `
  -RegisterTask -StartWorker
```

The installer verifies that `100.117.16.25` is assigned to the local machine
(override `-ExpectedServerAddress` only if the host's verified address changes),
checks Python 3.11+, resolves the local directory of SMB share `d` using
`Get-SmbShare`, verifies
all five directories and read access, runs the test suite, restores and
migrates the database locally, and atomically rebases stored file paths from
the UNC roots to those verified local paths. It refuses to overwrite an
existing install or database. The index must remain local on Dell; do not open
the same SQLite file from both PCs over SMB.

The optional scheduled task starts at logon for the current user, requires no
embedded password, ignores duplicate task instances, and retries failed
starts. It does not configure organization power policy. The worker itself
inhibits system sleep while active, while allowing display sleep. Check status
and control it with:

```powershell
& "$env:LOCALAPPDATA\OlsHashWorker\deployment\start_hash_worker.ps1" -Action Status
& "$env:LOCALAPPDATA\OlsHashWorker\deployment\start_hash_worker.ps1" -Action Pause
& "$env:LOCALAPPDATA\OlsHashWorker\deployment\start_hash_worker.ps1" -Action Resume
& "$env:LOCALAPPDATA\OlsHashWorker\deployment\start_hash_worker.ps1" -Action Stop
```

The package and migration backup can be prepared here, but actual Dell
installation, the local short hash test, scheduled-task registration, and the
continuous Dell run remain pending until these commands are run on Dell. The
parser dependency `py-ols` is not bundled; it remains a separate optional
parser checkout and is not required for streaming hashing.

The current local SQLite snapshot was verified at schema 16 with integrity
`ok`: 8 projects, 8 project versions, 26 slots, 16 map definitions, 10
reference artifacts, 10 reference checks/history rows, and 6 analysis runs.
It preserves 30 full-file hashes and 1,074,236 pending OLS locations; all 10
reference checks remain `INCONCLUSIVE`. The package manifest contains the
complete table counts and record digests. No additional content hashing was
performed while preparing this deployment.

## Tests

```powershell
$env:PY_OLS_PATH = "C:\path\to\py-ols"
python -m unittest discover -s tests -v
```

The eight-project integration test is skipped unless `PY_OLS_PATH` points to a
separate py-ols checkout and all eight sample files are available in the
workspace. Unit tests still run without the external parser.
The Phase 11D full run used the configured separate parser checkout and all
eight samples: 91 tests passed, with 0 failures and 0 skips. Both real-parser
integration tests ran. The unittest child process exit was `0`; the PowerShell
tool wrapper reported `1` for the same invocation, so these results and exit
codes are recorded separately in `PHASE11_STATUS.md`.

### Read-only OLS parser diagnostics

With the eight samples and a separate `py-ols` checkout available, create a
detailed, read-only JSON parser report outside the repository:

```powershell
$env:PY_OLS_PATH = "C:\path\to\py-ols"
python -m ols_platform.ols_sample_diagnostics `
  --samples-dir "C:\path\to\ols-samples" `
  --py-ols-path $env:PY_OLS_PATH `
  --output "$env:TEMP\ols-sample-diagnostics.json"
```

The report records per-file SHA-256, parser and OLS format versions, metadata
field presence/emptiness as exposed by the parser object, named versions,
enumerated binary slots, map types, map and axis details, and every parser
warning/error. Empty parser fields do not prove that the corresponding source
field is absent from the OLS project. The tool only reads the OLS samples and
refuses to write its report over one of them. Reference exports use the
canonical `reference-package-template` manifest and the package
`reference-version-match`, `reference-metadata-compare`, and
`reference-map-compare` commands. The diagnostic report itself is never an
independent reference.

### Independent WinOLS reference validation

The parser diagnostic report is observational only. Use the existing
reference-package manifest to attach separate WinOLS exports, record their
hashes, sizes, export method/settings, project/version/slot relationships, and
manual review evidence, then run `reference-package-inspect` and the relevant
version, metadata, or map comparison. Results stay `NOT_TESTED` when an
independent artifact is absent and `INCONCLUSIVE` when its provenance or
relationship is incomplete. No independent WinOLS exports were available for
the eight samples during Phase 11D, so their structural validation remains
`NOT_TESTED`.

### Phase 11E independent parser audit

The Phase 11E audit compared read-only observations from `py-ols` and `jinglinsen/ols-parser` on the eight tracked OLS samples. Neither parser is ground truth; disagreements and extracted hashes do not independently validate maps, versions, or binary exports. See the [audit and verified test results](./PHASE11E_STATUS.md) and [machine-readable parser report](./reports/phase11e_parser_audit.json). Independent WinOLS validation remains `NOT_TESTED`; Phase 11 remains in progress.

### Phase 11F/11G review

Phase 11F's structural regression results and the Phase 11G independent code-review verdict are documented in [PHASE11F_STATUS.md](./PHASE11F_STATUS.md) and [PHASE11G_REVIEW.md](./PHASE11G_REVIEW.md). The review found and fixed an invalid version-record geometry case that could mislabel a binary slot. Phase 11 remains in progress; independent WinOLS validation and ECU compatibility remain `NOT_TESTED`.

### Phase 12 parser validation

Phase 12 reran the parser-structure regressions and the real-parser integration tests against the eight read-only OLS samples using a temporary database. All 26 focused and integration tests passed, and all 14 tracked OLS/BIN fixture sizes and SHA-256 values remained unchanged. This validates only the adapter's tested behavior; independent WinOLS validation remains `NOT_TESTED`. See [PHASE12_STATUS.md](./PHASE12_STATUS.md).

## Phase 5 limits

The inventory command is a dry-run and is not the 6-TB indexer. It has not been
run against an unspecified full database volume. The existing importer remains
serial and uses a configurable per-file size cap; `py-ols` reads a whole OLS
file into memory, so parallel parsing or relaxing the cap is not safe without
separate memory measurements and parser validation. Full-hash verification
must be deliberately requested; default stat matches are labeled
`stat_match_hash_unverified`.

The project matcher is read-only and stores no inferred project/version links.
It provides an evidence explanation and preserves uncertainty; no map values
are decoded, no embeddings or external AI APIs are used, and no tuning files
are generated. No WinOLS automation is enabled. Use the export queue and the
existing reference packet workflow until the installed WinOLS version,
documented automation interface, license permissions, and correct version
selection can be established.
