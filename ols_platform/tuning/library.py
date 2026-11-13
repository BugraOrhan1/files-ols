"""Resumable library update: archive -> confirmed pairs -> trained package.

Sources are read only. All state (checkpoint database, pairs, originals,
cache, packages) lives in the local store. Projects are processed by a small
pool of worker processes; the main process records every finished project
in SQLite immediately, so an interrupted run resumes where it stopped and
never drops or half-records a project.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import SOFTWARE_VERSION
from .identity import learn_identity
from .learn import DEFAULT_SETTINGS, TrainingPair, diff_regions
from .ols_versions import checksum_state, classify_profile, inspect_project, read_slot
from .package import MAX_LEARNING_PAIRS, PackageBuilder, PackageStore, prepare_variant

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    size INTEGER,
    mtime_ns INTEGER,
    sha256 TEXT,
    status TEXT NOT NULL,
    message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    pair_ids TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pairs (
    pair_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    project_sha256 TEXT NOT NULL,
    original_sha256 TEXT NOT NULL,
    tuned_sha256 TEXT NOT NULL,
    profile TEXT NOT NULL,
    family_key TEXT NOT NULL,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    stats TEXT
);
"""

DONE_STATES = ("parsed_no_pairs", "parsed_with_pairs", "rejected", "too_large")
STATUS_TEXT = {
    "parsed_with_pairs": "gelezen, voorbeelden gevonden",
    "parsed_no_pairs": "gelezen, geen bruikbare versies",
    "rejected": "overgeslagen (zie reden in database)",
    "too_large": "overgeslagen (te groot)",
    "retry_pending": "niet gelukt, volgende keer opnieuw",
}
_IMPORT_LINE = re.compile(rb"Importiert aus Datei:\r\n([^\r\n]{1,400})")
PAIR_FORMAT = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 2) - 1))


def is_network_path(path: Path) -> bool:
    text = str(path)
    if text.startswith(("\\\\", "//")):
        return True
    if sys.platform == "win32":
        drive = os.path.splitdrive(os.path.abspath(text))[0]
        if drive:
            try:
                import ctypes

                return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE
            except Exception:
                return False
    return False


def _read_form_hint(project_path: Path) -> dict[str, str]:
    with project_path.open("rb") as handle:
        head = handle.read(65536)
    match = _IMPORT_LINE.search(head)
    if not match:
        return {"read_form": "onbekend", "import_file": ""}
    name = match.group(1).decode("latin-1").replace("\\", "/").rsplit("/", 1)[-1]
    lower = name.lower()
    if "obd" in lower:
        form = "OBD (afgeleid uit importnaam, niet geverifieerd)"
    elif any(k in lower for k in ("bench", "boot", "bdm", "jtag")):
        form = "Bench/Boot (afgeleid uit importnaam, niet geverifieerd)"
    else:
        form = "onbekend"
    return {"read_form": form, "import_file": name}


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(4 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
            total += len(block)
    return digest.hexdigest(), total


def _copy_and_hash(source: Path, target: Path) -> tuple[str, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as reader, target.open("wb") as writer:
        while True:
            block = reader.read(4 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
            writer.write(block)
            total += len(block)
    return digest.hexdigest(), total


# ----- pair storage (module level so worker processes can use it) -----------
def save_pair(root: Path, pair: TrainingPair) -> None:
    pairs_dir = root / "pairs"
    originals_dir = root / "originals"
    pairs_dir.mkdir(parents=True, exist_ok=True)
    originals_dir.mkdir(parents=True, exist_ok=True)
    original_path = originals_dir / f"{pair.record['original_sha256']}.bin.gz"
    if not original_path.exists():
        tmp = original_path.with_name(f"{original_path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(gzip.compress(pair.original, compresslevel=1, mtime=0))
        os.replace(tmp, original_path)
    path = pairs_dir / f"{pair.pair_id}.json.gz"
    if path.exists():
        return
    payload = {
        "format": PAIR_FORMAT,
        "pair_id": pair.pair_id,
        "family_key": pair.family_key,
        "variant_key": pair.variant_key,
        "identity": pair.identity,
        "profile": pair.profile,
        "maps": pair.maps,
        "record": pair.record,
        "tuned_patch": [[s, pair.tuned[s:e].hex()] for s, e in diff_regions(pair.original, pair.tuned, 0)],
    }
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(gzip.compress(json.dumps(payload, sort_keys=True).encode(), compresslevel=3, mtime=0))
    os.replace(tmp, path)


def load_pair(root: Path, pair_id: str, originals_cache: dict[str, bytes] | None = None) -> TrainingPair:
    new_path = root / "pairs" / f"{pair_id}.json.gz"
    if new_path.exists():
        payload = json.loads(gzip.decompress(new_path.read_bytes()))
        sha = payload["record"]["original_sha256"]
        original = originals_cache.get(sha) if originals_cache is not None else None
        if original is None:
            original = gzip.decompress((root / "originals" / f"{sha}.bin.gz").read_bytes())
            if originals_cache is not None:
                originals_cache[sha] = original
    else:  # format 1: original embedded as hex
        payload = json.loads(gzip.decompress((root / "pairs" / f"{pair_id}.pair.gz").read_bytes()))
        original = gzip.decompress(bytes.fromhex(payload["original_hex_gz"]))
    tuned = bytearray(original)
    for start, hex_bytes in payload["tuned_patch"]:
        chunk = bytes.fromhex(hex_bytes)
        tuned[start:start + len(chunk)] = chunk
    pair = TrainingPair(
        pair_id=payload["pair_id"], family_key=payload["family_key"],
        variant_key=payload["variant_key"], identity=payload["identity"],
        original=original, tuned=bytes(tuned), profile=payload["profile"],
        maps=payload["maps"], record=payload["record"],
    )
    if hashlib.sha256(pair.tuned).hexdigest() != pair.record["tuned_sha256"]:
        raise ValueError(f"Paar {pair_id} is beschadigd.")
    return pair


# ----- extraction ----------------------------------------------------------
def extract_pairs(path: Path, reader: Any, project_sha: str, export_hashes: set[str],
                  display_name: str | None = None) -> tuple[list[TrainingPair], list[dict[str, Any]]]:
    versions = inspect_project(path, reader, project_sha)
    name = display_name or path.name
    rejected: list[dict[str, Any]] = []
    if versions.tag is None or not versions.slots:
        rejected.append({"project": name, "reason": "; ".join(versions.issues) or "geen versies"})
        return [], rejected
    if versions.issues:
        rejected.append({"project": name, "reason": "; ".join(versions.issues)})
        return [], rejected
    slots = sorted(versions.slots, key=lambda s: s["slot_index"])
    original = read_slot(path, slots[0])
    identity = learn_identity(versions.metadata, original)
    if not identity:
        rejected.append({"project": name, "reason": "Geen enkele ECU/HW/SW-identificatie uit de "
                         "projectgegevens komt letterlijk voor in de bytes."})
        return [], rejected
    hint = _read_form_hint(path)
    meta = versions.metadata
    family_label = identity["ecu_type"][0] if "ecu_type" in identity else (
        meta.get("ecu_type")
        or " ".join(x for x in (meta.get("ecu_manufacturer", ""), meta.get("hw_code") or meta.get("hw_number", "")) if x)
        or "onbekend")
    family_key = f"{family_label}|{len(original)}"
    variant_key = "|".join(f"{k}={'+'.join(v)}" for k, v in sorted(identity.items()) if k != "ecu_type") or "ecu_only"
    original_sha = hashlib.sha256(original).hexdigest()
    pairs: list[TrainingPair] = []
    for tag_version, slot in zip(versions.tag.versions[1:], slots[1:]):
        tuned = read_slot(path, slot)
        profile = classify_profile(tag_version.name)
        if profile == "original":
            continue
        if len(tuned) != len(original):
            rejected.append({"project": name, "version": tag_version.name,
                             "reason": "Versie heeft een andere lengte dan het origineel."})
            continue
        regions = diff_regions(original, tuned, 0)
        differing = sum(e - s for s, e in regions)
        fraction = differing / len(original)
        if differing == 0:
            rejected.append({"project": name, "version": tag_version.name,
                             "reason": "Versie is identiek aan het origineel."})
            continue
        if fraction > 0.10:
            rejected.append({"project": name, "version": tag_version.name,
                             "reason": f"{fraction:.1%} van de bytes verschilt: geen kalibratiebewerking."})
            continue
        if learn_identity(versions.metadata, tuned) != identity:
            rejected.append({"project": name, "version": tag_version.name,
                             "reason": "ECU-identificatie verschilt tussen origineel en versie."})
            continue
        tuned_sha = hashlib.sha256(tuned).hexdigest()
        evidence = [
            "revision_tag_order_matches_slot_count",
            "slot_image_offset_blob_plus_4_verified_on_14_exports",
            "identity_tokens_identical_in_both",
            f"differing_bytes={differing} ({fraction:.3%})",
        ]
        if original_sha in export_hashes:
            evidence.append("original_matches_independent_winols_export_sha256")
        if tuned_sha in export_hashes:
            evidence.append("tuned_matches_independent_winols_export_sha256")
        pairs.append(TrainingPair(
            pair_id=hashlib.sha256(f"{original_sha}:{tuned_sha}".encode()).hexdigest()[:16],
            family_key=family_key, variant_key=variant_key, identity=identity,
            original=original, tuned=tuned, profile=profile, maps=versions.maps,
            record={
                "project_name": name,
                "project_sha256": project_sha,
                "original_slot": slots[0]["slot_index"],
                "tuned_slot": slot["slot_index"],
                "original_version_name": versions.tag.versions[0].name,
                "tuned_version_name": tag_version.name,
                "checksum_note": tag_version.checksum_note,
                "checksum_state": checksum_state(tag_version.checksum_note),
                "original_sha256": original_sha,
                "tuned_sha256": tuned_sha,
                "lineage": original_sha,
                "read_method": f"{getattr(reader, 'parser_name', 'reader')} + RevisionTag",
                "read_form": hint["read_form"],
                "import_file": hint["import_file"],
                "memory_layout": f"single image, {len(original)} bytes, file offset = image offset",
                "ecu_type": meta.get("ecu_type", ""),
                "ecu_manufacturer": meta.get("ecu_manufacturer", ""),
                "hw_number": meta.get("hw_number", ""),
                "hw_code": meta.get("hw_code", ""),
                "sw_number": meta.get("sw_number", ""),
                "vehicle": " ".join(x for x in (meta.get("make", ""), meta.get("model", ""),
                                                meta.get("engine", "")) if x),
                "evidence": evidence,
            },
        ))
    return pairs, rejected


def _reader_from_spec(spec: Any):
    if spec == "native":
        from .ols_native import NativeOlsReader

        return NativeOlsReader()
    if isinstance(spec, (list, tuple)) and spec and spec[0] == "pyols":
        from ..reader import PyOlsReader

        return PyOlsReader(Path(spec[1]))
    raise ValueError(f"Onbekende lezer: {spec!r}")


def reader_spec(reader: Any) -> Any:
    """A picklable description of the reader, or None for in-process readers."""

    if getattr(reader, "parser_name", "") == "ols-tuner-native":
        return "native"
    if getattr(reader, "parser_name", "") == "py-ols" and getattr(reader, "source_path", None):
        return ("pyols", str(reader.source_path))
    return None


def process_project(task: dict[str, Any], reader: Any = None) -> dict[str, Any]:
    """Hash, parse and store one project. Runs in a worker process.

    Returns only light metadata; the main process writes the checkpoint.
    """

    path = Path(task["path"])
    root = Path(task["store_root"])
    reader = reader if reader is not None else _reader_from_spec(task["reader"])
    result: dict[str, Any] = {"path": task["path"], "bytes": 0}
    attempt = 0
    while True:
        try:
            st = path.stat()
            result["size"], result["mtime_ns"] = st.st_size, st.st_mtime_ns
            if st.st_size > task["max_file_bytes"]:
                result.update(status="too_large", message=f"{st.st_size} bytes", pairs=[], sha256=None)
                return result
            local = path
            if task["network"]:
                local = root / "scratch" / f"{os.getpid()}-{threading.get_ident()}{path.suffix}"
                sha, read = _copy_and_hash(path, local)
            else:
                sha, read = _sha256_file(path)
            result["bytes"] += read
            try:
                if path.stat().st_mtime_ns != st.st_mtime_ns:
                    raise OSError("bestand veranderde tijdens het lezen")
                pairs, rejected = extract_pairs(local, reader, sha, set(task["export_hashes"]), path.name)
            finally:
                if local != path:
                    local.unlink(missing_ok=True)
            for pair in pairs:
                save_pair(root, pair)
            result.update(
                status="parsed_with_pairs" if pairs else ("rejected" if rejected else "parsed_no_pairs"),
                message=json.dumps(rejected, ensure_ascii=False) if rejected else "",
                sha256=sha,
                pairs=[{
                    "pair_id": p.pair_id, "project_sha256": p.record["project_sha256"],
                    "original_sha256": p.record["original_sha256"], "tuned_sha256": p.record["tuned_sha256"],
                    "profile": p.profile, "family_key": p.family_key, "variant_key": p.variant_key,
                    "project_name": p.record["project_name"],
                } for p in pairs],
            )
            return result
        except OSError as exc:
            attempt += 1
            if attempt > task["retries"]:
                result.update(status="retry_pending", message=str(exc), pairs=[], sha256=None)
                return result
            time.sleep(task["retry_delay"] * attempt)
        except Exception as exc:  # parser failure: record and continue
            result.update(status="rejected", message=f"lezer: {exc}", pairs=[], sha256=None)
            return result


@dataclass
class ResourceMeter:
    started: float = field(default_factory=time.monotonic)
    bytes_read: int = 0
    files: int = 0

    def summary(self) -> dict[str, Any]:
        elapsed = max(1e-6, time.monotonic() - self.started)
        peak = None
        try:
            import resource  # POSIX only

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        except Exception:  # pragma: no cover - Windows
            peak = None
        return {
            "elapsed_seconds": round(elapsed, 3),
            "files_processed": self.files,
            "bytes_read": self.bytes_read,
            "files_per_hour": round(self.files / elapsed * 3600, 1),
            "bytes_per_hour": round(self.bytes_read / elapsed * 3600),
            "peak_memory_bytes": peak,
        }


class Library:
    def __init__(self, store: PackageStore) -> None:
        self.store = store
        self.root = store.root
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "library.sqlite3", timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(pairs)")}
        for column in ("variant_key", "project_name"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE pairs ADD COLUMN {column} TEXT")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def load_pair(self, pair_id: str, originals_cache: dict[str, bytes] | None = None) -> TrainingPair:
        return load_pair(self.root, pair_id, originals_cache)

    def _record(self, source_key: str, result: dict[str, Any], stats: dict[str, Any]) -> None:
        status = result["status"]
        new = 0
        for meta in result.get("pairs", []):
            new += self.db.execute(
                "INSERT OR IGNORE INTO pairs(pair_id,source,project_sha256,original_sha256,tuned_sha256,"
                "profile,family_key,first_seen,variant_key,project_name) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (meta["pair_id"], source_key, meta["project_sha256"], meta["original_sha256"],
                 meta["tuned_sha256"], meta["profile"], meta["family_key"], _now(),
                 meta["variant_key"], meta["project_name"]),
            ).rowcount
            stats["profiles"][meta["profile"]] = stats["profiles"].get(meta["profile"], 0) + 1
        stats["new_pairs"] += new
        stats["confirmed_pairs"] += len(result.get("pairs", []))
        stats["stage1_pairs"] += sum(1 for m in result.get("pairs", []) if m["profile"] == "stage1")
        if result.get("sha256"):
            stats["hashed"] += 1
        if status in ("parsed_with_pairs", "parsed_no_pairs") or (status == "rejected" and result.get("sha256")):
            stats["parsed"] += 1
        if status == "rejected":
            stats["rejected"] += 1
        if status == "retry_pending":
            stats["retry_pending"] += 1
            stats["errors"].append(f"{Path(result['path']).name}: {result.get('message')}")
        self.db.execute(
            "INSERT INTO files(path,source,size,mtime_ns,sha256,status,message,attempts,pair_ids,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "size=excluded.size,mtime_ns=excluded.mtime_ns,sha256=COALESCE(excluded.sha256,files.sha256),"
            "status=excluded.status,message=excluded.message,attempts=files.attempts+1,"
            "pair_ids=excluded.pair_ids,updated_at=excluded.updated_at",
            (result["path"], source_key, result.get("size"), result.get("mtime_ns"), result.get("sha256"),
             status, result.get("message", ""), 1, json.dumps([m["pair_id"] for m in result.get("pairs", [])]),
             _now()),
        )
        self.db.commit()  # checkpoint per project

    def scan(self, sources: dict[str, Path], reader: Any, *,
             export_dirs: list[Path] | None = None, max_file_bytes: int = 1 << 30,
             max_files: int | None = None, retries: int = 3, retry_delay: float = 1.0,
             progress: Callable[[str], None] | None = None,
             stop: threading.Event | None = None,
             on_file: Callable[[str, int, int], None] | None = None,
             on_pairs: Callable[[int], None] | None = None,
             workers: int | None = None) -> dict[str, Any]:
        """Scan sources. ``on_file(source, done, found)`` after each project,
        ``on_pairs(new_pairs_total)`` after projects that added pairs (callers
        use it to publish intermediate packages)."""

        say = progress or (lambda message: None)
        meter = ResourceMeter()
        export_hashes: set[str] = set()
        for directory in export_dirs or []:
            for entry in sorted(directory.iterdir()):
                if entry.is_file():
                    sha, read = _sha256_file(entry)
                    export_hashes.add(sha)
                    meter.bytes_read += read
        spec = reader_spec(reader)
        workers = default_workers() if workers is None else workers
        if spec is None:
            workers = 1
        per_source: dict[str, dict[str, Any]] = {}
        run_id = self.db.execute("INSERT INTO runs(started_at,status) VALUES(?,?)", (_now(), "running")).lastrowid
        self.db.commit()
        processed = 0
        new_total = 0
        stopped = lambda: stop is not None and stop.is_set()  # noqa: E731
        pool = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
        try:
            for source_key, root in sources.items():
                stats = per_source.setdefault(source_key, {
                    "root": str(root), "status": "available", "found": 0, "hashed": 0, "parsed": 0,
                    "confirmed_pairs": 0, "stage1_pairs": 0, "new_pairs": 0, "rejected": 0,
                    "retry_pending": 0, "skipped_unchanged": 0, "errors": [], "profiles": {},
                    "network": False,
                })
                if stopped():
                    stats["status"] = "interrupted"
                    continue
                try:
                    walker = list(_walk_ols(root))
                except OSError as exc:
                    stats["status"] = f"unavailable: {exc.strerror or exc}"
                    say(f"Bron {source_key} niet bereikbaar ({root}): {exc.strerror or exc}")
                    continue
                network = is_network_path(root)
                stats["network"] = network
                stats["found"] = len(walker)
                say(f"{source_key}: {len(walker)} projecten gevonden "
                    f"({'netwerk, via lokale kopie' if network else 'lokale schijf'}, {workers} tegelijk)")
                if on_file:
                    on_file(source_key, 0, len(walker))
                known = {
                    row[0]: (row[1], row[2], row[3])
                    for row in self.db.execute("SELECT path,size,mtime_ns,status FROM files WHERE source=?",
                                               (source_key,))
                } if walker else {}
                done = 0
                pending: dict[Any, str] = {}
                queue_iter = iter(walker)

                def submit_next() -> bool:
                    nonlocal processed
                    for path in queue_iter:
                        key = str(path)
                        row = known.get(key)
                        if row and row[2] in DONE_STATES:
                            try:
                                st = path.stat()
                            except OSError:
                                st = None
                            if st and row[0] == st.st_size and row[1] == st.st_mtime_ns:
                                stats["skipped_unchanged"] += 1
                                return_skip(path)
                                continue
                        if max_files is not None and processed >= max_files:
                            stats["status"] = "batch_limit_reached"
                            return False
                        task = {"path": key, "store_root": str(self.root), "reader": spec, "network": network,
                                "export_hashes": sorted(export_hashes), "max_file_bytes": max_file_bytes,
                                "retries": retries, "retry_delay": retry_delay}
                        processed += 1
                        if pool is None:
                            finish(process_project(task, reader))
                        else:
                            pending[pool.submit(process_project, task)] = key
                        return True
                    return False

                def return_skip(path: Path) -> None:
                    nonlocal done
                    done += 1
                    if on_file:
                        on_file(source_key, done, len(walker))

                def finish(result: dict[str, Any]) -> None:
                    nonlocal done, new_total
                    before = stats["new_pairs"]
                    self._record(source_key, result, stats)
                    meter.files += 1
                    meter.bytes_read += result.get("bytes", 0)
                    done += 1
                    say(f"{source_key}: {Path(result['path']).name} – {STATUS_TEXT.get(result['status'], result['status'])}")
                    if on_file:
                        on_file(source_key, done, len(walker))
                    if stats["new_pairs"] > before:
                        new_total += stats["new_pairs"] - before
                        if on_pairs:
                            on_pairs(new_total)

                limit = workers * 2
                exhausted = False
                while not stopped() and not (exhausted and not pending):
                    while not exhausted and len(pending) < limit and not stopped():
                        if not submit_next():
                            exhausted = True
                    if pending:
                        finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                        for future in finished:
                            pending.pop(future)
                            finish(future.result())
                # let running workers finish so nothing is half-recorded
                for future in list(pending):
                    finish(future.result())
                if stopped() and stats["status"] == "available":
                    stats["status"] = "interrupted"
        finally:
            if pool is not None:
                pool.shutdown(wait=True)
        summary = {"sources": per_source, "resources": meter.summary(), "workers": workers}
        self.db.execute("UPDATE runs SET finished_at=?,status=?,stats=? WHERE id=?",
                        (_now(), "interrupted" if stopped() else "finished", json.dumps(summary), run_id))
        self.db.commit()
        return summary

    def all_pairs(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT pair_id FROM pairs ORDER BY pair_id")]

    def pair_counts_by_source(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for source, profile, count in self.db.execute("SELECT source,profile,COUNT(*) FROM pairs GROUP BY source,profile"):
            out.setdefault(source, {})[profile] = count
        return out

    def scan_overview(self) -> dict[str, dict[str, Any]]:
        """What has been scanned so far, per source (works mid-run too)."""

        out: dict[str, dict[str, Any]] = {}
        for source, status, count in self.db.execute("SELECT source,status,COUNT(*) FROM files GROUP BY source,status"):
            out.setdefault(source, {"files": {}, "profiles": {}})["files"][status] = count
        for source, profile, count in self.db.execute("SELECT source,profile,COUNT(*) FROM pairs GROUP BY source,profile"):
            out.setdefault(source, {"files": {}, "profiles": {}})["profiles"][profile] = count
        return out

    def train(self, *, profiles: list[str] | None = None, holdout_projects: list[str] | None = None,
              settings: dict[str, Any] | None = None, activate: bool = True,
              progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Train every software variant (all recognised profiles), one variant
        at a time. Unchanged variants are taken from the cache."""

        say = progress or (lambda message: None)
        cfg = {**DEFAULT_SETTINGS, **(settings or {})}
        rows = [
            {"pair_id": r[0], "project_sha256": r[1], "original_sha256": r[2], "tuned_sha256": r[3],
             "profile": r[4], "family_key": r[5], "variant_key": r[6], "project_name": r[7] or ""}
            for r in self.db.execute(
                "SELECT pair_id,project_sha256,original_sha256,tuned_sha256,profile,family_key,"
                "variant_key,project_name FROM pairs ORDER BY pair_id")
        ]
        for row in rows:  # rows from before the variant column existed
            if row["variant_key"] is None:
                pair = self.load_pair(row["pair_id"])
                row["variant_key"] = pair.variant_key
                self.db.execute("UPDATE pairs SET variant_key=?,project_name=? WHERE pair_id=?",
                                (pair.variant_key, pair.record.get("project_name", ""), row["pair_id"]))
        self.db.commit()
        holdout = holdout_closure(rows, holdout_projects or [])
        groups: dict[tuple[str, str], dict[str, list[str]]] = {}
        zones: dict[tuple[str, str], list[str]] = {}
        excluded = []
        for row in rows:
            if row["pair_id"] in holdout:
                continue
            key = (row["family_key"], row["variant_key"])
            wanted = profiles is None or row["profile"] in profiles
            if row["profile"] in ("other", "original") or not wanted:
                zones.setdefault(key, []).append(row["pair_id"])
                excluded.append({"pair_id": row["pair_id"], "profile": row["profile"],
                                 "reason": "Onbekende versienaam; alleen gebruikt om bewerkte invoer te herkennen."})
            else:
                groups.setdefault(key, {}).setdefault(row["profile"], []).append(row["pair_id"])
        if not groups:
            raise ValueError("Nog geen bruikbare voorbeelden (Origineel + getunede versie) gevonden.")
        try:
            previous = self.store.load_active()
        except ValueError:
            previous = None
        cache_dir = self.root / "cache" / "variants"
        cache_dir.mkdir(parents=True, exist_ok=True)
        used_cache: set[str] = set()
        builder = PackageBuilder(cfg)
        reused = 0
        for index, key in enumerate(sorted(groups), 1):
            by_profile = {p: sorted(ids) for p, ids in groups[key].items()}
            zone_ids = sorted(zones.get(key, []))[:MAX_LEARNING_PAIRS]
            cache_key = hashlib.sha256(json.dumps(
                {"p": by_profile, "z": zone_ids, "s": cfg, "v": SOFTWARE_VERSION, "f": 3},
                sort_keys=True).encode()).hexdigest()[:32]
            cache_file = cache_dir / f"{cache_key}.json"
            used_cache.add(cache_file.name)
            if cache_file.exists():
                try:
                    builder.add_prepared(json.loads(cache_file.read_text(encoding="utf-8")))
                    reused += 1
                    continue
                except (OSError, ValueError, KeyError):
                    pass
            total = sum(len(v) for v in by_profile.values())
            say(f"Leren variant {index}/{len(groups)}: {key[0]} ({total} voorbeelden, {len(by_profile)} profielen)")
            cache: dict[str, bytes] = {}
            learning = {p: [self.load_pair(i, cache) for i in ids[:MAX_LEARNING_PAIRS]] for p, ids in by_profile.items()}
            extra = {p: (self.load_pair(i, cache) for i in ids[MAX_LEARNING_PAIRS:]) for p, ids in by_profile.items()}
            zone_pairs = [self.load_pair(i, cache) for i in zone_ids]
            unit = prepare_variant(learning, extra, zone_pairs, cfg)
            builder.add_prepared(unit)
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(unit), encoding="utf-8")
            os.replace(tmp, cache_file)
            del learning, extra, zone_pairs, cache
        for stale in cache_dir.glob("*.json"):
            if stale.name not in used_cache:
                stale.unlink(missing_ok=True)
        package = builder.finish(holdout=sorted(holdout), excluded=excluded)
        if previous is not None and previous.package_id == package.package_id:
            installed = False
        else:
            self.store.install(package, activate=activate)
            installed = True
        previous_pairs = {p["pair_id"] for p in previous["training_selection"]} if previous else set()
        current_pairs = {p["pair_id"] for p in package["training_selection"]}
        profile_counts: dict[str, int] = {}
        for item in package["training_selection"]:
            profile_counts[item["profile"]] = profile_counts.get(item["profile"], 0) + 1
        return {
            "package_id": package.package_id,
            "previous_package_id": previous.package_id if previous else None,
            "installed": installed,
            "variants_reused_from_cache": reused,
            "pairs_used": len(current_pairs),
            "profiles": profile_counts,
            "pairs_added_since_previous": sorted(current_pairs - previous_pairs),
            "pairs_removed_since_previous": sorted(previous_pairs - current_pairs),
            "holdout_pairs": sorted(holdout),
            "families": [
                {"family_key": f["family_key"], "variants": len(f["variants"]),
                 "pairs": sum(len(v["pairs"]) for v in f["variants"]),
                 "projects": len({p for v in f["variants"] for p in v["projects"]})}
                for f in package["families"]
            ],
        }


def holdout_closure(pairs: list[Any], holdout_projects: list[str]) -> set[str]:
    """Pairs to exclude: the named projects plus every duplicate/descendant.

    A pair is linked when it shares a project hash, original hash or tuned
    hash with an already excluded pair (transitively). Accepts TrainingPair
    objects or row dicts.
    """

    def record(item: Any) -> dict[str, Any]:
        return item.record | {"pair_id": item.pair_id} if hasattr(item, "record") else item

    records = [record(p) for p in pairs]
    wanted = {h.lower() for h in holdout_projects}
    excluded: set[str] = set()
    hashes: set[str] = set()
    for r in records:
        if r.get("project_sha256") in wanted or (r.get("project_name") or "").lower() in wanted \
                or r.get("original_sha256") in wanted:
            excluded.add(r["pair_id"])
            hashes.update({r.get("project_sha256"), r.get("original_sha256"), r.get("tuned_sha256")})
    hashes.discard(None)
    changed = True
    while changed:
        changed = False
        for r in records:
            linked = {r.get("project_sha256"), r.get("original_sha256"), r.get("tuned_sha256")} - {None}
            if r["pair_id"] not in excluded and linked & hashes:
                excluded.add(r["pair_id"])
                hashes |= linked
                changed = True
    return excluded


def _walk_ols(root: Path):
    stack = [root]
    if not root.exists():
        raise FileNotFoundError(2, "map niet gevonden", str(root))
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                items = sorted(entries, key=lambda e: e.name)
        except PermissionError:
            continue  # unreadable subfolder: skip, keep scanning the rest
        for entry in items:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.name.lower().endswith(".ols"):
                    yield Path(entry.path)
            except OSError:
                continue
