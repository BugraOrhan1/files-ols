"""Map knowledge: find a tuned map in other software by its axes.

Byte transfer (``transfer.place_edits``) finds a donor's change through the
identical bytes around it. In other software of the same ECU the map values
and surroundings often differ, while the map's *axes* (engine speed, load,
pressure breakpoints) stay the same. WinOLS projects with a map pack or
damos carry each map's data address, size, cell type and axis addresses.

For every changed map of a training pair a small record is kept: the
original and tuned map bytes and the original axis bytes with their
position relative to the map data. In a new file the map is located by
searching those axis bytes (all axes must sit at the same relative
positions), and the donor's per-cell change is applied to this file's own
values with the same plausibility check as other recalculated maps.

Nothing is invented: a map is only written when all its adjacent axes are
found and every changed cell lies within the donor's value tolerance.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Iterable

CELL_BYTES = {"UBYTE": 1, "SBYTE": 1, "UWORD": 2, "SWORD": 2}
MAX_MAP_BYTES = 0x4000      # larger "maps" are code or tables WinOLS shows as one block
MAX_AXIS_DISTANCE = 0x400   # an axis this close to its data moves with it (Bosch map layout)
MIN_AXIS_BYTES = 6          # shorter axes are too common to locate anything
MAX_HITS = 64               # occurrences of one axis that are checked
MAPS_DIR = "maps"


def _ranges_overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b1 and b0 < a1


def changed_regions(original: bytes, tuned: bytes) -> list[tuple[int, int]]:
    regions = []
    start = None
    for i, (a, b) in enumerate(zip(original, tuned)):
        if a != b:
            if start is None:
                start = i
        elif start is not None:
            regions.append((start, i))
            start = None
    if start is not None:
        regions.append((start, len(original)))
    return regions


def map_records(maps: Iterable[dict[str, Any]], original: bytes, tuned: bytes,
                regions: list[tuple[int, int]] | None = None) -> list[dict[str, Any]]:
    """Records of the maps a tune changed, located by adjacent axes."""

    regions = changed_regions(original, tuned) if regions is None else regions
    if not regions:
        return []
    records = []
    seen: set[tuple[int, int]] = set()
    for item in maps:
        width = CELL_BYTES.get(str(item.get("data_type", "")).upper())
        start = item.get("data_offset")
        if not width or start is None:
            continue
        size = int(item.get("columns") or 0) * int(item.get("rows") or 0) * width
        end = start + size
        if size <= 0 or size > MAX_MAP_BYTES or end > len(original) or (start, end) in seen:
            continue
        if not any(_ranges_overlap(start, end, r0, r1) for r0, r1 in regions):
            continue
        orig, new = original[start:end], tuned[start:end]
        if orig == new:
            continue
        axes = []
        for axis in item.get("axes") or ():
            a_width = CELL_BYTES.get(str(axis.get("data_type", "")).upper())
            address, points = axis.get("address"), int(axis.get("points") or 0)
            if not a_width or address is None or points < 2:
                continue
            a_bytes = original[address:address + points * a_width]
            if len(a_bytes) < MIN_AXIS_BYTES or abs(address - start) > MAX_AXIS_DISTANCE:
                continue
            if tuned[address:address + len(a_bytes)] != a_bytes:
                continue  # the tune rescaled this axis: not a stable anchor
            axes.append({"rel": address - start, "bytes": a_bytes.hex()})
        if not axes:
            continue
        seen.add((start, end))
        records.append({
            "name": str(item.get("description") or "")[:120],
            "start": start, "width": width,
            "columns": int(item["columns"]), "rows": int(item["rows"]),
            "orig": orig.hex(), "tuned": new.hex(), "axes": axes,
        })
    return records


def _occurrences(data: bytes, pattern: bytes) -> list[int]:
    hits = []
    pos = data.find(pattern)
    while pos != -1 and len(hits) < MAX_HITS:
        hits.append(pos)
        pos = data.find(pattern, pos + 1)
    return hits


def locate(data: bytes, record: dict[str, Any], near: int | None = None) -> int | None:
    """Data offset of the record's map in ``data``: every axis found at its
    relative position. With several candidates the one nearest ``near``
    (the donor offset plus the shift of its neighbours) wins."""

    axes = sorted(record["axes"], key=lambda a: -len(a["bytes"]))
    size = len(record["orig"]) // 2
    first = bytes.fromhex(axes[0]["bytes"])
    candidates = []
    for hit in _occurrences(data, first):
        start = hit - axes[0]["rel"]
        if start < 0 or start + size > len(data):
            continue
        ok = True
        for axis in axes[1:]:
            pattern = bytes.fromhex(axis["bytes"])
            at = start + axis["rel"]
            if data[at:at + len(pattern)] != pattern:
                ok = False
                break
        if ok:
            candidates.append(start)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if near is None:
        return None  # ambiguous without a position hint
    candidates.sort(key=lambda c: abs(c - near))
    if abs(candidates[0] - near) == abs(candidates[1] - near):
        return None
    return candidates[0]


def _path(root: Path, original_sha: str) -> Path:
    return root / MAPS_DIR / original_sha[:2] / f"{original_sha}.json.gz"


def save_records(root: Path, original_sha: str, tuned_sha: str, records: list[dict[str, Any]]) -> None:
    path = _path(root, original_sha)
    data = load_all(root, original_sha)
    data[tuned_sha] = records
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))
    tmp.replace(path)


def load_all(root: Path, original_sha: str) -> dict[str, list[dict[str, Any]]]:
    path = _path(root, original_sha)
    if not path.exists():
        return {}
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_records(root: Path | None, original_sha: str, tuned_sha: str) -> list[dict[str, Any]]:
    if root is None:
        return []
    return load_all(root, original_sha).get(tuned_sha, [])


def store_root_of(package: Any) -> Path | None:
    """Store folder of an installed package (``<store>/packages/<id>.olstune``)."""

    path = getattr(package, "path", None)
    if path is None:
        return None
    root = Path(path).parent.parent
    return root if (root / MAPS_DIR).is_dir() else None


# ----- learning -------------------------------------------------------------

_WORKER_READER: Any = None


def _init_worker(py_ols: str) -> None:
    global _WORKER_READER
    from ..reader import PyOlsReader

    _WORKER_READER = PyOlsReader(Path(py_ols))


def records_for_project(path: str, wanted: list[tuple[str, str]], reader: Any = None) -> dict[str, Any]:
    """Map records for the wanted (original, tuned) image pairs of one project."""

    import hashlib

    from .ols_versions import inspect_project, read_slot

    reader = reader or _WORKER_READER
    try:
        versions = inspect_project(Path(path), reader, project_sha256="-")
    except Exception as exc:  # unreadable project: remembered, not retried every run
        return {"path": path, "error": f"{type(exc).__name__}: {exc}"[:200], "pairs": []}
    if not versions.maps:
        return {"path": path, "maps": 0, "pairs": []}
    needed = {sha for pair in wanted for sha in pair}
    images: dict[str, bytes] = {}
    for slot in versions.slots:
        try:
            image = read_slot(Path(path), slot)
        except OSError:
            continue
        sha = hashlib.sha256(image).hexdigest()
        if sha in needed:
            images[sha] = image
    out = []
    for original_sha, tuned_sha in wanted:
        if original_sha in images and tuned_sha in images:
            out.append((original_sha, tuned_sha,
                        map_records(versions.maps, images[original_sha], images[tuned_sha])))
    return {"path": path, "maps": len(versions.maps), "pairs": out}


def learn_maps(root: Path, py_ols: Path, *, workers: int = 4, max_files: int | None = None,
               progress=print, stop=None) -> dict[str, int]:
    """Extract map records for every training pair of the library at ``root``.

    Uses py-ols (the built-in reader does not decode map definitions).
    Resumable: finished projects are listed in ``maps/done.txt``."""

    import sqlite3
    from concurrent.futures import ProcessPoolExecutor, as_completed

    maps_dir = root / MAPS_DIR
    maps_dir.mkdir(parents=True, exist_ok=True)
    done_path = maps_dir / "done.txt"
    done = set(done_path.read_text(encoding="utf-8").split()) if done_path.exists() else set()
    db = sqlite3.connect(f"file:{root / 'library.sqlite3'}?mode=ro", uri=True, timeout=60)
    wanted: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for path, project_sha, original_sha, tuned_sha in db.execute(
            "SELECT f.path, f.sha256, p.original_sha256, p.tuned_sha256 FROM files f "
            "JOIN pairs p ON p.project_sha256 = f.sha256 WHERE f.status = 'parsed_with_pairs'"):
        if project_sha in done:
            continue
        entry = wanted.setdefault(project_sha, (path, []))
        if (original_sha, tuned_sha) not in entry[1]:
            entry[1].append((original_sha, tuned_sha))
    db.close()
    jobs = list(wanted.items())[:max_files] if max_files else list(wanted.items())
    stats = {"projects": len(jobs), "with_maps": 0, "pairs": 0, "records": 0, "errors": 0}
    progress(f"Kaartkennis: {len(jobs)} projecten te lezen ({len(done)} al gedaan), {workers} tegelijk.")
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(str(py_ols),)) as pool, \
            done_path.open("a", encoding="utf-8") as done_file:
        futures = {pool.submit(records_for_project, path, pairs): sha for sha, (path, pairs) in jobs}
        for count, future in enumerate(as_completed(futures), 1):
            if stop is not None and stop():
                for f in futures:
                    f.cancel()
                break
            result = future.result()
            if result.get("error"):
                stats["errors"] += 1
            if result.get("maps"):
                stats["with_maps"] += 1
            for original_sha, tuned_sha, records in result["pairs"]:
                if records:
                    save_records(root, original_sha, tuned_sha, records)
                    stats["pairs"] += 1
                    stats["records"] += len(records)
            done_file.write(futures[future] + "\n")
            done_file.flush()
            if count % 100 == 0 or count == len(jobs):
                progress(f"Kaartkennis: {count}/{len(jobs)} projecten, {stats['with_maps']} met kaarten, "
                         f"{stats['records']} kaarten van {stats['pairs']} tunes.")
    return stats


# ----- rules per map type ---------------------------------------------------

def _cells(blob: bytes, width: int, endian: str) -> list[int]:
    if width == 1:
        return list(blob)
    count = len(blob) // 2
    import struct

    return list(struct.unpack(f"{'<' if endian == 'little' else '>'}{count}H", blob[:count * 2]))


def _guess_endian(blob: bytes) -> str:
    """Byte order whose 16-bit cells change more smoothly."""

    def rough(endian: str) -> float:
        cells = _cells(blob, 2, endian)
        if len(cells) < 2:
            return 0.0
        return sum(abs(a - b) for a, b in zip(cells, cells[1:])) / (len(cells) - 1)

    return "little" if rough("little") <= rough("big") else "big"


def map_change(record: dict[str, Any]) -> dict[str, Any] | None:
    """Median relative change of the changed cells of one map record."""

    orig, tuned = bytes.fromhex(record["orig"]), bytes.fromhex(record["tuned"])
    width = record["width"]
    best = None
    # The byte order is not stored; the right one gives the smoother map and,
    # since tuners change values moderately, the smaller typical change.
    for endian in (("little", "big") if width == 2 else ("big",)):
        before, after = _cells(orig, width, endian), _cells(tuned, width, endian)
        ratios = sorted(t / o for o, t in zip(before, after) if o != t and o > 0)
        if not ratios:
            continue
        score = (abs(ratios[len(ratios) // 2] - 1), endian != _guess_endian(orig))
        if best is None or score < best[0]:
            best = (score, before, after, ratios)
    if best is None:
        return None
    _score, before, after, ratios = best
    changed = sum(1 for o, t in zip(before, after) if o != t)
    ceiling = (1 << (8 * width)) - 1
    return {"median": ratios[len(ratios) // 2], "changed_share": changed / max(1, len(before)),
            "to_max": all(t >= ceiling - 1 for o, t in zip(before, after) if o != t)}


def normalize_map_name(name: str) -> str:
    import re

    name = re.sub(r"\s+", " ", name.lower()).strip()
    name = re.sub(r"\(.*?\)|\[.*?\]|#\d+|\b\d+\b", " ", name)
    return re.sub(r"\s+", " ", name).strip(" -_:.")


def map_rules(root: Path, min_examples: int = 3) -> list[dict[str, Any]]:
    """Per map name and tuning profile: how often tuners change it and by how much."""

    import sqlite3

    db = sqlite3.connect(f"file:{root / 'library.sqlite3'}?mode=ro", uri=True, timeout=60)
    profiles = {(o, t): (p, f.split("|")[0]) for o, t, p, f in db.execute(
        "SELECT original_sha256, tuned_sha256, profile, family_key FROM pairs")}
    db.close()
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for path in (root / MAPS_DIR).glob("*/*.json.gz"):
        original_sha = path.name.split(".")[0]
        for tuned_sha, records in load_all(root, original_sha).items():
            profile, family = profiles.get((original_sha, tuned_sha), ("other", "onbekend"))
            profile = next((part for part in profile.split("+") if part.startswith("stage")), profile)
            for record in records:
                name = normalize_map_name(record.get("name", ""))
                if not name or name in ("map", "kennfeld", "kennlinie", "curve", "value")                         or name.startswith(('map "', "map bosch", "unbekannt", "unknown")):
                    continue  # unnamed maps (tuner map packs) say nothing about the map type
                change = map_change(record)
                if change is None:
                    continue
                group = groups.setdefault((name, profile), {"ratios": [], "families": set(), "to_max": 0})
                group["ratios"].append(change["median"])
                group["families"].add(family)
                group["to_max"] += change["to_max"]
    rules = []
    for (name, profile), group in groups.items():
        ratios = sorted(group["ratios"])
        if len(ratios) < min_examples:
            continue
        rules.append({"map": name, "profile": profile, "examples": len(ratios),
                      "median_change": ratios[len(ratios) // 2] - 1,
                      "spread": (ratios[len(ratios) // 4] - 1, ratios[3 * len(ratios) // 4] - 1),
                      "to_max_share": group["to_max"] / len(ratios),
                      "ecu_families": sorted(group["families"])})
    rules.sort(key=lambda r: (-r["examples"], r["map"], r["profile"]))
    return rules
