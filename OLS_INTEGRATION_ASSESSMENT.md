# WinOLS OLS integration assessment

Assessment date: 2026-11-10

## Outcome

The eight supplied `.ols` files were parsed with both `OLSReader.parse()` and
`read_ols()` from the cloned `TheFlashBold/py-ols` repository. The parser is not
reliable enough to serve as an authoritative project importer: it finds only
one named binary version per file even though its own records advertise 2–4
slots; six files yield no map definitions; and the 16 maps/curve found in the
other two have no names or axis labels/units. It parses some structural and
metadata fields, but does not decode map values.

No application source, database, schema, or database-indexing code is present
in the working directory; it contains only the eight input files. Therefore
there was no application to integrate an adapter into and no existing database
or original-versus-tuned engine to continue. No database import was performed.

## Source review

The repository was cloned to `C:\Users\Bugra\Desktop\py-ols-study`. Reviewed
upstream commit: `4c283a80161e130b03eb8743c752a689ecda6b78`
(`more parsing fixes`, 2026-04-29). The implementation is concentrated in
[`ols_reader.py`](../py-ols-study/ols_reader.py); the README is a feature
summary, not validation evidence.

- `OLSReader.__init__` reads the entire file into memory with `Path.read_bytes`.
  Parsing and extraction also allocate slices/copies. There is no bounded
  streaming, worker pool, progress checkpoint, resume, or resource-limit API.
- `parse()` calls `_parse_header()` but ignores its Boolean result. The header
  reader checks the magic/signature, reads up to 15 length-prefixed metadata
  strings, and applies simple length checks; malformed/non-OLS input is not
  reliably surfaced as a parse failure.
- `OLSFile` contains vehicle/ECU metadata, parameters, binary versions, one
  optional CAL block, folders, and KP/endian flags. It does not model project
  identity, complete version history, per-version map/value relationships,
  checksums, or provenance as first-class validated relationships.
- `BinaryVersion` records a name, description, blob offset/size, index/count,
  and optional EPK. `_extract_binary_versions()` scans a limited header range
  for marker `0x42007899`, assumes a shared record layout, and derives offsets
  using `blob_offset + index * blob_size`. It scans only a limited region for
  derived records and caps the record's slots at 20. The eight samples
  demonstrate that the advertised slot count is not the number of named
  versions returned.
- `extract_binary()` strips four bytes unconditionally and returns bytes; it
  does not validate the stripped marker or checksum. In these samples the
  returned size is consistently `blob_size - 4`, but the marker values vary
  (including `ba77dc9e` and `ffffffff`). This establishes the parser's output
  shape, not that those payloads match WinOLS exports.
- `_find_binary_verification()` searches known hard-coded CAL offsets and then
  searches the whole file for a CAS/EPK pattern. This is a heuristic; a found
  EPK is not a cryptographic or structural validation of the extracted
  version.
- The parameter parser searches for marker `0x11883377` only within the first
  `0x20000` bytes, optionally decompresses the remaining data, then reads
  records sequentially with version-number feature gates. It derives
  VALUE/CURVE/MAP, dimensions and conversion fields. `Parameter` exposes
  factors, offsets, data type, dimensions, and two axes, but does not contain
  decoded values. An EOF/value/struct error stops parsing and returns a
  partial list without an incomplete-record diagnostic.
- `AxisInfo.address` is the parsed axis address; `offset` and `factor` are
  conversion values. Axis units, IDs, and descriptions are not populated by
  the current parser in the maps observed here. Axis point counts are assigned
  from parameter dimensions rather than read from the axis block.
- KP detection requires a v597+ header and a ZIP containing `intern` at a
  fixed search range. KP records are found using heuristic metadata/name
  patterns, guessed address windows, and guessed dimensions. The parser
  returns no embedded binary versions/CAL for KP files; no `.kp` was supplied
  to validate this path.
- CAL extraction uses a CAS regex, a maximum-size window, an FF-run heuristic,
  and alignment. Folder extraction scans for a byte pattern from one-third of
  the file. Neither strategy has been independently validated here.
- README claims support spanning versions 100–834+, intermediate/standard
  layouts, multi-version projects, KP, and WinOLS 5. The supplied projects
  exercise only format versions 503 and 597. Other format generations remain
  unverified.

## Real sample parses

“Slots declared” is `BinaryVersion.version_count` in the one record the parser
found. “Returned” is the count of `BinaryVersion` objects it actually
produced. Every returned entry was named `Original`; its slot index is shown.
Descriptions were import/provenance strings for six samples and empty for
`100010.ols` and `100010054.ols`. The names/descriptions for the other slots
were not recovered.

| File | Format | Source bytes | Parsed definitions | Axes | Versions returned / slots declared | Extracted bytes |
|---|---:|---:|---|---:|---:|---:|
| `100004.ols` | 597 | 23,081,851 | 0 | 0 | Original, slot 3; 1 / 4 | 5,767,164 |
| `100007.ols` | 597 | 8,538,495 | 0 | 0 | Original, slot 3; 1 / 4 | 2,097,148 |
| `100008.ols` | 597 | 12,869,126 | 0 | 0 | Original, slot 2; 1 / 3 | 4,194,300 |
| `100009.ols` | 597 | 6,067,756 | 4 MAP | 8 | Original, slot 1; 1 / 2 | 2,621,436 |
| `100010.ols` | 597 | 12,886,631 | 0 | 0 | Original, slot 2; 1 / 3 | 4,194,300 |
| `10001004.ols` | 503 | 13,198,494 | 0 | 0 | Original, slot 2; 1 / 3 | 4,194,300 |
| `10001005.ols` | 503 | 26,187,364 | 0 | 0 | Original, slot 2; 1 / 3 | 8,388,604 |
| `100010054.ols` | 503 | 17,457,135 | 11 MAP, 1 CURVE | 23 | Original, slot 3; 1 / 4 | 4,194,300 |

All eight were recognized by the reader and returned a nonzero header version.
Metadata read (make/model; engine/year; ECU maker/type; SW number):

- `100004.ols`: VW/Golf; 2.0 TSI/2017; Siemens/Continental/SIMOS18.10;
  5G0906259N.
- `100007.ols`: Peugeot/307; 2.0 HDi 16V/2011;
  Siemens/Continental/(empty); (empty).
- `100008.ols`: (empty)/(empty); (empty)/(empty);
  Siemens/Continental/SID209; (empty).
- `100009.ols`: BMW/7er; 50D/(empty); Bosch/MDG1_MD1CP002;
  `B57D30S0-G1xB57S0_RDE#S15A-17-07-504-FS (O_8KTB1-000047D1-028)`.
- `100010.ols`: MB/C220; 2.2 CDI R4/2011; Delphi/CRD3;
  `CRD3-651-WMA4HD11-204WM-125kW-4CSL2S-EU5OP_AJ122_LH-EF-DS04_ME185001`.
- `10001004.ols`: Audi/RS6; 4.0 TFSI V8/2017; Bosch/MED17.1.1;
  4G0906560F.
- `10001005.ols`: MB/GT-S AMG; 4.0 AMG V8 Biturbo/2016; Bosch/MED17.7.5;
  `8Y35210H0830000 1201`.
- `100010054.ols`: Audi/A8; 4.2 TDI CR V8/2012; Bosch/EDC17CP44;
  4H0907409B.

The reader also returns fields for year, fuel, displacement, power, gearbox,
memory type, HW number/code, and user. Empty-field observations: `100007`
misses gearbox, ECU type, SW and HW number; `100008` misses make, model,
engine, year, fuel, displacement, power, gearbox, SW/HW numbers and HW code;
`100009` misses year and gearbox; `10001005` misses HW number. The other
reported metadata fields in the eight parses were non-empty.

For all eight returned binaries, extraction length was exactly four bytes
less than the recorded blob size. Parser-output SHA-256 values (not compared
with independent reference exports):

| File | Extracted payload SHA-256 |
|---|---|
| `100004.ols` | `994ee0936b51b75754349b5784cead004d6c8c9081bae9c82ea35f3e0327a677` |
| `100007.ols` | `a2d0878564ab687df0c0f46dc4af0bf24d383f17eb3197a3a3925087715f3d16` |
| `100008.ols` | `5b48cfb53ce027fd513c970b88d4a920694b435391b07ea1dd3c470984961171` |
| `100009.ols` | `ccbbbba613b671e683eed6a73de6b28d40d31ac09501e393b2586ea1107d4503` |
| `100010.ols` | `010ddd88d1de5e7712e77e2d9037d293402b071d5a8284c49bd03522d4059121` |
| `10001004.ols` | `968e8ff89ab327bcb17640cef7bbc7bf50ca4e4b87f0d9d8ed7cb0986ce03a43` |
| `10001005.ols` | `ab418bcdc382a650e188dfaa1867167dc357e982d062fef3e3900cc108f7501d` |
| `100010054.ols` | `012bba69695ed2352f4f28ce2962421189e1fb9408af67397730ff4859031505` |

For the 16 definitions that were returned, all parameter names were empty.
Descriptions were generic (`Kennfeld "Bosch II 16"` or `Kennfeld`); all 31
axes had empty IDs, descriptions, and units. Parameter conversion values were
the default `factor=1, offset=0`; axis factors/offsets were also `1/0`.
Dimensions and inferred value extents fell within the extracted payloads for
these two files, but no parameter or axis values were decoded or checked
against an independent source. One axis was typed `FLOAT32`; the other 30 were
typed `UWORD`. The parser detected one CAL block (2,100,357 bytes) in
`100004.ols` and no folders in any sample. No thrown parser error occurred
during these API reads; silent omissions and empty fields above are incomplete
results, not proof the source projects lack those items.

## Issues, regression, and license

The GitHub repository has two open issues:

- [#1 Object Type Error](https://github.com/TheFlashBold/py-ols/issues/1):
  the CLI calls `hex(p.x_axis.offset)`, although `offset` is a float
  conversion value. Reproduced on `100010054.ols` with the reported
  `TypeError`. The local clone now formats `AxisInfo.address` instead and has
  a regression test in `tests/test_cli.py`; the test passes and the CLI
  completes on that real sample.
- [#2 dependencies ?](https://github.com/TheFlashBold/py-ols/issues/2): open
  with no description/body or comments, so there is no actionable failure to
  reproduce. The inspected parser imports only Python standard-library
  modules; the repository has no dependency manifest or existing test suite.

The repository includes the GNU GPL version 3 license. Shipping this parser
inside a distributed product can create copyleft/source-distribution
obligations for the combined work; modifications to the parser also remain
under GPLv3. This needs legal review before distribution. Process isolation or
an export-based adapter may reduce coupling but does not itself settle the
license question.

## Independent reference and remaining blockers

WinOLS 5.80 is installed, but this unattended session could not enable desktop
automation. No WinOLS exports were generated. Consequently there is no
independent comparison of metadata, all version names/counts, binary sizes,
offsets, checksums, marker handling, or hashes. The parser's one-found-versus-
multiple-declared slots is a specific release blocker; the hashes above are
not correctness evidence.

No application/database files are in the workspace, so an internal
`OlsProjectReader` adapter, idempotent database schema/import, 6-TB bounded
indexer, resume/checkpoint behavior, and original-versus-tuned comparison
engine could not be implemented or tested here. Do not treat this assessment
or the local CLI fix as a completed integration. The app repository and
independent WinOLS exports are required before continuing those changes or
claiming the definition of done.
