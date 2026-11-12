# Phase 11 status — initial identity evidence and OLS parser validation

## Scope and publication

- Repository: [BugraOrhan1/files-ols](https://github.com/BugraOrhan1/files-ols)
- Branch: `main`
- Verified base revision before these changes: `4bcb141f293e13e70adc20bd65e930a8d87c8fef`
- Validated implementation commit: `bc0012c322edf4d72654daa8fcbbb9ffac05ad7d`
  (implementation, tests, and README).
- Published Phase 11 revision: [`a93af53557d516459580b698e4c4c7a2ac8db129`](https://github.com/BugraOrhan1/files-ols/commit/a93af53557d516459580b698e4c4c7a2ac8db129).
- Remote `main` and the published code, tests, README, and status-document blob
  IDs were verified against the local Git objects after push.
- Initial ECU identity and reference-evidence implementation commit:
  `bc0012c322edf4d72654daa8fcbbb9ffac05ad7d`.
- This report now also records Phase 11B's real-parser sample validation.
  Neither phase establishes that all Phase 11 goals are complete.

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

## Phase 11 initial-scope verification

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

The initial-scope full suite was run before the external parser was configured;
its two skips were subsequently eliminated by the Phase 11B run below.

## Phase 11B: actual py-ols sample run

The separately checked-out parser was at upstream commit
`4c283a80161e130b03eb8743c752a689ecda6b78` (`more parsing fixes`). The checkout
has no `pyproject.toml`, `setup.py`, requirements file, or other installation
manifest. Its package initializer reports version `1.1.0`; its parser module
imports only Python standard-library modules. It was configured as a source
checkout using `PY_OLS_PATH=C:\Users\Bugra\Desktop\py-ols-study`, which is the
path mechanism documented by this application. The adapter successfully
loaded `OLSReader` and `read_ols`; its parser fingerprint was
`adapter-3+py-ols-sha256:02dacf610120f5eb4971aa0cd606efd29821b13a9e32db0165e6d5be49832771`.

The parser checkout was already locally modified before this run: its only
tracked diff changes the CLI's x-axis display from the conversion `offset` to
the `address` and adds a formatting helper; untracked parser tests are also
present. No checkout files were changed during Phase 11B. The diff is outside
the parser's `.ols` API path; for reproducibility, the exact parser-module
fingerprint above identifies the file that the adapter loaded.

The checkout includes a GPL-3.0 license. The parser is not bundled into this
repository and is loaded from the external source path. This does not resolve
distribution obligations if it is later combined with or distributed beside
the application; obtain legal review before shipping such a product.

The existing real-parser test module was executed with `PY_OLS_PATH` set:
both `test_eight_projects_import_without_losing_parser_output` (which imports
all eight samples into a temporary SQLite database) and
`test_damaged_file_is_reported_as_failed_by_real_parser` ran and passed. A
separate read-only inspection using the same `PyOlsReader` recorded these
sample results:

| Sample | SHA-256 | Format | Parser result | Named versions / enumerated slots | Maps / axes | Missing recognized metadata | Parse warnings |
|---|---|---:|---|---:|---:|---|---|
| `100004.ols` | `053aa96ea3021a6e3d71b11fe8023bab31650205a41196e8ecc39968b006167c` | 597 | Parsed with warnings | 1 / 4 | 0 / 0 | none | 3 unlabeled slots |
| `100007.ols` | `726179f17d56922d3667e18f87af739e49f59732df963332195c0249dad6a178` | 597 | Parsed with warnings | 1 / 4 | 0 / 0 | gearbox, ECU type, SW number, HW number | 4 missing metadata fields; 3 unlabeled slots |
| `100008.ols` | `7234cdacd4509455724b9a2ec318c5ce7edc17cdb3210ef2f3691e290f32b4cf` | 597 | Parsed with warnings | 1 / 3 | 0 / 0 | make, model, engine, year, fuel, displacement, power, gearbox, SW/HW number, HW code | 11 missing metadata fields; 2 unlabeled slots |
| `100009.ols` | `ef55e64d62358f1da11c36631ea0ba572383faa803188b556821ab5bcd3956ee` | 597 | Parsed with warnings | 1 / 2 | 4 / 8 | year, gearbox | 2 missing metadata fields; 4 unnamed maps; 4 incomplete-axis warnings at `0x224dda`, `0x2249a6`, `0x1aa468`, `0x1aa224`; 1 unlabeled slot |
| `100010.ols` | `4f20f13b6fc0c7a1606073b00a392ab8051db1c5689eff0fcc704bc594c8bd16` | 597 | Parsed with warnings | 1 / 3 | 0 / 0 | none | 2 unlabeled slots |
| `10001004.ols` | `2e22340c61b1c9b44710501775669e8d75d0592d6bb936245ca80fd899db0326` | 503 | Parsed with warnings | 1 / 3 | 0 / 0 | none | 2 unlabeled slots |
| `10001005.ols` | `4a117e3549cf457c1a8977f5e1da497517e7f3c21e80bf138ca72dd108cce2d3` | 503 | Parsed with warnings | 1 / 3 | 0 / 0 | HW number | 1 missing metadata field; 2 unlabeled slots |
| `100010054.ols` | `eda083e60ad20c8b4f286b289bb6760f7e5a9666b496e7e1ce2ef16b978bc635` | 503 | Parsed with warnings | 1 / 4 | 12 / 23 | none | 12 unnamed maps; 12 incomplete-axis warnings at `0x3a9bb0`, `0x3b0232`, `0x3afbf8`, `0x3af4a0`, `0x3af328`, `0x3af1b0`, `0x3aeebe`, `0x3af038`, `0x3aebce`, `0x3aed46`, `0x3b7c54`, `0x371a62`; 3 unlabeled slots |

Across the eight samples the parser returned 8 named version records (each
named `Original`) but the adapter enumerated 26 binary slots; 18 slot names
remain unresolved. All 26 enumerated slots were classified by the adapter as
`present_nonempty_unverified`. This is a parser/adapter observation, not proof
that these are all WinOLS versions or that each slot has the assumed meaning.
The six non-empty version descriptions are import-history text; the other two
are empty. No independently exported WinOLS file was available here to
validate a named version against an enumerated slot.

Among the 16 returned map definitions, all names were empty. The parser
reported 31 axes, with missing axis unit or identifier warnings for each map
(16 warning records). Its recognized metadata values, grouped by sample, were:

- `100004`: make `VW`, model `Golf`, engine `2.0 TSI`, year `2017`, fuel
  `Turbo-Benzin`, displacement `2.0`, power `245.0PS / 180.2kW`, gearbox
  `DSG7`, ECU maker `Siemens/Continental`, ECU type `SIMOS18.10`, SW
  `5G0906259N`, HW number `5G0906259N`, HW code `SCG0051000000`, memory
  `Eprom`, user `Filedatabase.eu`.
- `100007`: make `Peugeot`, model `307`, engine `2.0 HDi 16V`, year `2011`,
  fuel `Turbo-Diesel`, displacement `2.0`, power `150.0PS / 110.3kW`, HW code
  `PO30052000000`, ECU maker `Siemens/Continental`, memory `Eprom`, user
  `Filedatabase.de`.
- `100008`: ECU maker `Siemens/Continental`, ECU type `SID209`, memory
  `Eprom`, user `Filedatabase.eu`.
- `100009`: make `BMW`, model `7er`, engine `50D`, fuel `Turbo-Diesel`,
  displacement `2.993`, power `399.7PS / 294.0kW`, ECU maker `Bosch`, ECU
  type `MDG1_MD1CP002`, SW
  `B57D30S0-G1xB57S0_RDE#S15A-17-07-504-FS (O_8KTB1-000047D1-028)`, HW number
  `8KTB1-000047D1-028`, HW code `B57D30S0-G1`, memory `Eprom`, user
  `Filedatabase.eu`.
- `100010`: make `MB`, model `C220`, engine `2.2 CDI R4`, year `2011`, fuel
  `Turbo-Diesel`, displacement `2.2`, power `170.0PS / 125.0kW`, gearbox
  `Automatik`, ECU maker `Delphi`, ECU type `CRD3`, SW
  `CRD3-651-WMA4HD11-204WM-125kW-4CSL2S-EU5OP_AJ122_LH-EF-DS04_ME185001`, HW
  number `6519040001`, HW code `6519024803`, memory `Eprom`, user
  `Filedatabase.eu`.
- `10001004`: make `Audi`, model `RS6`, engine `4.0 TFSI V8`, year `2017`,
  fuel `Turbo-Benzin`, displacement `4.0`, power `560.0PS / 411.9kW`, gearbox
  `Automatik`, ECU maker `Bosch`, ECU type `MED17.1.1`, SW `4G0906560F`, HW
  number `4H0906014`, HW code `013988`, memory `Eprom`, user `DYNO Setting`.
- `10001005`: make `MB`, model `GT-S AMG`, engine `4.0 AMG V8 Biturbo`, year
  `2016`, fuel `Turbo-Benzin`, displacement `4.0`, power `510.0PS / 375.1kW`,
  gearbox `DKG`, ECU maker `Bosch`, ECU type `MED17.7.5`, SW
  `8Y35210H0830000 1201`, HW code `033192`, memory `Eprom`, user `DYNO Setting`.
- `100010054`: make `Audi`, model `A8`, engine `4.2 TDI CR V8`, year `2012`,
  fuel `Turbo-Diesel`, displacement `4.2`, power `350.0PS / 257.4kW`, gearbox
  `S-Tronic 8`, ECU maker `Bosch`, ECU type `EDC17CP44`, SW and HW number
  `4H0907409B`, HW code `522813`, memory `Eprom`, user `Fastech Setting`.

These are values the parser returned, not independently verified ECU facts.
The returned definitions, axis metadata, format IDs, version names, and parser
data remain parser output/provenance; they do not prove compatibility or
relationships. All eight parses had warnings and no parser errors. This
matches the existing integration assertions (format versions, slot/map/axis
counts, one `Original` record, and unresolved slots); the earlier
[OLS integration assessment](./OLS_INTEGRATION_ASSESSMENT.md) remains clear
that no WinOLS export validation was performed.

## Phase 11B verification

| Check | Result |
|---|---|
| Parser import/API probe | Passed; loaded `OLSReader` and `read_ols`, reported package version `1.1.0` |
| Existing eight-sample and damaged-file integration test module | 2 tests passed, 0 failed, 0 skipped; exit `0` |
| Full suite with `PY_OLS_PATH` configured | 82 tests run, 82 passed, 0 failed, 0 skipped; unittest child exit `0` |
| Full-suite PowerShell/tool wrapper | Reported `1` although the `cmd`-captured Python exit was `0` and the complete log ends `OK`; no test failure was hidden |
| `python -m compileall -q ols_platform deployment tests` | Exit `0` |
| `node --check ols_platform\dashboard.js` | Exit `0` |
| `git diff --check` | Exit `0` (to be rerun after this document edit) |

Parser metadata remains evidence from the named parser build only. A matching
parser result is not independent ECU identity or project/version validation.
The eight sample hashes were checked again after parsing and matched the
pre-run values. Parser reads, tests, and import assertions used source samples
read-only and a temporary SQLite database; the live index and queue were not
opened. PID `45712` was verified still running after these parser tests with
the same continuous two-reader command. No source OLS/BIN files or tuning
files were created or changed.

The Phase 11B documentation-only revision and remote publication status are
recorded after commit and push.

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

## Phase 11C: repeatable parser diagnostics and reference gating

Added `python -m ols_platform.ols_sample_diagnostics` as a read-only way to
produce per-sample JSON diagnostics outside the repository. It records the
source SHA-256 and size, parser identity/version, OLS format, parse status,
metadata field states, all returned versions and slots, map-type counts,
complete map/axis fields, skipped elements, parser errors, and every warning.
Optional reference templates start at `NOT_TESTED`; the command refuses to
write its report over any of the eight source samples. The test suite continues
to import the samples only into a temporary SQLite database.

Metadata field status describes what the parser object exposes: the adapter
distinguishes an absent parser attribute from an empty returned value. All
eight samples exposed the 15 expected metadata attributes; 18 values were
empty and none were absent from the parser object. This does not prove that
the OLS source itself lacks those values. Unnamed map definitions and
structurally incomplete axes are represented explicitly; no names, labels,
addresses, version relationships, or conversion values are inferred.

| Sample | Source size | SHA-256 | OLS format | Parser status | Named versions / slots | Maps / axes | Incomplete axes | Warnings | Reference |
|---|---:|---|---:|---|---:|---:|---:|---:|---|
| `100004.ols` | 23,081,851 | `053aa96ea3021a6e3d71b11fe8023bab31650205a41196e8ecc39968b006167c` | 597 | Parsed with warnings | 1 (`Original`) / 4 | 0 / 0 | 0 | 1 | `NOT_TESTED` |
| `100007.ols` | 8,538,495 | `726179f17d56922d3667e18f87af739e49f59732df963332195c0249dad6a178` | 597 | Parsed with warnings | 1 (`Original`) / 4 | 0 / 0 | 0 | 2 | `NOT_TESTED` |
| `100008.ols` | 12,869,126 | `7234cdacd4509455724b9a2ec318c5ce7edc17cdb3210ef2f3691e290f32b4cf` | 597 | Parsed with warnings | 1 (`Original`) / 3 | 0 / 0 | 0 | 2 | `NOT_TESTED` |
| `100009.ols` | 6,067,756 | `ef55e64d62358f1da11c36631ea0ba572383faa803188b556821ab5bcd3956ee` | 597 | Parsed with warnings | 1 (`Original`) / 2 | 4 / 8 | 8 | 7 | `NOT_TESTED` |
| `100010.ols` | 12,886,631 | `4f20f13b6fc0c7a1606073b00a392ab8051db1c5689eff0fcc704bc594c8bd16` | 597 | Parsed with warnings | 1 (`Original`) / 3 | 0 / 0 | 0 | 1 | `NOT_TESTED` |
| `10001004.ols` | 13,198,494 | `2e22340c61b1c9b44710501775669e8d75d0592d6bb936245ca80fd899db0326` | 503 | Parsed with warnings | 1 (`Original`) / 3 | 0 / 0 | 0 | 1 | `NOT_TESTED` |
| `10001005.ols` | 26,187,364 | `4a117e3549cf457c1a8977f5e1da497517e7f3c21e80bf138ca72dd108cce2d3` | 503 | Parsed with warnings | 1 (`Original`) / 3 | 0 / 0 | 0 | 2 | `NOT_TESTED` |
| `100010054.ols` | 17,457,135 | `eda083e60ad20c8b4f286b289bb6760f7e5a9666b496e7e1ce2ef16b978bc635` | 503 | Parsed with warnings | 1 (`Original`) / 4 | 12 / 23 | 23 | 14 | `NOT_TESTED` |

Across the eight samples, 18 of 26 enumerated slots have no named version
record; those slots remain unresolved, not presumed to be missing, empty, or
reserved. All 16 returned map definitions are unnamed. The 31 returned axes
have a missing unit or identifier; the parser emitted 16 corresponding
per-map warnings. No parser errors occurred in these eight parses. This
reproduces parser output; it does not establish that the parser found every
project version or map.

### Independent reference inventory

Three existing local manifests have artifacts for `100004.ols`, `100007.ols`,
and `100009.ols` (four, four, and two binary files respectively). They record
hashes and confirmed project identity, but the WinOLS version is unknown, the
exact export method/settings are unknown, and every artifact's individual
version/slot relation and representation are unknown. They do not contain
independent map metadata or map-value exports. In an isolated temporary
database, the eight samples were imported and these ten artifacts were
rechecked: all ten checks were `INCONCLUSIVE`; zero versions were independently
confirmed. No live SQLite database was opened or changed. The `100010054.ols`
reference package is only a template with no artifacts.

Phase 11C briefly added a second sample-diagnostics reference manifest. Phase
11D removes that parallel format and directs reference comparisons through the
existing WinOLS reference-package manifest and its inspection/version/map
workflow. No existing package meets the independent evidence gates, so the
eight-sample independent metadata/map/version validation remains
**`NOT_TESTED`**. No WinOLS automation or new exports were run.

### Phase 11C verification and limits

- The real eight-sample parser integration tests passed: 2 run, 0 failed,
  0 skipped. The damaged-file test also passed by explicitly reporting parser
  failure.
- The full suite ran 87 tests: 87 passed, 0 failed, 0 skipped. The Python
  unittest child process exit was `0`; the complete redirected log reports
  `Ran 87 tests` and `OK`. The PowerShell tool wrapper returned `1` for the
  same invocation, so its status is recorded separately rather than treated
  as the Python test result. An earlier `Tee-Object` attempt also produced a
  PowerShell native-stderr error and was not used as test evidence.
- `python -m compileall -q ols_platform deployment tests`: exit `0`.
- `node --check ols_platform\dashboard.js`: exit `0`.
- `git diff --check`: exit `0`.
- All eight source SHA-256 values were checked before and after the final test
  run; every post-test hash matches the value in the sample table.
- PID `45712` was observed running the existing continuous hash-worker
  command before this work. It was not stopped, restarted, paused, or
  reconfigured; the live index and queue were not queried or changed.
- Parser checkout files were not modified. No customer OLS/BIN/ORI files,
  databases, backups, caches, or environment folders were added to Git.

Phase 11 remains **in progress**. This phase delivers reproducible observation
and reference-test scaffolding; structural parser completeness, version/slot
relationships, and independent WinOLS validation remain unresolved.

## Phase 11D: independent WinOLS reference workflow

Phase 11D reuses the canonical `reference-package` manifest and CLI rather
than maintaining a second sample-diagnostics reference format. The packet
template and checklist capture the source OLS hash and size; WinOLS project
identity and OLS-to-project evidence; each export's hash, size, type,
representation, WinOLS version, export method/settings, project/version/slot
relationships, reviewed fields, reviewer evidence, coverage, and missing
evidence. The root `project_id` remains the local index ID; the WinOLS
project ID is separate and stays unknown until confirmed.

The workflow exposes `PASS`, `FAIL`, and `NOT_COMPARABLE` field results.
Project metadata JSON uses `{"metadata": {...}}`; only fields explicitly
reviewed against an independent WinOLS artifact are compared. Map rows require
an explicitly reviewed parser ordinal, not a name-based join; addresses are
compared only when the parser and export representations are declared
equivalent. Missing provenance, uncertain relations, hash/size mismatches, or
incomplete coverage cannot produce a general validation `PASS`. Package
integrity and `version_evidence_status` remain distinct from a passing binary
comparison and from ECU compatibility. Parser output is never accepted as an
independent reference.

### Existing reference inventory and required follow-up

No existing export package had enough provenance to independently validate a
version, parser metadata, or map definition. Existing manifests for
`100004.ols`, `100007.ols`, and `100009.ols` contain four, four, and two binary
artifacts respectively, but still lack confirmed WinOLS version/export
settings and individual artifact-to-version/slot and representation evidence.
`100010054.ols` has a template with no artifacts. No package was found for
`100008.ols`, `100010.ols`, `10001004.ols`, or `10001005.ols`. These are
inventory observations; no missing relationship was inferred from filenames
or hashes.

Next steps for the samples:

- For `100004.ols`, `100007.ols`, and `100009.ols`, confirm each existing
  export's source, WinOLS version/method/settings, representation, and exact
  project-version-slot relationship in WinOLS; add independent metadata and
  map exports where available.
- For `100008.ols`, `100010.ols`, `10001004.ols`, and `10001005.ols`, create
  packets from the canonical template and record independently confirmed
  project/version metadata and separate exports for each available version.
- For `100010054.ols`, complete its existing template with the same project,
  export, and reviewer evidence rather than creating a parallel manifest.
- For the map-bearing samples `100009.ols` and `100010054.ols`, independently
  confirm the map inventory, dimensions/axes, and address representation
  before running the map comparison. Parser axis warnings remain unresolved.

Until these exports and relations are supplied and reviewed, independent
validation for all eight samples is **`NOT_TESTED`**. No WinOLS automation or
new export was performed, and the parser samples remain read-only.

### Phase 11D verification and limits

- The targeted reference-package tests passed: 18 tests; the parser-diagnostic
  tests passed: 2 tests.
- The complete configured suite ran 91 tests: 91 passed, 0 failed, 0 skipped.
  The two real-parser tests ran, including the import of all eight OLS samples
  into a temporary database and the damaged-file failure case. The captured
  unittest output ends with `Ran 91 tests` and `OK`; the Python child exit was
  `0`.
- The PowerShell tool wrapper reported exit `1` for the full-suite invocation
  even though the captured child exit was `0` and the complete log reports
  success. The two statuses are recorded separately; no test failure was
  suppressed or reclassified. A separate launch path stalled at the final
  worker pause/resume test and was not used as evidence.
- The eight source sample SHA-256 values were checked before and after the
  completed run; all remained equal to the Phase 11C table above.
- PID `45712` remained the existing Python continuous hash worker. Its process
  and command line were checked read-only; it remained responsive and was not
  stopped, restarted, paused, or reconfigured. The live index and queue were
  not opened or changed by this work.
- Tests used temporary databases and synthetic fixtures. No OLS, BIN, ORI,
  SQLite, backup, cache, or environment files were added to Git.

Phase 11D completes the reproducible reference-workflow implementation and
regression coverage, not the underlying independent data validation. Phase 11
remains **in progress** until the required WinOLS exports and structural
version/map relationships are independently confirmed.

## Phase 11E independent parser audit

Read-only comparisons of `py-ols` and `jinglinsen/ols-parser` covered all eight samples; parser outputs remain observations, not ground truth. The audit and machine-readable report are in [PHASE11E_STATUS.md](./PHASE11E_STATUS.md) and [reports/phase11e_parser_audit.json](./reports/phase11e_parser_audit.json). The full suite ran 105 tests (103 passed, 0 failed, 2 environment-dependent skips); the two real-parser tests passed separately. All sample hashes remained unchanged. Independent WinOLS validation is `NOT_TESTED`; Phase 11 remains in progress.
