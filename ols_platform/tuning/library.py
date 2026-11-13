"""Resumable library update: archive -> confirmed pairs -> trained package.

Sources are read only. All state (checkpoint database, pair records,
packages) lives in the local store. Each project is checkpointed after it is
processed, so an interrupted or network-failed run resumes where it stopped
and never drops or half-records a project.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .identity import learn_identity
from .learn import DEFAULT_SETTINGS, TrainingPair, diff_regions
from .ols_versions import checksum_state, classify_profile, inspect_project, read_slot
from .package import MAX_LEARNING_PAIRS, PackageBuilder, PackageStore

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _stream_sha256(path: Path, budget: "ResourceMeter") -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            budget.bytes_read += len(block)
    return digest.hexdigest()


def _copy_and_hash(source: Path, target: Path, budget: "ResourceMeter") -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with source.open("rb") as reader, target.open("wb") as writer:
        while True:
            block = reader.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            writer.write(block)
            budget.bytes_read += len(block)
    return digest.hexdigest()


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
            try:
                import ctypes
                from ctypes import wintypes

                class PMC(ctypes.Structure):
                    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                                ("PeakWorkingSetSize", ctypes.c_size_t),
                                ("WorkingSetSize", ctypes.c_size_t)] + [
                        (n, ctypes.c_size_t) for n in (
                            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                            "PagefileUsage", "PeakPagefileUsage")]
                counters = PMC()
                counters.cb = ctypes.sizeof(PMC)
                ctypes.windll.psapi.GetProcessMemoryInfo(
                    ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
                peak = counters.PeakWorkingSetSize
            except Exception:
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
        self.pairs_dir = self.root / "pairs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "library.sqlite3")
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(pairs)")}
        for column in ("variant_key", "project_name"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE pairs ADD COLUMN {column} TEXT")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ----- pair persistence -------------------------------------------------
    def _save_pair(self, pair: TrainingPair) -> None:
        self.pairs_dir.mkdir(parents=True, exist_ok=True)
        path = self.pairs_dir / f"{pair.pair_id}.pair.gz"
        if path.exists():
            return
        regions = diff_regions(pair.original, pair.tuned, 0)
        payload = {
            "pair_id": pair.pair_id,
            "family_key": pair.family_key,
            "variant_key": pair.variant_key,
            "identity": pair.identity,
            "profile": pair.profile,
            "maps": pair.maps,
            "record": pair.record,
            "original_hex_gz": gzip.compress(pair.original, mtime=0).hex(),
            "tuned_patch": [[s, pair.tuned[s:e].hex()] for s, e in regions],
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(json.dumps(payload, sort_keys=True).encode(), mtime=0))
        os.replace(tmp, path)

    def load_pair(self, pair_id: str) -> TrainingPair:
        payload = json.loads(gzip.decompress((self.pairs_dir / f"{pair_id}.pair.gz").read_bytes()))
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

    # ----- extraction -------------------------------------------------------
    def pairs_from_project(self, path: Path, reader: Any, project_sha: str,
                           export_hashes: set[str], display_name: str | None = None,
                           ) -> tuple[list[TrainingPair], list[dict[str, Any]]]:
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
            rejected.append({
                "project": name,
                "reason": "Geen enkele ECU/HW/SW-identificatie uit de projectgegevens komt "
                          "letterlijk voor in de bytes.",
            })
            return [], rejected
        hint = _read_form_hint(path)
        family_label = (
            identity["ecu_type"][0] if "ecu_type" in identity
            else versions.metadata.get("ecu_type") or "onbekend"
        )
        family_key = f"{family_label}|{len(original)}"
        variant_key = "|".join(
            f"{k}={'+'.join(v)}" for k, v in sorted(identity.items()) if k != "ecu_type"
        ) or "ecu_only"
        original_sha = hashlib.sha256(original).hexdigest()
        pairs: list[TrainingPair] = []
        for tag_version, slot in zip(versions.tag.versions[1:], slots[1:]):
            tuned = read_slot(path, slot)
            profile = classify_profile(tag_version.name)
            if len(tuned) != len(original):
                rejected.append({"project": name, "version": tag_version.name,
                                 "reason": "Versie heeft een andere lengte dan het origineel."})
                continue
            differing = sum(1 for s, e in diff_regions(original, tuned, 0) for _ in range(s, e))
            fraction = differing / len(original)
            if differing == 0:
                rejected.append({"project": name, "version": tag_version.name,
                                 "reason": "Versie is identiek aan het origineel."})
                continue
            if fraction > 0.10:
                rejected.append({"project": name, "version": tag_version.name,
                                 "reason": f"{fraction:.1%} van de bytes verschilt: geen kalibratiebewerking."})
                continue
            tuned_identity = learn_identity(versions.metadata, tuned)
            if tuned_identity != identity:
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
            pair_id = hashlib.sha256(f"{original_sha}:{tuned_sha}".encode()).hexdigest()[:16]
            meta = versions.metadata
            pairs.append(TrainingPair(
                pair_id=pair_id, family_key=family_key, variant_key=variant_key,
                identity=identity, original=original, tuned=tuned, profile=profile,
                maps=versions.maps,
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
                    "read_method": "py-ols slot enumeration + RevisionTag",
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

    # ----- batch processing -------------------------------------------------
    def scan(self, sources: dict[str, Path], reader: Any, *,
             export_dirs: list[Path] | None = None, max_file_bytes: int = 1 << 30,
             max_files: int | None = None, retries: int = 3, retry_delay: float = 1.0,
             progress: Callable[[str], None] | None = None,
             stop: threading.Event | None = None,
             on_file: Callable[[str, int, int], None] | None = None) -> dict[str, Any]:
        """``on_file(source, done, found)`` is called after each project."""
        say = progress or (lambda message: None)
        meter = ResourceMeter()
        export_hashes: set[str] = set()
        for directory in export_dirs or []:
            for entry in sorted(directory.iterdir()):
                if entry.is_file():
                    export_hashes.add(_stream_sha256(entry, meter))
        per_source: dict[str, dict[str, Any]] = {}
        run_id = self.db.execute(
            "INSERT INTO runs(started_at,status) VALUES(?,?)", (_now(), "running")
        ).lastrowid
        self.db.commit()
        processed = 0
        for source_key, root in sources.items():
            stats = per_source.setdefault(source_key, {
                "root": str(root), "status": "available", "found": 0, "hashed": 0,
                "parsed": 0, "confirmed_pairs": 0, "stage1_pairs": 0, "new_pairs": 0, "rejected": 0,
                "retry_pending": 0, "skipped_unchanged": 0, "errors": [],
            })
            try:
                walker = list(_walk_ols(root))
            except OSError as exc:
                stats["status"] = f"unavailable: {exc.strerror or exc}"
                say(f"Bron {source_key} niet bereikbaar ({root}): {exc.strerror or exc}")
                continue
            stats["found"] = len(walker)
            if on_file:
                on_file(source_key, 0, len(walker))
            for done_index, path in enumerate(walker, 1):
                if stop is not None and stop.is_set():
                    stats["status"] = "interrupted"
                    break
                if max_files is not None and processed >= max_files:
                    stats["status"] = "batch_limit_reached"
                    break
                key = str(path)
                row = self.db.execute(
                    "SELECT size,mtime_ns,status,attempts FROM files WHERE path=?", (key,)
                ).fetchone()
                attempt = 0
                while True:
                    try:
                        st = path.stat()
                        if row and row[0] == st.st_size and row[1] == st.st_mtime_ns and row[2] in DONE_STATES:
                            stats["skipped_unchanged"] += 1
                            status = None
                            break
                        if st.st_size > max_file_bytes:
                            status, message, pair_ids, sha = "too_large", f"{st.st_size} bytes", [], None
                            break
                        # One network read: copy to local scratch while hashing.
                        local = self.root / "scratch" / f"current{path.suffix}"
                        sha = _copy_and_hash(path, local, meter)
                        stats["hashed"] += 1
                        if path.stat().st_mtime_ns != st.st_mtime_ns:
                            raise OSError("bestand veranderde tijdens het lezen")
                        try:
                            pairs, rejected = self.pairs_from_project(
                                local, reader, sha, export_hashes, display_name=path.name)
                        finally:
                            local.unlink(missing_ok=True)
                        stats["parsed"] += 1
                        stats["rejected"] += len(rejected)
                        pair_ids = []
                        for pair in pairs:
                            self._save_pair(pair)
                            inserted = self.db.execute(
                                "INSERT OR IGNORE INTO pairs(pair_id,source,project_sha256,original_sha256,"
                                "tuned_sha256,profile,family_key,first_seen,variant_key,project_name) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (pair.pair_id, source_key, pair.record["project_sha256"],
                                 pair.record["original_sha256"], pair.record["tuned_sha256"],
                                 pair.profile, pair.family_key, _now(), pair.variant_key,
                                 pair.record.get("project_name", "")),
                            ).rowcount
                            stats["new_pairs"] += inserted
                            pair_ids.append(pair.pair_id)
                        stats["confirmed_pairs"] += len(pairs)
                        stats["stage1_pairs"] += sum(1 for p in pairs if p.profile == "stage1")
                        status = "parsed_with_pairs" if pairs else (
                            "rejected" if rejected else "parsed_no_pairs")
                        message = json.dumps(rejected, ensure_ascii=False) if rejected else ""
                        break
                    except OSError as exc:
                        attempt += 1
                        if attempt > retries:
                            status, message, pair_ids, sha = "retry_pending", str(exc), [], None
                            stats["retry_pending"] += 1
                            stats["errors"].append(f"{path.name}: {exc}")
                            break
                        time.sleep(retry_delay * attempt)
                    except Exception as exc:  # parser failure: record, continue
                        status, message, pair_ids, sha = "rejected", f"parser: {exc}", [], None
                        stats["rejected"] += 1
                        break
                if status is None:
                    if on_file:
                        on_file(source_key, done_index, len(walker))
                    continue
                try:
                    st_size, st_mtime = (path.stat().st_size, path.stat().st_mtime_ns)
                except OSError:
                    st_size, st_mtime = (row[0], row[1]) if row else (None, None)
                self.db.execute(
                    "INSERT INTO files(path,source,size,mtime_ns,sha256,status,message,attempts,pair_ids,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                    "size=excluded.size,mtime_ns=excluded.mtime_ns,sha256=COALESCE(excluded.sha256,files.sha256),"
                    "status=excluded.status,message=excluded.message,attempts=files.attempts+1,"
                    "pair_ids=excluded.pair_ids,updated_at=excluded.updated_at",
                    (key, source_key, st_size, st_mtime, sha, status, message, 1,
                     json.dumps(pair_ids), _now()),
                )
                self.db.commit()  # checkpoint per project
                processed += 1
                meter.files += 1
                say(f"{source_key}: {path.name} – {STATUS_TEXT.get(status, status)}")
                if on_file:
                    on_file(source_key, done_index, len(walker))
        summary = {"sources": per_source, "resources": meter.summary()}
        self.db.execute(
            "UPDATE runs SET finished_at=?,status=?,stats=? WHERE id=?",
            (_now(), "finished", json.dumps(summary), run_id),
        )
        self.db.commit()
        return summary

    def all_pairs(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT pair_id FROM pairs ORDER BY pair_id")]

    def pair_counts_by_source(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for source, profile, count in self.db.execute(
            "SELECT source,profile,COUNT(*) FROM pairs GROUP BY source,profile"
        ):
            out.setdefault(source, {})[profile] = count
        return out

    def train(self, *, profile: str = "stage1", holdout_projects: list[str] | None = None,
              settings: dict[str, Any] | None = None, activate: bool = True,
              progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Train per software variant, loading one variant's pairs at a time."""

        say = progress or (lambda message: None)
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
        groups: dict[tuple[str, str], list[str]] = {}
        other_groups: dict[tuple[str, str], list[str]] = {}
        excluded = []
        for row in rows:
            key = (row["family_key"], row["variant_key"])
            if row["pair_id"] in holdout:
                continue
            if row["profile"] == profile:
                groups.setdefault(key, []).append(row["pair_id"])
            else:
                other_groups.setdefault(key, []).append(row["pair_id"])
                excluded.append({"pair_id": row["pair_id"], "profile": row["profile"],
                                 "reason": "Geen zuiver profiel; alleen gebruikt om andere bewerkingen te herkennen."})
        if not groups:
            raise ValueError("Geen bevestigde trainingsparen voor dit profiel (na uitsluiting van testdata).")
        previous = None
        try:
            previous = self.store.load_active()
        except ValueError:
            previous = None
        builder = PackageBuilder({**DEFAULT_SETTINGS, **(settings or {})})
        for index, key in enumerate(sorted(groups), 1):
            ids = sorted(groups[key])
            say(f"Leren variant {index}/{len(groups)}: {key[0]} ({len(ids)} paren)")
            learn_ids = ids[:MAX_LEARNING_PAIRS]
            target = [self.load_pair(pid) for pid in learn_ids]
            extra = (self.load_pair(pid) for pid in ids[MAX_LEARNING_PAIRS:])
            others = [self.load_pair(pid) for pid in sorted(other_groups.get(key, []))[:MAX_LEARNING_PAIRS]]
            builder.add_variant(target, others, extra_known=extra, profile=profile)
            del target, others
        package = builder.finish(holdout=sorted(holdout), excluded=excluded)
        self.store.install(package, activate=activate)
        previous_pairs = {p["pair_id"] for p in previous["training_selection"]} if previous else set()
        current_pairs = {p["pair_id"] for p in package["training_selection"]}
        return {
            "package_id": package.package_id,
            "previous_package_id": previous.package_id if previous else None,
            "pairs_used": len(current_pairs),
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
        with os.scandir(current) as entries:
            items = sorted(entries, key=lambda e: e.name)
        for entry in items:
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            elif entry.name.lower().endswith(".ols"):
                yield Path(entry.path)
