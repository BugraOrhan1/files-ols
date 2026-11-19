"""Practice: the library tests itself and learns where transfer is reliable.

For each confirmed example (original + real tuned version) the example's own
project, its duplicates and descendants are hidden. The original is then
tuned from the rest of the library through the similarity transfer (other
software/hardware numbers included) and the result is compared byte for byte
with the real tuned version. Results are stored per example; aggregated per
ECU family they give

* a success rate and average match shown to the user, and
* a learned minimum similarity per family: the lowest similarity from which
  transfers reproduced the real tune (>= 99 %) in at least 95 % of the tests.
  Generation uses it automatically, so unreliable transfers are refused.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from .fileio import read_json, write_json

PRACTICE_VERSION = 1
STATS_FILE = "practice_stats.json"
GOOD_SCORE = 0.99
TEST_MIN_SIMILARITY = 0.30  # practise over a wide range so the safe floor can be learned
MIN_TESTS_FOR_FLOOR = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS practice (
    pair_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    family_key TEXT NOT NULL,
    variant_key TEXT,
    profile TEXT NOT NULL,
    status TEXT NOT NULL,
    code TEXT,
    donor_project TEXT,
    donor_variant_key TEXT,
    same_variant INTEGER,
    similarity REAL,
    coverage REAL,
    real_changed INTEGER,
    reproduced INTEGER,
    unexpected INTEGER,
    score REAL,
    seconds REAL,
    tested_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def linked_originals(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
    """pair_id -> original hashes to hide when practising on that pair: the
    whole group linked by shared project, original or tuned hashes."""

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for r in rows:
        keys = [f"p:{r['project_sha256']}", f"o:{r['original_sha256']}", f"t:{r['tuned_sha256']}"]
        for k in keys[1:]:
            union(keys[0], k)
    groups: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        groups[find(f"p:{r['project_sha256']}")].add(r["original_sha256"])
    return {r["pair_id"]: groups[find(f"p:{r['project_sha256']}")] for r in rows}


def cell_score(original: bytes, ours: bytes, real: bytes) -> tuple[int, int, int]:
    """(cells the real tune changes, of those reproduced, cells we changed that
    the real tune did not). A 16-bit cell counts as reproduced when it equals
    the real value or lies within 0.5 % (rounding of recalculated maps)."""

    import struct

    from .learn import diff_regions

    real_cells = {i - i % 2 for s, e in diff_regions(original, real, 0) for i in range(s, e)}
    our_cells = {i - i % 2 for s, e in diff_regions(original, ours, 0) for i in range(s, e)}
    reproduced = 0
    for cell in real_cells:
        if ours[cell:cell + 2] == real[cell:cell + 2]:
            reproduced += 1
            continue
        if cell + 2 > len(real):
            continue
        for fmt in ("<H", ">H"):
            a = struct.unpack_from(fmt, ours, cell)[0]
            b = struct.unpack_from(fmt, real, cell)[0]
            if abs(a - b) <= max(1, 0.005 * b):
                reproduced += 1
                break
    return len(real_cells), reproduced, len(our_cells - real_cells)


_WORKER: dict[str, Any] = {}


def _init_worker(store_root: str, package_path: str) -> None:
    from .package import read_package

    _WORKER["root"] = Path(store_root)
    _WORKER["package"] = read_package(Path(package_path))


def practice_pair(task: dict[str, Any]) -> dict[str, Any]:
    """Tune one hidden example from the rest of the library and score it."""

    from .evaluate import compare_to_reference
    from .library import load_pair
    from .transfer import transfer

    if not _WORKER:
        _init_worker(task["store_root"], task["package_path"])
    started = time.monotonic()
    pair = load_pair(_WORKER["root"], task["pair_id"])
    result: dict[str, Any] = {"pair_id": pair.pair_id, "family_key": pair.family_key,
                              "variant_key": pair.variant_key, "profile": task["profile"]}
    try:
        outcome = transfer(pair.original, task["profile"], _WORKER["package"], exclude=set(task["exclude"]),
                           min_similarity=TEST_MIN_SIMILARITY, min_coverage=0.0)
    except Exception as exc:  # recorded, practice continues
        outcome = {"status": "failed", "code": "error", "reason": str(exc)}
    if outcome["status"] != "ok":
        result.update(status="failed", code=outcome.get("code"), score=0.0, similarity=None, coverage=None)
    else:
        cmp = compare_to_reference(pair.original, outcome["output"], pair.tuned)
        changed, reproduced, unexpected = cell_score(pair.original, outcome["output"], pair.tuned)
        result["byte_exact"] = round(cmp["reproduction_ratio"], 4)
        donor = outcome["donor"]
        result.update(
            status="ok", code=None, donor_project=donor.get("project"),
            donor_variant_key=next((v["variant_key"] for _f, v in _WORKER["package"].variants()
                                    if v["variant_id"] == donor["variant_id"]), None),
            similarity=round(donor["similarity"], 4), coverage=round(outcome["coverage"], 4),
            real_changed=changed, reproduced=reproduced, unexpected=unexpected,
            score=round(reproduced / (changed + unexpected), 4) if changed + unexpected else 1.0,
        )
        result["same_variant"] = int(result["donor_variant_key"] == pair.variant_key)
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


def run_practice(library, *, max_tests: int | None = None, workers: int | None = None,
                 stop: threading.Event | None = None, progress: Callable[[str], None] | None = None,
                 on_test: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """Practise on examples not yet tested with the current library version."""

    from .library import default_workers

    say = progress or (lambda message: None)
    db = library.db
    db.executescript(SCHEMA)
    package = library.store.load_active()
    if package is None:
        return {"tested": 0, "reason": "Nog geen bibliotheekversie."}
    package_path = library.store.packages_dir / f"{package.package_id}.olstune"
    rows = [
        {"pair_id": r[0], "project_sha256": r[1], "original_sha256": r[2], "tuned_sha256": r[3], "profile": r[4]}
        for r in db.execute("SELECT pair_id,project_sha256,original_sha256,tuned_sha256,profile FROM pairs")
    ]
    hidden = linked_originals(rows)
    done = {r[0] for r in db.execute("SELECT pair_id FROM practice WHERE package_id=? AND version=?",
                                     (package.package_id, PRACTICE_VERSION))}
    todo = [r for r in rows if r["profile"] not in ("other", "original") and r["pair_id"] not in done]
    todo.sort(key=lambda r: r["pair_id"])
    if max_tests is not None:
        todo = todo[:max_tests]
    total = len(todo)
    say(f"Oefenen: {total} voorbeelden te testen (elk verborgen en opnieuw getuned uit de rest).")
    workers = default_workers() if workers is None else workers
    tasks = iter([{"pair_id": r["pair_id"], "profile": r["profile"], "exclude": sorted(hidden[r["pair_id"]]),
                   "store_root": str(library.root), "package_path": str(package_path)} for r in todo])
    tested = 0
    stopped = lambda: stop is not None and stop.is_set()  # noqa: E731

    def record(res: dict[str, Any]) -> None:
        nonlocal tested
        db.execute(
            "INSERT OR REPLACE INTO practice(pair_id,package_id,version,family_key,variant_key,profile,status,code,"
            "donor_project,donor_variant_key,same_variant,similarity,coverage,real_changed,reproduced,unexpected,"
            "score,seconds,tested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (res["pair_id"], package.package_id, PRACTICE_VERSION, res["family_key"], res.get("variant_key"),
             res["profile"], res["status"], res.get("code"), res.get("donor_project"), res.get("donor_variant_key"),
             res.get("same_variant"), res.get("similarity"), res.get("coverage"), res.get("real_changed"),
             res.get("reproduced"), res.get("unexpected"), res.get("score"), res.get("seconds"), _now()))
        db.commit()
        tested += 1
        if on_test:
            on_test(tested, total)
        if tested % 25 == 0:
            write_stats(library.root, db, package.package_id)

    if workers <= 1:
        for task in tasks:
            if stopped():
                break
            record(practice_pair(task))
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                                 initargs=(str(library.root), str(package_path))) as pool:
            pending = set()
            exhausted = False
            while not (exhausted and not pending):
                while not exhausted and len(pending) < workers * 2 and not stopped():
                    task = next(tasks, None)
                    if task is None:
                        exhausted = True
                        break
                    pending.add(pool.submit(practice_pair, task))
                if stopped():
                    exhausted = True
                if pending:
                    finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in finished:
                        record(future.result())
    stats = write_stats(library.root, db, package.package_id)
    say(f"Oefenen: {tested} getest. " + summary_line(stats))
    return {"tested": tested, "remaining": total - tested, "stats": stats}


def _floor(results: list[tuple[float, float]]) -> float | None:
    """Lowest similarity from which >= 95 % of transfers scored >= GOOD_SCORE.

    Only set when practice actually found poor transfers below it: without
    failures there is nothing to protect against and no limit is imposed."""

    tested = sorted((s, score) for s, score in results if s is not None)
    if not any(score < GOOD_SCORE for _s, score in tested):
        return None
    for i, (sim, _score) in enumerate(tested):
        rest = tested[i:]
        if len(rest) < MIN_TESTS_FOR_FLOOR:
            return None
        good = sum(1 for _s, score in rest if score >= GOOD_SCORE)
        if good / len(rest) >= 0.95:
            return round(sim, 4) if i > 0 else None
    return None


def write_stats(root: Path, db: sqlite3.Connection, package_id: str | None = None) -> dict[str, Any]:
    families: dict[str, dict[str, Any]] = {}
    per_family: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for fk, status, score, sim, same in db.execute(
            "SELECT family_key,status,score,similarity,same_variant FROM practice WHERE version=?", (PRACTICE_VERSION,)):
        f = families.setdefault(fk, {"tests": 0, "ok": 0, "good": 0, "score_sum": 0.0, "other_software": 0,
                                     "other_software_good": 0})
        f["tests"] += 1
        if status == "ok":
            f["ok"] += 1
            f["score_sum"] += score or 0.0
            per_family[fk].append((sim, score or 0.0))
            good = (score or 0) >= GOOD_SCORE
            f["good"] += good
            if not same:
                f["other_software"] += 1
                f["other_software_good"] += good
    floors = {}
    for fk, f in families.items():
        f["average_score"] = round(f["score_sum"] / f["ok"], 4) if f["ok"] else 0.0
        del f["score_sum"]
        floor = _floor(per_family[fk])
        f["learned_min_similarity"] = floor
        if floor is not None:
            floors[fk] = floor
    stats = {"updated_at": _now(), "package_id": package_id, "families": families, "floors": floors,
             "total_tests": sum(f["tests"] for f in families.values()),
             "total_good": sum(f["good"] for f in families.values())}
    try:
        write_json(root / STATS_FILE, stats, indent=1)
    except OSError:
        pass
    return stats


def read_stats(root: Path) -> dict[str, Any]:
    stats = read_json(root / STATS_FILE, None)
    if not isinstance(stats, dict):
        return {"families": {}, "floors": {}, "total_tests": 0, "total_good": 0}
    return stats


def summary_line(stats: dict[str, Any]) -> str:
    total = stats.get("total_tests", 0)
    if not total:
        return "Nog geen oefenresultaten."
    good = stats.get("total_good", 0)
    return f"{good} van {total} oefentests ({good / total:.0%}) gaven ≥99% dezelfde bytes als de echte tune."


def family_line(stats: dict[str, Any], family_key: str) -> str | None:
    f = stats.get("families", {}).get(family_key)
    if not f or not f["tests"]:
        return None
    text = (f"Oefenresultaat voor deze ECU: {f['good']} van {f['tests']} tests ≥99% gelijk aan de echte tune, "
            f"gemiddeld {f['average_score']:.1%}")
    if f.get("other_software"):
        text += f"; met andere software {f['other_software_good']} van {f['other_software']}"
    return text + "."
