"""Tune without a similar original: what independent tunes of the ECU agree on.

Transfer needs one donor whose software resembles the input. This module
needs none: every changed map the library knows for the ECU (``maps``) is
searched in the input through its axes. A map is only changed when several
*independent* originals point at the same place with the same shape, move it
in the same direction, and its present values are plausible for that map.
Each cell then gets the median relative change of those examples.

The result is a *proposal to review in WinOLS*, never a flash file: measured
on hidden real tunes it finds the right places and direction, but only part
of what a complete tune changes, and a partial tune is unsafe to write.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import statistics
import struct
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .maps import MAPS_DIR, load_all, uses_map_knowledge

INDEX_DIR = "families"
MIN_VOTERS = 4               # independent originals that must agree on a map
MIN_SHARE = 0.25             # ... as a share of all originals found to have maps in the input
MIN_MAPS = 3                 # measured: proposals of 1-2 maps are right in direction only 38-75 %
MIN_DIRECTION = 0.80         # agreement on up/down per map
MIN_ANCHOR_BYTES = 16        # axis bytes that must match to trust a place
VALUE_WINDOW = 0.35          # median deviation of present values from an example's original
MIN_RATIO, MAX_RATIO = 0.60, 1.60   # a cell never changes more than this
NO_FAMILY = {"", "ONBEKEND", "UNKNOWN"}


def family_label(family_key: str) -> str:
    return family_key.rpartition("|")[0].upper()


def eligible_profile(profile: str) -> bool:
    """Performance tunes only. ``other`` is unnamed and may hold anything."""

    return profile not in ("other", "original") and uses_map_knowledge(profile)


def _cells(blob: bytes, width: int, endian: str) -> tuple[int, ...]:
    if width == 1:
        return tuple(blob)
    return struct.unpack(f"{'<' if endian == 'little' else '>'}{len(blob) // 2}H", blob)


def _endian(blob: bytes, columns: int) -> str:
    """Byte order in which the table is smoother along its rows."""

    def rough(endian: str) -> float:
        cells = _cells(blob, 2, endian)
        steps = [abs(a - b) for i, (a, b) in enumerate(zip(cells, cells[1:])) if (i + 1) % max(columns, 1)]
        spread = max(cells) - min(cells) if cells else 0
        return statistics.fmean(steps) / spread if steps and spread else 0.0

    return "little" if rough("little") < rough("big") else "big"


# ----- index ----------------------------------------------------------------

STRUCT_DIR = "structuur2"  # small files of an earlier version, taken over once
STRUCT_VERSION = 2         # bump when structural_records finds more


def _index_path(root: Path, label: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)
    return root / MAPS_DIR / INDEX_DIR / f"{safe}.json.gz"


def _struct_path(root: Path, pair_id: str) -> Path:
    return root / MAPS_DIR / STRUCT_DIR / pair_id[:2] / f"{pair_id}.json.gz"


def _write_gz(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # several processes may write the same example
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=4) as handle:
        json.dump(value, handle, separators=(",", ":"))
    tmp.replace(path)


def _struct_db(root: Path) -> Any:
    """All structural records in one file: hundreds of thousands of small
    files take hours to read back from a hard disk."""

    import sqlite3

    path = root / MAPS_DIR / "structuur.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=120)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("CREATE TABLE IF NOT EXISTS structuur (pair_id TEXT NOT NULL, version INTEGER NOT NULL, "
               "records BLOB NOT NULL, PRIMARY KEY (pair_id, version))")
    return db


def _pack(records: list[dict[str, Any]]) -> bytes:
    return zlib.compress(json.dumps(records, separators=(",", ":")).encode(), 4)


def _save_structures(root: Path, rows: list[tuple[str, bytes]]) -> None:
    db = _struct_db(root)
    try:
        with db:
            db.executemany("INSERT OR IGNORE INTO structuur VALUES (?,?,?)",
                           [(pair_id, STRUCT_VERSION, blob) for pair_id, blob in rows])
    finally:
        db.close()


def store_structure(root: Path, pair: Any) -> int:
    """Write the structural records of a loaded pair (once). -1: not eligible."""

    if not eligible_profile(pair.profile) or family_label(pair.family_key) in NO_FAMILY:
        return -1
    records = structural_records(pair.original, pair.tuned)
    _save_structures(root, [(pair.pair_id, _pack(records))])
    return len(records)


def _stored_structures(root: Path) -> set[str]:
    db = _struct_db(root)
    try:
        return {r[0] for r in db.execute("SELECT pair_id FROM structuur WHERE version=?", (STRUCT_VERSION,))}
    finally:
        db.close()


def _learn_pair(task: tuple[str, str]) -> tuple[str, bytes] | None:
    root, pair_id = Path(task[0]), task[1]
    old = _struct_path(root, pair_id)
    try:
        if old.exists():  # written as a small file by an earlier version
            with gzip.open(old, "rt", encoding="utf-8") as handle:
                return pair_id, _pack(json.load(handle))
        from .library import load_pair

        pair = load_pair(root, pair_id)
        return pair_id, _pack(structural_records(pair.original, pair.tuned))
    except (OSError, EOFError, ValueError, KeyError):
        return None


def _pairs(root: Path) -> list[tuple]:
    import sqlite3

    db = sqlite3.connect(f"file:{root / 'library.sqlite3'}?mode=ro", uri=True, timeout=60)
    rows = db.execute("SELECT pair_id, original_sha256, tuned_sha256, profile, family_key, variant_key, "
                      "project_sha256 FROM pairs").fetchall()
    db.close()
    return [r for r in rows if eligible_profile(r[3]) and family_label(r[4]) not in NO_FAMILY]


def base_profile(profile: str) -> str:
    """Stage of a combined tune; extras are outvoted by the plain ones."""

    return next((part for part in profile.split("+") if part.startswith("stage")), profile)


def learn_structure(root: Path, workers: int = 4, progress=print, stop=None) -> int:
    """Read the changed tables of every eligible example (resumable), then
    rebuild the per-ECU index."""

    from concurrent.futures import ProcessPoolExecutor

    have = _stored_structures(root)
    todo = [(str(root), r[0]) for r in _pairs(root) if r[0] not in have]
    progress(f"Kaartstructuur: {len(todo)} voorbeelden te lezen, {workers} tegelijk.")
    done = 0
    if todo:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for begin in range(0, len(todo), 400):
                if stop is not None and stop():
                    return done
                rows = [x for x in pool.map(_learn_pair, todo[begin:begin + 400], chunksize=10) if x]
                _save_structures(root, rows)
                done += len(rows)
                progress(f"Kaartstructuur: {min(begin + 400, len(todo))}/{len(todo)} voorbeelden.")
    build_index(root, progress)
    return done


def build_index(root: Path, progress=print) -> dict[str, int]:
    """Group structural and defined map records per ECU label (one file each)."""

    labels: dict[str, list[tuple]] = defaultdict(list)
    for row in _pairs(root):
        labels[family_label(row[4])].append(row)
    stats = {"families": 0, "tunes": 0, "maps": 0}
    defined = {p.name.split(".")[0] for p in (root / MAPS_DIR).glob("??/*.json.gz")}
    struct_db = _struct_db(root)
    for label, rows in labels.items():
        entries = []
        for pair_id, original, tuned, profile, _family, variant, project in rows:
            found = struct_db.execute("SELECT records FROM structuur WHERE pair_id=? AND version=?",
                                      (pair_id, STRUCT_VERSION)).fetchone()
            records: list[dict[str, Any]] = json.loads(zlib.decompress(found[0])) if found else []
            spans = [(r["start"], r["start"] + len(r["orig"]) // 2) for r in records]
            for record in load_all(root, original).get(tuned, []) if original in defined else ():
                s, e = record["start"], record["start"] + len(record["orig"]) // 2
                if not any(s < y and x < e for x, y in spans):
                    records.append(record)
            if records:
                entries.append({"original": original, "tuned": tuned, "profile": base_profile(profile),
                                "variant": variant, "project": project, "records": records})
        if entries:
            _write_gz(_index_path(root, label), {"label": label, "entries": entries})
            stats["families"] += 1
            stats["tunes"] += len(entries)
            stats["maps"] += sum(len(e["records"]) for e in entries)
    struct_db.close()
    progress(f"Kaartindex: {stats['maps']} kaarten van {stats['tunes']} tunes, {stats['families']} ECU-types.")
    return stats


def load_index(root: Path, label: str) -> list[dict[str, Any]]:
    path = _index_path(root, label.upper())
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    return data["entries"] if data.get("label") == label.upper() else []


# ----- locating -------------------------------------------------------------

class _Finder:
    """Unique positions of axis patterns in one input (cached)."""

    def __init__(self, data: bytes):
        self.data = data
        self.cache: dict[bytes, list[int]] = {}

    def hits(self, pattern: bytes) -> list[int]:
        found = self.cache.get(pattern)
        if found is None:
            found, pos = [], self.data.find(pattern)
            while pos != -1 and len(found) < 64:
                found.append(pos)
                pos = self.data.find(pattern, pos + 1)
            self.cache[pattern] = found
        return found

    def place(self, record: dict[str, Any]) -> list[int]:
        axes = sorted(((bytes.fromhex(a["bytes"]), a["rel"]) for a in record["axes"]), key=lambda a: -len(a[0]))
        if sum(len(a) for a, _ in axes) < MIN_ANCHOR_BYTES or len(set(axes[0][0])) < 3:
            return []
        size = len(record["orig"]) // 2
        places = []
        for hit in self.hits(axes[0][0]):
            start = hit - axes[0][1]
            if start < 0 or start + size > len(self.data):
                continue
            if all(self.data[start + rel:start + rel + len(b)] == b for b, rel in axes[1:]):
                places.append(start)
        return places


def _plausible(current: tuple[int, ...], orig: tuple[int, ...], tuned: tuple[int, ...]) -> float | None:
    """Median relative deviation of the present values from the example's
    original; None when it looks like another map or like the tuned values."""

    to_orig = [abs(c - o) / max(o, 1) for c, o in zip(current, orig)]
    deviation = statistics.median(to_orig)
    if deviation > VALUE_WINDOW:
        return None
    changed = [(c, o, t) for c, o, t in zip(current, orig, tuned) if o != t]
    if changed and sum(abs(c - t) < abs(c - o) for c, o, t in changed) > 0.5 * len(changed):
        return None
    return deviation


def vote(data: bytes, entries: Iterable[dict[str, Any]], profile: str, *,
         exclude: set[str] = frozenset()) -> tuple[dict[tuple[int, int, int], list[dict[str, Any]]], set[str]]:
    """Places in ``data`` that examples point at: (start, size, width) -> votes."""

    finder = _Finder(data)
    places: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    contributors: set[str] = set()
    for entry in entries:
        if entry["profile"] != profile or entry["original"] in exclude:
            continue
        for record in entry["records"]:
            candidates = finder.place(record)
            if not candidates:
                continue
            orig_b, tuned_b = bytes.fromhex(record["orig"]), bytes.fromhex(record["tuned"])
            width = record["width"]
            if width == 2 and len(orig_b) % 2:
                continue
            endian = record.get("endian") or (_endian(orig_b, record["columns"]) if width == 2 else "big")
            orig, tuned = _cells(orig_b, width, endian), _cells(tuned_b, width, endian)
            fits = []
            for start in candidates:
                current = _cells(data[start:start + len(orig_b)], width, endian)
                deviation = _plausible(current, orig, tuned)
                if deviation is not None:
                    fits.append((deviation, start))
            fits.sort()
            # Several equally good places: not decidable, no vote.
            if not fits or (len(fits) > 1 and fits[1][0] - fits[0][0] < 0.05):
                continue
            start = fits[0][1]
            contributors.add(entry["original"])
            places[(start, len(orig_b), width)].append({
                "original": entry["original"], "tuned": entry["tuned"], "project": entry["project"],
                "endian": endian, "name": record["name"], "columns": record["columns"], "rows": record["rows"],
                "ratios": [t / o if o else None for o, t in zip(orig, tuned)],
            })
    return places, contributors


def propose(data: bytes, entries: Iterable[dict[str, Any]], profile: str, *,
            exclude: set[str] = frozenset(), min_voters: int = MIN_VOTERS,
            min_share: float = MIN_SHARE) -> dict[str, Any]:
    """Consensus change set for ``data``: output, changed maps and why."""

    places, contributors = vote(data, entries, profile, exclude=exclude)
    output = bytearray(data)
    accepted: list[dict[str, Any]] = []
    candidates = []
    for (start, size, width), votes in places.items():
        # One vote per original: several tunes of one file are not independent.
        per_original: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for v in votes:
            per_original[v["original"]].append(v)
        voters = len(per_original)
        projects = len({v["project"] for v in votes})
        if min(voters, projects) < min_voters or voters / max(len(contributors), 1) < min_share:
            continue
        endians = [v["endian"] for v in votes]
        endian = max(set(endians), key=endians.count)
        merged = []  # per original: median ratio per cell over its tunes
        for group in per_original.values():
            rows = [v["ratios"] for v in group if v["endian"] == endian]
            if rows:
                merged.append([statistics.median([r for r in col if r is not None]) if any(
                    r is not None for r in col) else None for col in zip(*rows)])
        if len(merged) < min_voters:
            continue
        signs = []
        for row in merged:
            moved = [r for r in row if r is not None and r != 1]
            if moved:
                signs.append(1 if statistics.median(moved) > 1 else -1)
        if not signs or max(signs.count(1), signs.count(-1)) / len(signs) < MIN_DIRECTION:
            continue
        candidates.append((voters, start, size, width, endian, merged, votes[0]))
    candidates.sort(key=lambda c: (-c[0], -c[2], c[1]))
    taken: list[tuple[int, int]] = []
    for voters, start, size, width, endian, merged, sample in candidates:
        if any(start < e and s < start + size for s, e in taken):
            continue
        current = _cells(data[start:start + size], width, endian)
        top = 0xFF if width == 1 else 0xFFFF
        new = []
        for i, value in enumerate(current):
            ratios = [row[i] for row in merged if row[i] is not None]
            ratio = statistics.median(ratios) if len(ratios) >= min_voters else 1.0
            ratio = min(MAX_RATIO, max(MIN_RATIO, ratio))
            new.append(max(0, min(top, round(value * ratio))))
        if list(current) == new:
            continue
        packed = bytes(new) if width == 1 else struct.pack(
            f"{'<' if endian == 'little' else '>'}{len(new)}H", *new)
        output[start:start + size] = packed
        taken.append((start, start + size))
        moved = [n / c for n, c in zip(new, current) if c and n != c]
        accepted.append({"start": start, "end": start + size, "voters": voters, "name": sample["name"],
                         "columns": sample["columns"], "rows": sample["rows"],
                         "median_change": (statistics.median(moved) - 1) if moved else 0.0,
                         "changed_bytes": sum(a != b for a, b in zip(packed, data[start:start + size]))})
    accepted.sort(key=lambda a: a["start"])
    return {"output": bytes(output), "maps": accepted, "contributors": len(contributors)}


# ----- structure: maps recognised without definitions -----------------------

HEADER_SEARCH = 0xA00        # how far before a change its map header may lie
MERGE_GAP = 8
MIN_LINE = 7                 # shorter curves have too little axis to be recognised


def _rising(values: tuple[int, ...]) -> bool:
    return all(a < b for a, b in zip(values, values[1:]))


def _header_at(data: bytes, at: int, fmt: str) -> tuple[int, int, int] | None:
    """(columns, rows, data start) when a Bosch table header lies at ``at``:
    two dimensions, two strictly rising axes (signed or not), then the cells."""

    nx, ny = struct.unpack_from(f"{fmt}2H", data, at)
    if not (3 <= nx <= 32 and 3 <= ny <= 32) or nx * ny < 12:
        return None
    start = at + 4 + 2 * (nx + ny)
    if start + 2 * nx * ny > len(data):
        return None
    for kind in "Hh":
        axes = struct.unpack_from(f"{fmt}{nx + ny}{kind}", data, at + 4)
        if _rising(axes[:nx]) and _rising(axes[nx:]):
            return nx, ny, start
    return None


def _line_at(data: bytes, at: int, fmt: str) -> tuple[int, int, int] | None:
    """The same for a curve: one dimension, one rising axis, then the cells."""

    (nx,) = struct.unpack_from(f"{fmt}H", data, at)
    start = at + 2 + 2 * nx
    if not MIN_LINE <= nx <= 32 or start + 2 * nx > len(data):
        return None
    for kind in "Hh":
        if _rising(struct.unpack_from(f"{fmt}{nx}{kind}", data, at + 2)):
            return nx, 1, start
    return None


def _find_table(data: bytes, pos: int) -> tuple[int, str, int, int, int] | None:
    """Nearest table before ``pos`` whose cells contain ``pos``."""

    for at in range(pos - 4 - pos % 2, max(-1, pos - HEADER_SEARCH), -2):
        if at < 0:
            break
        if data[at] and data[at + 1]:
            continue  # a dimension word has one zero byte
        for fmt in "<>":
            found = _header_at(data, at, fmt) or (_line_at(data, at, fmt) if pos - at < 0x84 else None)
            if found and found[2] <= pos < found[2] + 2 * found[0] * found[1]:
                return (at, fmt, *found)
    return None


def structural_records(original: bytes, tuned: bytes) -> list[dict[str, Any]]:
    """Records of changed tables found by their own layout in the original."""

    from .learn import diff_regions

    if len(original) != len(tuned):
        return []
    records: dict[int, dict[str, Any]] = {}
    covered_to = -1
    # Small gap: neighbouring tables are often changed together, and each
    # change run beyond the table found so far is looked up on its own.
    for start, end in diff_regions(original, tuned, MERGE_GAP):
        pos = max(start, covered_to)
        while pos < end:
            if original[pos] == tuned[pos]:
                pos += 1
                continue
            found = _find_table(original, pos)
            if found is None:
                break
            at, fmt, nx, ny, data_start = found
            data_end = covered_to = data_start + 2 * nx * ny
            if tuned[at:data_start] == original[at:data_start]:  # else the axes were rescaled
                records[data_start] = {
                    "name": "", "start": data_start, "width": 2, "columns": nx, "rows": ny,
                    "endian": "little" if fmt == "<" else "big",
                    "orig": original[data_start:data_end].hex(), "tuned": tuned[data_start:data_end].hex(),
                    "axes": [{"rel": at - data_start, "bytes": original[at:data_start].hex()}],
                }
            pos = max(pos + 1, data_end)
    return list(records.values())


# ----- proposal -------------------------------------------------------------

def detect_labels(root: Path, data: bytes, file_name: str = "") -> list[str]:
    """Indexed ECU labels named in the file name (as AutoTuner does) or in
    the file itself; file name first, longest first."""

    from .identity import token_in

    parts = {part.upper() for part in re.split(r"[\s_()]+", file_name)}
    found = []
    for path in (root / MAPS_DIR / INDEX_DIR).glob("*.json.gz"):
        label = path.name[:-len(".json.gz")]
        if len(label) >= 5 and any(c.isdigit() for c in label) and (
                label in parts or token_in(label, data)):
            found.append(label)
    return sorted(found, key=lambda x: (x not in parts, -len(x), x))


def write_proposal(data: bytes, root: Path, profile: str, output: Path, *, label: str | None = None,
                   source_name: str = "") -> dict[str, Any]:
    """Write the proposal as CSV and JSON next to ``output`` (no binary)."""

    import csv
    import hashlib

    if not eligible_profile(profile):
        raise ValueError("Voor dit type tuning wordt geen kaartvoorstel gemaakt.")
    profile = base_profile(profile)
    entries: list[dict[str, Any]] = []
    for candidate in ([label.upper()] if label else detect_labels(root, data, source_name)):
        entries = load_index(root, candidate)
        if entries:
            label = candidate
            break
    if not entries:
        raise ValueError("Geen kaartkennis voor deze ECU, of het ECU-type staat niet in het bestand.")
    result = propose(data, entries, profile)
    if len(result["maps"]) < MIN_MAPS:
        raise ValueError(f"Te weinig overeenstemming voor een voorstel ({len(result['maps'])} kaarten, "
                         f"minimaal {MIN_MAPS}). Meer projecten van deze ECU zijn nodig.")
    rows = [{"adres": f"0x{m['start']:X}", "einde": f"0x{m['end']:X}", "kolommen": m["columns"], "rijen": m["rows"],
             "naam": m["name"], "voorbeelden": m["voters"], "verandering": round(m["median_change"], 4)}
            for m in result["maps"]]
    report = {"soort": "KAARTVOORSTEL - geen complete tune, niet flashen", "bestand": source_name,
              "sha256": hashlib.sha256(data).hexdigest(), "ecu": label, "tuning": profile,
              "voorbeelden_gevonden": result["contributors"], "kaarten": rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = output.with_suffix(".json"), output.with_suffix(".csv")
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, ["adres", "einde", "kolommen", "rijen", "naam", "voorbeelden", "verandering"],
                                delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    return dict(report, csv=str(csv_path), json=str(json_path))
