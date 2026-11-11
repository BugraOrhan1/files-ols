from __future__ import annotations

import os
import ctypes
import json
import hashlib
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__ as APPLICATION_VERSION
from .importer import ImportOutcome, import_sources, stream_sha256
from .reader import OlsProjectReader


def _normalized(path: Path) -> str:
    return os.path.abspath(path)


def register_source(database: Any, path: Path) -> dict[str, Any]:
    normalized = _normalized(path)
    if path.exists() and not path.is_dir():
        raise ValueError(f"Data source must be a directory: {path}")
    if path.is_dir() and not os.access(path, os.R_OK):
        status, error = "unavailable", "Directory is not readable."
    elif not path.exists():
        status, error = "unavailable", "Directory does not currently exist."
    else:
        status, error = "available", ""
    key = str(uuid.uuid4())
    source_id = database.register_source(
        source_key=key, path=normalized, status=status, error=error
    )
    return test_source(database, source_id)


def test_source(database: Any, source_id: int) -> dict[str, Any]:
    source = database.get_source(source_id)
    if source is None:
        raise ValueError(f"Unknown registered source ID {source_id}.")
    path = Path(source["path"])
    if not path.exists():
        status, error = "unavailable", "Directory does not currently exist."
    elif not path.is_dir():
        status, error = "unavailable", "Registered path is not a directory."
    elif not os.access(path, os.R_OK):
        status, error = "unavailable", "Directory is not readable."
    else:
        try:
            with os.scandir(path) as entries:
                next(entries, None)
            status, error = "available", ""
        except OSError as exc:
            status, error = "unavailable", f"{type(exc).__name__}: {exc}"
    database.update_source_status(source_id, status=status, error=error)
    result = database.get_source(source_id)
    if result is None:
        raise RuntimeError("Registered source disappeared during access check.")
    return result


def move_source(database: Any, source_id: int, new_path: Path) -> dict[str, Any]:
    source = database.get_source(source_id)
    if source is None:
        raise ValueError(f"Unknown registered source ID {source_id}.")
    normalized = _normalized(new_path)
    if new_path.exists() and not new_path.is_dir():
        raise ValueError(f"Data source must be a directory: {new_path}")
    status = "available" if new_path.is_dir() and os.access(new_path, os.R_OK) else "unavailable"
    error = "" if status == "available" else "Moved directory is not currently accessible."
    database.update_source_path(
        source_id, path=normalized, status=status, error=error
    )
    result = database.get_source(source_id)
    if result is None:
        raise RuntimeError("Registered source disappeared during move.")
    return result


def scan_registered_source(
    database: Any,
    source_id: int,
    *,
    progress_every: int = 10000,
    force_recheck: bool = False,
    benchmark_bytes: int = 16 * 1024 * 1024,
) -> dict[str, Any]:
    if progress_every < 1:
        raise ValueError("Progress interval must be positive.")
    if benchmark_bytes < 0:
        raise ValueError("Benchmark byte limit cannot be negative.")
    source = test_source(database, source_id)
    if source["status"] != "available":
        report = {
            "source_id": source_id,
            "path": source["path"],
            "status": "unavailable",
            "files": 0,
            "ols_files": 0,
            "total_size_bytes": 0,
            "errors": 1,
            "error_samples": [source["last_error"]],
            "network_read_benchmark": None,
            "note": "No scan was started because Windows access checks failed.",
        }
        report["scan_run_id"] = database.store_source_scan_run(
            source_id, "unavailable", report
        )
        raise OSError(
            f"Registered source {source_id} is unavailable at {source['path']}: "
            f"{source['last_error']}. Register or move the correct location, then run source test."
        )

    root = Path(source["path"])
    scan_run_id = database.start_or_resume_source_scan(
        source_id, _normalized(root)
    )
    started_at = time.perf_counter()
    files_since_progress = 0
    symlinks = 0
    interrupted = False
    prefix_read: dict[str, Any] | None = None
    error_messages: list[str] = []
    current_directory: dict | None = None
    current_directory_files = 0

    def persist_progress(status: str = "scanning") -> None:
        progress = database.source_inventory_report(source_id, scan_run_id)
        progress.update({
            "source_id": source_id,
            "path": str(root),
            "status": status,
            "scan_run_id": scan_run_id,
            "symlinks_skipped": symlinks,
            "resume_supported": True,
            "last_progress_at": time.time(),
        })
        with database.connection:
            database.connection.execute(
                "UPDATE source_scan_runs SET status=?,report_json=? WHERE id=?",
                (
                    status,
                    json.dumps(progress, ensure_ascii=False, sort_keys=True),
                    scan_run_id,
                ),
            )

    try:
        while True:
            current_directory = database.claim_source_scan_directory(scan_run_id)
            if current_directory is None:
                break
            directory = Path(current_directory["path"])
            directory_errors: list[dict] = []
            observations: list[dict] = []
            child_directories: list[str] = []
            current_directory_files = 0
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_symlink():
                                symlinks += 1
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                child_directories.append(_normalized(Path(entry.path)))
                                if len(child_directories) >= 500:
                                    database.enqueue_source_scan_directories(
                                        scan_run_id,source_id,child_directories
                                    )
                                    child_directories.clear()
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            path = Path(entry.path)
                            info = entry.stat(follow_symlinks=False)
                            extension = path.suffix.casefold().lstrip(".")
                            observations.append({
                                "path": _normalized(path),
                                "filename": path.name,
                                "extension": extension,
                                "file_kind": "ols" if extension == "ols" else "other",
                                "size_bytes": info.st_size,
                                "modified_ns": info.st_mtime_ns,
                            })
                            current_directory_files += 1
                            files_since_progress += 1
                            if len(observations) >= 500:
                                database.record_source_inventory_batch(
                                    source_id,scan_run_id,observations,
                                    force_recheck=force_recheck,
                                )
                                observations.clear()
                            if (
                                prefix_read is None and benchmark_bytes
                                and extension == "ols"
                            ):
                                try:
                                    sample_limit = min(benchmark_bytes, info.st_size)
                                    sample_started = time.perf_counter()
                                    bytes_read = 0
                                    with path.open("rb") as stream:
                                        while bytes_read < sample_limit:
                                            chunk = stream.read(
                                                min(1024 * 1024,sample_limit - bytes_read)
                                            )
                                            if not chunk:
                                                break
                                            bytes_read += len(chunk)
                                    elapsed = max(time.perf_counter() - sample_started,1e-9)
                                    prefix_read = {
                                        "path": str(path),
                                        "bytes_read": bytes_read,
                                        "seconds": round(elapsed,6),
                                        "mib_per_second": round(
                                            bytes_read / (1024 * 1024) / elapsed,3
                                        ),
                                        "method": "sequential prefix read; not SHA-256",
                                    }
                                except OSError as exc:
                                    directory_errors.append({
                                        "path": _normalized(path),
                                        "scope": "file",
                                        "error_type": type(exc).__name__,
                                        "message": str(exc),
                                    })
                            if files_since_progress >= progress_every:
                                database.record_source_inventory_batch(
                                    source_id,scan_run_id,observations,
                                    force_recheck=force_recheck,
                                )
                                observations.clear()
                                files_since_progress = 0
                                persist_progress()
                                print(
                                    f"Inventory source {source_id}: "
                                    f"{database.source_inventory_report(source_id,scan_run_id)['found_paths']:,} "
                                    f"paths recorded; current directory {directory}"
                                )
                        except OSError as exc:
                            directory_errors.append({
                                "path": _normalized(Path(entry.path)),
                                "scope": "file",
                                "error_type": type(exc).__name__,
                                "message": str(exc),
                            })
                    if child_directories:
                        database.enqueue_source_scan_directories(
                            scan_run_id,source_id,child_directories
                        )
                    if observations:
                        database.record_source_inventory_batch(
                            source_id,scan_run_id,observations,
                            force_recheck=force_recheck,
                        )
            except OSError as exc:
                directory_errors.append({
                    "path": _normalized(directory),
                    "scope": "directory",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                })
            database.record_source_scan_errors(
                scan_run_id,source_id,directory_errors
            )
            directory_status = "retryable" if directory_errors else "complete"
            database.finish_source_scan_directory(
                current_directory["id"],
                status=directory_status,
                files_seen=current_directory_files,
                error="; ".join(item["message"] for item in directory_errors[:10]),
            )
            if len(error_messages) < 100:
                error_messages.extend(
                    f"{item['path']}: {item['error_type']}: {item['message']}"
                    for item in directory_errors[:100-len(error_messages)]
                )
            persist_progress()
    except KeyboardInterrupt:
        interrupted = True
        if current_directory is not None:
            database.finish_source_scan_directory(
                current_directory["id"],
                status="interrupted",
                files_seen=current_directory_files,
                error="Scan interrupted; directory will be rescanned on resume.",
            )

    remaining = database.connection.execute(
        """
        SELECT COUNT(*) FROM source_scan_directories
        WHERE scan_run_id=? AND status!='complete'
        """,
        (scan_run_id,),
    ).fetchone()[0]
    network_source = str(root).startswith("\\\\") or str(root).startswith("//")
    access_check = test_source(database,source_id) if network_source else source
    status = (
        "interrupted" if interrupted
        else "unavailable" if access_check["status"] != "available"
        else "scan_warnings" if remaining
        else "available"
    )
    report = database.source_inventory_report(source_id,scan_run_id)
    report.update({
        "source_id":source_id,
        "path":str(root),
        "status":status,
        "scan_run_id":scan_run_id,
        "elapsed_seconds":round(time.perf_counter()-started_at,3),
        "prefix_read_benchmark":prefix_read,
        "force_recheck":force_recheck,
        "queue_pending":database.count_index_queue(
            source_id,statuses=("pending","interrupted","in_progress","retryable")
        ),
        "errors":report["errors"],
        "error_samples":report.pop("error_samples"),
        "files":report["found_paths"],
        "ols_files":report["ols_paths"],
        "total_size_bytes":report["physical_bytes"],
        "new_ols_files":report["new_ols_files"],
        "stat_changed_ols_files":report["stat_changed_ols_files"],
        "cached_fingerprint_files":report["cached_fingerprint_files"],
        "network_read_benchmark":prefix_read,
        "symlinks_skipped":symlinks,
        "resume_supported":True,
        "note":(
            "Metadata inventory only. Stat matches are not treated as content identity. "
            "File size and modification time are not cryptographic proof; "
            "same-name/size groups are candidates only."
        ),
    })
    database.finish_source_scan(
        source_id=source_id,
        status=status,
        found_files=report["found_paths"],
        total_size_bytes=report["physical_bytes"],
        indexed_files=report["ols_paths"]-report["unindexed_ols_locations"],
        new_files=report["new_locations"],
        changed_files=report["changed_locations"],
        error="; ".join(error_messages[:100]),
    )
    database.finish_source_scan_run(
        scan_run_id,
        source_id=source_id,
        status=status,
        report=report,
        complete=status=="available",
    )
    with database.connection:
        database.connection.execute(
            "UPDATE source_scan_runs SET report_json=? WHERE id=?",
            (json.dumps(report,ensure_ascii=False,sort_keys=True),scan_run_id),
        )
    return report


def _hash_benchmark_candidates(
    database: Any,
    source_scans: list[dict],
    *,
    max_files: int,
    max_bytes: int,
) -> tuple[list[dict], list[dict]]:
    if not 1 <= max_files <= 25:
        raise ValueError("Hash benchmark file limit must be between 1 and 25.")
    if not 1 <= max_bytes <= 2 * 1024 * 1024 * 1024:
        raise ValueError("Hash benchmark byte limit must be between 1 byte and 2 GiB.")

    buckets = (
        ("small_le_1_mib", 0, 1024 * 1024),
        ("medium_1_to_16_mib", 1024 * 1024 + 1, 16 * 1024 * 1024),
        ("large_16_to_128_mib", 16 * 1024 * 1024 + 1, 128 * 1024 * 1024),
        ("xlarge_128_mib_to_2_gib", 128 * 1024 * 1024 + 1, max_bytes),
    )
    candidates: list[dict] = []
    empty_sources = []
    for source_scan in source_scans:
        source_id = source_scan["source_id"]
        report = source_scan.get("inventory")
        if not report or source_scan["status"] != "available":
            continue
        if not report["ols_paths"]:
            empty_sources.append({
                "source_id":source_id,
                "name":source_scan["name"],
                "path":source_scan["path"],
                "status":"no_ols_files_found",
            })
            continue
        for bucket_name, lower, upper in buckets:
            count = database.connection.execute(
                """
                SELECT COUNT(*) FROM file_locations
                WHERE source_id=? AND last_seen_scan_id=? AND is_present=1
                  AND extension='ols' AND size_bytes BETWEEN ? AND ?
                """,
                (source_id,source_scan["scan_run_id"],lower,upper),
            ).fetchone()[0]
            if not count:
                continue
            row = database.connection.execute(
                """
                SELECT path,size_bytes,modified_ns FROM file_locations
                WHERE source_id=? AND last_seen_scan_id=? AND is_present=1
                  AND extension='ols' AND size_bytes BETWEEN ? AND ?
                ORDER BY size_bytes,path LIMIT 1 OFFSET ?
                """,
                (
                    source_id,source_scan["scan_run_id"],lower,upper,
                    int(count)//2,
                ),
            ).fetchone()
            if row is not None:
                candidates.append({
                    "source_id":source_id,
                    "source_name":source_scan["name"],
                    "source_root":source_scan["path"],
                    "path":row["path"],
                    "size_bytes":int(row["size_bytes"]),
                    "modified_ns":int(row["modified_ns"]),
                    "size_class":bucket_name,
                })

    selected = []
    skipped = []
    used_bytes = 0
    for candidate in candidates:
        if len(selected) >= max_files:
            skipped.append({**candidate,"reason":"file_count_limit"})
            continue
        if candidate["size_bytes"] > max_bytes - used_bytes:
            skipped.append({**candidate,"reason":"total_byte_budget"})
            continue
        selected.append(candidate)
        used_bytes += candidate["size_bytes"]
    return selected, [*empty_sources, *skipped]


def run_hash_benchmark(
    database: Any,
    source_scans: list[dict],
    *,
    max_files: int = 25,
    max_bytes: int = 2 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    candidates, skipped = _hash_benchmark_candidates(
        database,source_scans,max_files=max_files,max_bytes=max_bytes
    )
    started_at = time.perf_counter()
    hashed: list[dict] = []
    failures: list[dict] = []
    by_source: dict[int,dict[str,float|int]] = {}
    total_bytes = 0
    for candidate in candidates:
        path = Path(candidate["path"])
        try:
            before = path.stat()
            if (
                before.st_size != candidate["size_bytes"]
                or before.st_mtime_ns != candidate["modified_ns"]
            ):
                raise OSError("File changed after inventory; hash sample was not used.")
            started = time.perf_counter()
            digest = stream_sha256(path)
            elapsed = max(time.perf_counter() - started,1e-9)
            after = path.stat()
            if (
                after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                raise OSError("File changed during hashing; hash sample was discarded.")
            database.record_benchmark_hash(
                source_id=candidate["source_id"],
                path=candidate["path"],
                sha256=digest,
                size_bytes=before.st_size,
                modified_ns=before.st_mtime_ns,
            )
            sample = {
                **candidate,
                "sha256":digest,
                "bytes_read":before.st_size,
                "seconds":round(elapsed,6),
                "mib_per_second":round(
                    before.st_size / (1024*1024) / elapsed,3
                ),
                "status":"hashed",
            }
            hashed.append(sample)
            total_bytes += before.st_size
            stats = by_source.setdefault(
                candidate["source_id"],
                {"source_name":candidate["source_name"],"files":0,"bytes_read":0,
                 "seconds":0.0},
            )
            stats["files"] += 1
            stats["bytes_read"] += before.st_size
            stats["seconds"] += elapsed
        except OSError as exc:
            check = test_source(database,candidate["source_id"])
            failures.append({
                **candidate,
                "status":"retryable_source_error"
                    if check["status"] != "available"
                    else "file_error",
                "source_status":check["status"],
                "error":f"{type(exc).__name__}: {exc}",
            })
    elapsed_total = max(time.perf_counter() - started_at,1e-9)
    total_ols_bytes = sum(
        int((item.get("inventory") or {}).get("ols_physical_bytes",0))
        for item in source_scans
        if item.get("inventory") and item["status"] == "available"
    )
    average_rate = total_bytes / (1024*1024) / elapsed_total if total_bytes else 0
    source_rates = {}
    for source_id, stats in by_source.items():
        seconds = float(stats["seconds"])
        source_rates[source_id] = {
            "source_name":stats["source_name"],
            "files":stats["files"],
            "bytes_read":stats["bytes_read"],
            "seconds":round(seconds,6),
            "mib_per_second":round(
                int(stats["bytes_read"]) / (1024*1024) / max(seconds,1e-9),3
            ),
        }
    return {
        "status":"completed" if hashed else "not_completed",
        "started_at":datetime.now(timezone.utc).isoformat(),
        "files_selected":len(candidates),
        "files_hashed":len(hashed),
        "bytes_read":total_bytes,
        "elapsed_seconds":round(elapsed_total,6),
        "average_mib_per_second":round(average_rate,3),
        "per_source":source_rates,
        "samples":hashed,
        "failed_files":failures,
        "skipped":skipped,
        "limits":{"max_files":max_files,"max_total_bytes":max_bytes},
        "full_dataset_estimate":{
            "physical_ols_bytes":total_ols_bytes,
            "basis":"Only fully completed source inventories are included.",
            "estimated_seconds_at_sample_rate":(
                round(total_ols_bytes/(average_rate*1024*1024),1)
                if average_rate else None
            ),
            "uncertainty":(
                "Indicative only; sample files, SMB/network load, disk speed, caching, "
                "file size distribution, and retry overhead can materially change the time."
            ),
        },
        "note":(
            "Only selected OLS samples were fully read and SHA-256 hashed. "
            "No remaining corpus files were content-read."
        ),
    }


class _FileChangedDuringHash(Exception):
    def __init__(
        self, message: str, *, started_at: str, finished_at: str,
        bytes_read: int = 0, read_seconds: float = 0.0,
        sha256_update_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.started_at = started_at
        self.finished_at = finished_at
        self.bytes_read = bytes_read
        self.read_seconds = read_seconds
        self.sha256_update_seconds = sha256_update_seconds


def _hash_location(
    job: dict,
    chunk_size: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict:
    path = Path(job["path"])
    started_at = datetime.now(timezone.utc).isoformat()
    bytes_read = 0
    reported_bytes = 0
    read_seconds = 0.0
    sha256_update_seconds = 0.0

    def report_progress(*, force: bool = False) -> None:
        nonlocal reported_bytes
        delta = bytes_read - reported_bytes
        if progress_callback is not None and (
            force or delta >= 16 * 1024 * 1024
        ):
            progress_callback(bytes_read,delta)
            reported_bytes = bytes_read

    try:
        before = path.stat()
        if before.st_size != job["size_bytes"] or before.st_mtime_ns != job["modified_ns"]:
            raise _FileChangedDuringHash(
                "File changed after inventory.",
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                read_started = time.perf_counter()
                chunk = stream.read(chunk_size)
                read_seconds += time.perf_counter() - read_started
                if not chunk:
                    break
                update_started = time.perf_counter()
                digest.update(chunk)
                sha256_update_seconds += time.perf_counter() - update_started
                bytes_read += len(chunk)
                report_progress()
        after = path.stat()
        report_progress(force=True)
        if (
            after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or bytes_read != before.st_size
        ):
            report_progress(force=True)
            raise _FileChangedDuringHash(
                "File changed or was truncated during hashing.",
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                bytes_read=bytes_read,
                read_seconds=read_seconds,
                sha256_update_seconds=sha256_update_seconds,
            )
    except _FileChangedDuringHash:
        report_progress(force=True)
        raise
    except OSError as exc:
        report_progress(force=True)
        exc.hash_started_at = started_at
        exc.hash_finished_at = datetime.now(timezone.utc).isoformat()
        exc.hash_bytes_read = bytes_read
        exc.hash_read_seconds = read_seconds
        exc.hash_sha256_update_seconds = sha256_update_seconds
        raise
    return {
        "sha256":digest.hexdigest(),
        "bytes_read":bytes_read,
        "started_at":started_at,
        "finished_at":datetime.now(timezone.utc).isoformat(),
        "read_seconds":read_seconds,
        "sha256_update_seconds":sha256_update_seconds,
    }


def _set_worker_execution_state(active: bool) -> None:
    if os.name != "nt":
        return
    continuous = 0x80000000
    system_required = 0x00000001
    flags = continuous | system_required if active else continuous
    result = ctypes.windll.kernel32.SetThreadExecutionState(flags)
    if not result:
        raise ctypes.WinError()


def run_hashing(
    database: Any,
    *,
    workers: int = 2,
    chunk_size: int = 1024 * 1024,
    max_files: int = 1000,
    retry_failed: bool = False,
    progress_every: int = 25,
    progress_owner_token: str | None = None,
    sync_queue: bool = True,
) -> dict[str, Any]:
    if not 1 <= workers <= 2:
        raise ValueError("Network hash workers must be between 1 and 2.")
    if max_files < 0:
        raise ValueError("Maximum files must be zero (unlimited) or positive.")
    if progress_every < 1:
        raise ValueError("Progress interval must be positive.")
    slots = database.list_source_slots()
    selected_sources = [slot for slot in slots if slot["source_id"] is not None]
    if len(selected_sources) != 5:
        raise ValueError("Hashing requires all five registered source roots.")
    source_ids = [int(slot["source_id"]) for slot in selected_sources]
    statuses = {
        source_id:test_source(database,source_id)
        for source_id in source_ids
    }
    if sync_queue:
        database.sync_hash_jobs(source_ids,retry_failed=retry_failed)
    for source_id, source in statuses.items():
        if source["status"] == "available":
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE hash_jobs SET status='RETRY_PENDING',error='',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE source_id=? AND status='SOURCE_UNAVAILABLE'
                    """,
                    (source_id,),
                )
        else:
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE hash_jobs SET status='SOURCE_UNAVAILABLE',
                        error=?,updated_at=CURRENT_TIMESTAMP
                    WHERE source_id=? AND status IN ('HASH_PENDING','RETRY_PENDING')
                    """,
                    (source["last_error"],source_id),
                )
    run_id = database.begin_hash_run(
        worker_count=workers,chunk_size=chunk_size,
        worker_owner_token=progress_owner_token,
    )
    if progress_owner_token is not None:
        database.update_hash_worker(
            owner_token=progress_owner_token,active_run_id=run_id
        )
    started_clock = time.perf_counter()
    run_started_at = datetime.now(timezone.utc).isoformat()
    completed = 0
    bytes_read = 0
    errors = 0
    by_source: dict[int,dict[str,Any]] = {}
    recent_rates: list[float] = []
    outcome_counts: dict[str,int] = {}
    run_status = "BATCH_COMPLETE"
    timing_lock = threading.Lock()
    timing_totals = {
        "read_call_seconds":0.0,
        "sha256_update_seconds":0.0,
        "database_write_seconds":0.0,
        "file_task_seconds":0.0,
    }

    def add_timing(name: str, seconds: float) -> None:
        with timing_lock:
            timing_totals[name] += seconds

    def record_result(job: dict, future: Any) -> None:
        nonlocal completed,bytes_read,errors
        try:
            result = future.result()
            add_timing("read_call_seconds",result["read_seconds"])
            add_timing(
                "sha256_update_seconds",result["sha256_update_seconds"]
            )
            add_timing(
                "file_task_seconds",
                max(
                    (datetime.fromisoformat(result["finished_at"]) -
                     datetime.fromisoformat(result["started_at"])).total_seconds(),
                    0.0,
                ),
            )
            database_started = time.perf_counter()
            status = database.finish_hash_job(
                job=job,run_id=run_id,status="HASHED",
                started_at=result["started_at"],
                finished_at=result["finished_at"],
                bytes_read=result["bytes_read"],sha256=result["sha256"],
            )
            add_timing(
                "database_write_seconds",
                time.perf_counter()-database_started,
            )
            byte_count = result["bytes_read"] if status == "HASHED" else 0
            elapsed = max(
                (datetime.fromisoformat(result["finished_at"]) -
                 datetime.fromisoformat(result["started_at"])).total_seconds(),
                1e-9,
            )
            if status == "HASHED":
                bytes_read += byte_count
                rate = byte_count / (1024*1024) / elapsed
                recent_rates.append(rate)
                stats = by_source.setdefault(
                    job["source_id"],
                    {"source_name":next(
                        slot["name"] for slot in selected_sources
                        if slot["source_id"] == job["source_id"]
                    ),"files":0,"bytes_read":0,"seconds":0.0},
                )
                stats["files"] += 1
                stats["bytes_read"] += byte_count
                stats["seconds"] += elapsed
        except OSError as exc:
            check = test_source(database,job["source_id"])
            status = (
                "SOURCE_UNAVAILABLE" if check["status"] != "available"
                else "RETRY_PENDING"
            )
            message = f"{type(exc).__name__}: {exc}"
            partial_bytes = getattr(exc,"hash_bytes_read",0)
            add_timing("read_call_seconds",getattr(exc,"hash_read_seconds",0.0))
            add_timing(
                "sha256_update_seconds",
                getattr(exc,"hash_sha256_update_seconds",0.0),
            )
            bytes_read += partial_bytes
            database_started = time.perf_counter()
            database.finish_hash_job(
                job=job,run_id=run_id,status=status,
                started_at=getattr(exc,"hash_started_at",datetime.now(timezone.utc).isoformat()),
                finished_at=getattr(exc,"hash_finished_at",datetime.now(timezone.utc).isoformat()),
                bytes_read=partial_bytes,error=message,
            )
            add_timing(
                "database_write_seconds",
                time.perf_counter()-database_started,
            )
            errors += 1
        except _FileChangedDuringHash as exc:
            status = "FILE_CHANGED"
            message = f"{type(exc).__name__}: {exc}"
            now = datetime.now(timezone.utc).isoformat()
            bytes_read += exc.bytes_read
            add_timing("read_call_seconds",exc.read_seconds)
            add_timing(
                "sha256_update_seconds",exc.sha256_update_seconds
            )
            database_started = time.perf_counter()
            database.finish_hash_job(
                job=job,run_id=run_id,status=status,started_at=exc.started_at,
                finished_at=exc.finished_at,bytes_read=exc.bytes_read,error=message,
            )
            add_timing(
                "database_write_seconds",
                time.perf_counter()-database_started,
            )
            errors += 1
        except Exception as exc:
            status = "HASH_FAILED"
            message = f"{type(exc).__name__}: {exc}"
            now = datetime.now(timezone.utc).isoformat()
            add_timing("read_call_seconds",getattr(exc,"hash_read_seconds",0.0))
            add_timing(
                "sha256_update_seconds",
                getattr(exc,"hash_sha256_update_seconds",0.0),
            )
            database_started = time.perf_counter()
            database.finish_hash_job(
                job=job,run_id=run_id,status=status,started_at=now,
                finished_at=now,bytes_read=0,error=message,
            )
            add_timing(
                "database_write_seconds",
                time.perf_counter()-database_started,
            )
            errors += 1
        completed += 1
        outcome_counts[status] = outcome_counts.get(status,0) + 1
        if completed % progress_every == 0:
            print(
                f"Hash run {run_id}: {completed:,} files completed; "
                f"{bytes_read:,} bytes hashed."
            )

    active: dict[Any,dict] = {}
    try:
        with ThreadPoolExecutor(
            max_workers=workers,thread_name_prefix="ols-hash"
        ) as executor:
            startup_barrier=threading.Barrier(workers+1)
            startup_futures=[]
            try:
                for _ in range(workers):
                    startup_futures.append(executor.submit(startup_barrier.wait))
                startup_barrier.wait()
            except BaseException:
                startup_barrier.abort()
                raise
            for future in startup_futures:
                future.result()
            while True:
                control = database.hash_control_status()["status"]
                if control == "paused":
                    run_status = "PAUSED"
                elif control == "cancelled":
                    run_status = "CANCELLED"
                if run_status in {"PAUSED","CANCELLED"}:
                    break
                while len(active) < workers and (
                    max_files == 0 or completed + len(active) < max_files
                ):
                    jobs = database.claim_hash_jobs(
                        run_id=run_id,source_ids=source_ids,
                        limit=min(workers-len(active),
                                  max_files-completed-len(active)
                                  if max_files else workers),
                    )
                    if not jobs:
                        break
                    for job in jobs:
                        progress_callback = None
                        if progress_owner_token is not None:
                            def progress_callback(
                                completed_bytes: int, delta: int, *,
                                current_job: dict = job,
                            ) -> None:
                                database_started = time.perf_counter()
                                try:
                                    database.record_hash_progress(
                                    job_id=current_job["id"],run_id=run_id,
                                    owner_token=progress_owner_token,
                                    bytes_hashed=completed_bytes,bytes_delta=delta,
                                    )
                                finally:
                                    add_timing(
                                        "database_write_seconds",
                                        time.perf_counter()-database_started,
                                    )
                        active[executor.submit(
                            _hash_location,job,chunk_size,progress_callback
                        )] = job
                if not active:
                    break
                done,_ = wait(active,return_when=FIRST_COMPLETED)
                for future in done:
                    job = active.pop(future)
                    record_result(job,future)
                if max_files and completed >= max_files:
                    break
        if run_status == "BATCH_COMPLETE":
            control = database.hash_control_status()["status"]
            if control == "paused":
                run_status = "PAUSED"
            elif control == "cancelled":
                run_status = "CANCELLED"
    except KeyboardInterrupt:
        run_status = "INTERRUPTED"
        for future,job in list(active.items()):
            record_result(job,future)
            active.pop(future,None)
    finally:
        with database.connection:
            database.connection.execute(
                """
                UPDATE hash_jobs SET status='RETRY_PENDING',run_id=NULL,
                    error='Task interrupted before a complete hash was committed.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE run_id=? AND status='HASHING'
                """,
                (run_id,),
            )
    elapsed = max(time.perf_counter()-started_clock,1e-9)
    speed = bytes_read / (1024*1024) / elapsed if bytes_read else 0
    for stats in by_source.values():
        stats["seconds"] = round(stats["seconds"],6)
        stats["mib_per_second"] = round(
            stats["bytes_read"]/(1024*1024)/max(stats["seconds"],1e-9),3
        )
    queue = database.hash_queue_report(source_ids)
    if run_status == "BATCH_COMPLETE" and queue["totals"]["pending"] == 0:
        run_status = "COMPLETE"
    report = {
        "run_id":run_id,
        "status":run_status,
        "started_at":run_started_at,
        "files_completed":completed,
        "bytes_read":bytes_read,
        "elapsed_seconds":round(elapsed,3),
        "average_mib_per_second":round(speed,3),
        "recent_average_mib_per_second":round(
            sum(recent_rates[-100:])/len(recent_rates[-100:]),3
        ) if recent_rates else 0,
        "by_source":by_source,
        "outcomes":outcome_counts,
        "errors":errors,
        "worker_count":workers,
        "chunk_size":chunk_size,
        "max_files":max_files,
        "queue":queue,
        "estimate_remaining_seconds":(
            round(queue["totals"]["pending_physical_bytes"]/(speed*1024*1024),1)
            if speed and "pending_physical_bytes" in queue["totals"] else None
        ),
        "estimate_uncertainty":(
            "Extrapolation only; network load, file size distribution, caching, "
            "SMB latency and retries can materially change the remaining time."
        ),
        "timings": {
            **{
                name:round(seconds,6)
                for name,seconds in timing_totals.items()
            },
            "worker_wall_seconds":round(elapsed,6),
            "read_call_semantics":(
                "Time blocked in file.read(); includes SMB wait or cache latency, "
                "but is not a direct network-only measurement."
            ),
            "concurrency_note":(
                "Read, SHA-256, and file-task seconds are summed across readers "
                "and can exceed wall time."
            ),
        },
    }
    database.finish_hash_run(run_id,status=run_status,report=report)
    return report


def run_hash_worker(
    database: Any,
    *,
    workers: int = 2,
    batch_size: int = 10,
    chunk_size: int = 1024 * 1024,
    batch_delay_seconds: float = 1.0,
    retry_limit: int = 5,
    retry_delay_seconds: float = 60.0,
    progress_every: int = 25,
    max_batches: int | None = None,
) -> dict[str, Any]:
    if not 1 <= workers <= 2:
        raise ValueError("Network hash workers must be between 1 and 2.")
    if batch_size < 1:
        raise ValueError("Hash worker batch size must be positive.")
    if not 64 * 1024 <= chunk_size <= 16 * 1024 * 1024:
        raise ValueError("Hash chunk size must be between 64 KiB and 16 MiB.")
    if batch_delay_seconds < 0 or retry_delay_seconds < 0:
        raise ValueError("Worker delays cannot be negative.")
    if retry_limit < 1:
        raise ValueError("Hash retry limit must be positive.")
    if progress_every < 1:
        raise ValueError("Progress interval must be positive.")
    if max_batches is not None and max_batches < 1:
        raise ValueError("Maximum batch count must be positive.")

    owner_token = uuid.uuid4().hex
    database.acquire_hash_worker(
        owner_token=owner_token,process_id=os.getpid(),workers=workers,
        batch_size=batch_size,chunk_size=chunk_size,
        batch_delay_seconds=batch_delay_seconds,retry_limit=retry_limit,
        retry_delay_seconds=retry_delay_seconds,
    )
    started_clock = time.perf_counter()
    batches = 0
    files = 0
    bytes_read = 0
    errors = 0
    first_batch = True
    resume_after_paused_batch = False
    final_status = "stopped"
    final_error = ""
    last_batch: dict[str, Any] | None = None

    def wait_for(seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            control = database.hash_control_status()["status"]
            if control == "cancelled":
                return False
            paused = control == "paused"
            database.update_hash_worker(
                owner_token=owner_token,status="paused" if paused else "running"
            )
            time.sleep(min(1.0,max(0.0,deadline-time.monotonic())))
        return database.hash_control_status()["status"] != "cancelled"

    try:
        _set_worker_execution_state(True)
        while True:
            control = database.hash_control_status()["status"]
            if control == "cancelled":
                final_status = "stopped"
                break
            if control == "paused":
                database.update_hash_worker(owner_token=owner_token,status="paused")
                time.sleep(1.0)
                continue
            if resume_after_paused_batch:
                source_ids = [
                    int(slot["source_id"])
                    for slot in database.list_source_slots()
                    if slot["source_id"] is not None
                ]
                queue = database.hash_queue_report(source_ids)
                if queue["totals"]["pending"] == 0:
                    final_status = "complete"
                    break
                resume_after_paused_batch = False
            database.update_hash_worker(owner_token=owner_token,status="running")
            batch = run_hashing(
                database,workers=workers,chunk_size=chunk_size,
                max_files=batch_size,progress_every=progress_every,
                progress_owner_token=owner_token,sync_queue=first_batch,
            )
            first_batch = False
            batches += 1
            files += batch["files_completed"]
            bytes_read += batch["bytes_read"]
            errors += batch["errors"]
            last_batch = batch

            source_ids = [
                int(slot["source_id"]) for slot in database.list_source_slots()
                if slot["source_id"] is not None
            ]
            changed_jobs = database.connection.execute(
                """
                SELECT id,source_id,path FROM hash_jobs WHERE status='FILE_CHANGED'
                """
            ).fetchall()
            for job in changed_jobs:
                try:
                    stats = Path(job["path"]).stat()
                except OSError as exc:
                    source = test_source(database,int(job["source_id"]))
                    database.mark_hash_job_source_unavailable(
                        int(job["id"]),
                        f"{type(exc).__name__}: {exc}; source {source['status']}.",
                    )
                    continue
                database.refresh_changed_hash_job(
                    job_id=int(job["id"]),size_bytes=stats.st_size,
                    modified_ns=stats.st_mtime_ns,retry_limit=retry_limit,
                )
            exhausted = database.exhaust_hash_retries(source_ids,retry_limit)
            if exhausted:
                errors += exhausted
            database.update_hash_worker(
                owner_token=owner_token,status="running",active_run_id=None,
                batches_delta=1,files_delta=batch["files_completed"],
                errors_delta=batch["errors"]+exhausted,
            )

            if batch["status"] in {"PAUSED","CANCELLED","INTERRUPTED"}:
                if batch["status"] == "PAUSED":
                    resume_after_paused_batch = True
                    continue
                final_status = "stopped"
                break
            if batch["status"] not in {"BATCH_COMPLETE","COMPLETE"}:
                raise RuntimeError(
                    f"Hash batch {batch['run_id']} ended in {batch['status']}."
                )
            queue = database.hash_queue_report(source_ids)
            if queue["totals"]["pending"] == 0:
                final_status = "complete"
                break
            if max_batches is not None and batches >= max_batches:
                final_status = "stopped"
                break

            statuses = queue["queue_statuses"]
            can_run = statuses.get("HASH_PENDING",0) + statuses.get("RETRY_PENDING",0)
            waiting_on_sources = statuses.get("SOURCE_UNAVAILABLE",0) > 0
            if not can_run and not waiting_on_sources:
                final_status = "needs_attention"
                final_error = "Pending locations remain, but no hash job is automatically retryable."
                break
            delay = (
                retry_delay_seconds
                if batch["errors"] or not batch["files_completed"] or waiting_on_sources
                else batch_delay_seconds
            )
            if delay and not wait_for(delay):
                final_status = "stopped"
                break
    except KeyboardInterrupt:
        final_status = "stopped"
    except Exception as exc:
        final_status = "failed"
        final_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            database.release_hash_worker(
                owner_token=owner_token,status=final_status,error=final_error
            )
        finally:
            _set_worker_execution_state(False)

    queue = build_hashing_report(database)
    return {
        "status":final_status,
        "batches_completed":batches,
        "files_completed":files,
        "bytes_read":bytes_read,
        "errors":errors,
        "elapsed_seconds":round(time.perf_counter()-started_clock,3),
        "last_batch":last_batch,
        "queue":queue,
        "estimate_uncertainty":(
            "Extrapolation only; network load, file size distribution, caching, "
            "SMB latency and retries can materially change the remaining time."
        ),
    }


def build_hashing_report(
    database: Any, *, limit: int = 100
) -> dict[str, Any]:
    slots = database.list_source_slots()
    source_ids = [
        int(slot["source_id"]) for slot in slots if slot["source_id"] is not None
    ]
    queue = database.hash_queue_report(source_ids)
    latest = database.connection.execute(
        "SELECT id,status,report_json,started_at,finished_at FROM hash_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    source_filter = ",".join("?" for _ in source_ids)
    recent_runs = database.connection.execute(
        """
        SELECT id,started_at,finished_at,report_json FROM hash_runs
        WHERE status IN ('BATCH_COMPLETE','COMPLETE') AND finished_at IS NOT NULL
        ORDER BY id DESC LIMIT 10
        """
    ).fetchall()
    recent_run_reports = [
        {
            "id":int(row["id"]),
            "started_at":row["started_at"],
            "finished_at":row["finished_at"],
            "report":json.loads(row["report_json"] or "{}"),
        }
        for row in recent_runs
    ]
    recent_run_reports.reverse()

    def as_utc(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return (
            parsed.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        )

    recent_bytes = sum(
        int(item["report"].get("bytes_read",0))
        for item in recent_run_reports
    )
    recent_files = sum(
        int(item["report"].get("outcomes",{}).get(
            "HASHED",item["report"].get("files_completed",0)
        ))
        for item in recent_run_reports
    )
    recent_start = (
        as_utc(recent_run_reports[0]["started_at"])
        if recent_run_reports else None
    )
    recent_end = (
        as_utc(recent_run_reports[-1]["finished_at"])
        if recent_run_reports else None
    )
    recent_seconds = (
        max((recent_end-recent_start).total_seconds(),0.0)
        if recent_start and recent_end else 0.0
    )
    recent_rate = (
        recent_bytes/(1024*1024)/recent_seconds
        if recent_seconds else 0
    )
    recent_performance = {
        "measurement":"wall_clock_over_recent_completed_hash_runs",
        "runs":len(recent_run_reports),
        "window_started_at":(
            recent_start.isoformat() if recent_start else None
        ),
        "window_finished_at":recent_end.isoformat() if recent_end else None,
        "window_seconds":round(recent_seconds,3),
        "files_hashed":recent_files,
        "bytes_read":recent_bytes,
        "mib_per_second":round(recent_rate,3),
        "files_per_hour":(
            round(recent_files*3600/recent_seconds,1)
            if recent_seconds else 0
        ),
        "errors":sum(
            int(item["report"].get("errors",0))
            for item in recent_run_reports
        ),
    }
    for source in queue["by_source"]:
        source_id = str(source["source_id"])
        source_bytes = sum(
            int(
                item["report"].get("by_source",{}).get(
                    source_id,{}
                ).get("bytes_read",0)
            )
            for item in recent_run_reports
        )
        source["recent_bytes_read"] = source_bytes
        source["mib_per_second"] = (
            round(source_bytes/(1024*1024)/recent_seconds,3)
            if recent_seconds else 0
        )
        source["mib_per_second_basis"] = (
            "shared wall-clock window across the ten most recent completed runs"
        )
    pending_bytes = database.connection.execute(
        f"""
        SELECT COALESCE(SUM(size_bytes),0) FROM file_locations
        WHERE file_kind='ols' AND is_present=1 AND sha256 IS NULL
          AND source_id IN ({source_filter})
        """,
        source_ids,
    ).fetchone()[0]
    queue["totals"]["pending_physical_bytes"] = int(pending_bytes)
    queue["totals"]["remaining_seconds_at_recent_rate"] = (
        round(pending_bytes/(recent_rate*1024*1024),1) if recent_rate else None
    )
    queue["totals"]["remaining_eta_basis"] = (
        "Actual un-hashed OLS bytes divided by the wall-clock rate across "
        "the ten most recent completed hash runs."
    )
    queue["recent_hash_performance"] = recent_performance
    queue["exact_duplicate_groups"] = database.list_duplicate_content_locations(
        limit=limit,source_ids=source_ids
    )
    worker = database.hash_worker_status()
    worker_elapsed = 0.0
    if worker.get("started_at"):
        worker_elapsed = max(
            (
                datetime.now(timezone.utc)
                - as_utc(worker["started_at"])
            ).total_seconds(),
            0.0,
        )
    worker_rate = (
        worker["bytes_read"]/(1024*1024)/worker_elapsed
        if worker_elapsed else 0
    )
    successful_since_start = 0
    if worker.get("started_at"):
        successful_since_start = int(database.connection.execute(
            """
            SELECT COUNT(*) FROM hash_attempts a
            JOIN hash_runs r ON r.id=a.run_id
            WHERE a.status='HASHED' AND r.started_at>=?
            """,
            (worker["started_at"],),
        ).fetchone()[0])
    worker["performance"] = {
        "measurement":"since_worker_start_wall_clock",
        "elapsed_seconds":round(worker_elapsed,3),
        "bytes_read":int(worker["bytes_read"]),
        "average_mib_per_second":round(worker_rate,3),
        "successfully_hashed_files":successful_since_start,
        "files_per_hour":(
            round(successful_since_start*3600/worker_elapsed,1)
            if worker_elapsed else 0
        ),
    }
    size_rows = database.connection.execute(
        f"""
        SELECT size_bytes,bytes_read,started_at,finished_at FROM (
            SELECT size_bytes,bytes_read,started_at,finished_at
            FROM hash_attempts WHERE status='HASHED'
              AND source_id IN ({source_filter})
            ORDER BY id DESC LIMIT 100
        )
        """,
        source_ids,
    ).fetchall()
    sizes = sorted(int(row["size_bytes"]) for row in size_rows)
    if sizes:
        queue["recent_file_sizes"] = {
            "successful_hashes":len(sizes),
            "mean_bytes":round(sum(sizes)/len(sizes)),
            "median_bytes":sizes[len(sizes)//2],
            "p90_bytes":sizes[int((len(sizes)-1)*0.9)],
            "min_bytes":sizes[0],
            "max_bytes":sizes[-1],
            "measurement":"last 100 successfully hashed locations",
        }
    else:
        queue["recent_file_sizes"] = {
            "successful_hashes":0,
            "mean_bytes":None,
            "median_bytes":None,
            "p90_bytes":None,
            "min_bytes":None,
            "max_bytes":None,
            "measurement":"last 100 successfully hashed locations",
        }
    timing_runs = [
        item["report"].get("timings")
        for item in recent_run_reports
        if item["report"].get("timings")
    ]
    queue["recent_hash_timing_breakdown"] = (
        {
            "instrumented_runs":len(timing_runs),
            "read_call_seconds":round(sum(
                float(item.get("read_call_seconds",0))
                for item in timing_runs
            ),6),
            "sha256_update_seconds":round(sum(
                float(item.get("sha256_update_seconds",0))
                for item in timing_runs
            ),6),
            "database_write_seconds":round(sum(
                float(item.get("database_write_seconds",0))
                for item in timing_runs
            ),6),
            "worker_wall_seconds":round(sum(
                float(item.get("worker_wall_seconds",0))
                for item in timing_runs
            ),6),
            "instrumentation_note":(
                "File.read() time includes SMB waits or cache latency and is "
                "not a network-only measurement. Per-reader times are summed "
                "across concurrent readers."
            ),
        }
        if timing_runs else {
            "instrumented_runs":0,
            "read_call_seconds":None,
            "sha256_update_seconds":None,
            "database_write_seconds":None,
            "worker_wall_seconds":None,
            "instrumentation_note":(
                "The running worker was started before timing instrumentation "
                "was loaded; restart is intentionally avoided, so read and "
                "database-write breakdowns are unavailable for its existing runs."
            ),
        }
    )
    total_locations = int(queue["totals"]["ols_locations"] or 0)
    worker["progress_percent"] = (
        round(int(queue["totals"]["hashed"] or 0)*100/total_locations,4)
        if total_locations else 0
    )
    queue["worker"] = worker
    queue["latest_run"] = (
        {
            "id":latest["id"],
            "status":latest["status"],
            "started_at":latest["started_at"],
            "finished_at":latest["finished_at"],
            "report":json.loads(latest["report_json"]),
        }
        if latest else None
    )
    queue["duplicate_group_limit"] = limit
    queue["duplicate_report_note"] = (
        "Only full-file SHA-256 matches with equal size are included; groups are "
        "logical and original files are untouched."
    )
    return queue


def build_hashing_plan(database: Any) -> dict[str, Any]:
    slots = database.list_source_slots()
    sources = []
    source_ids = []
    for slot in slots:
        item = {
            "slot_id":slot["slot_id"],
            "name":slot["name"],
            "source_id":slot["source_id"],
            "path":slot["path"],
            "status":"unconfigured",
        }
        if slot["source_id"] is not None:
            source_id = int(slot["source_id"])
            source_ids.append(source_id)
            checked = test_source(database,source_id)
            item.update({
                "status":checked["status"],
                "error":checked["last_error"],
            })
        sources.append(item)
    source_filter = ",".join("?" for _ in source_ids)
    stats = database.connection.execute(
        f"""
        SELECT COUNT(*) AS locations,
               COALESCE(SUM(size_bytes),0) AS physical_bytes,
               SUM(CASE WHEN sha256 IS NOT NULL THEN 1 ELSE 0 END) AS hashed,
               COALESCE(SUM(CASE WHEN sha256 IS NULL THEN size_bytes ELSE 0 END),0)
                   AS pending_bytes
        FROM file_locations
        WHERE file_kind='ols' AND is_present=1
          AND source_id IN ({source_filter})
        """,
        source_ids,
    ).fetchone()
    missing_jobs = database.connection.execute(
        f"""
        SELECT COUNT(*) FROM file_locations l
        WHERE l.file_kind='ols' AND l.is_present=1
          AND l.source_id IN ({source_filter})
          AND NOT EXISTS (
              SELECT 1 FROM hash_jobs j
              WHERE j.source_id=l.source_id AND j.path=l.path
          )
        """,
        source_ids,
    ).fetchone()[0]
    return {
        "dry_run":True,
        "sources":sources,
        "only_configured_source_ids":source_ids,
        "all_five_sources_configured":len(source_ids)==5,
        "all_sources_available":len(source_ids)==5 and all(
            item["status"]=="available" for item in sources
        ),
        "ols_locations":int(stats["locations"] or 0),
        "hashed_locations":int(stats["hashed"] or 0),
        "pending_locations":int(stats["locations"] or 0)-int(stats["hashed"] or 0),
        "physical_bytes":int(stats["physical_bytes"] or 0),
        "pending_bytes":int(stats["pending_bytes"] or 0),
        "unmaterialized_queue_jobs":int(missing_jobs),
        "hash_queue_will_be_persisted_on_start":True,
        "source_files_opened_for_read":False,
        "note":(
            "Dry-run only checks configured-root accessibility and SQLite queue "
            "metadata; it does not read or hash OLS file contents."
        ),
    }


def run_multi_source_inventory(
    database: Any,
    *,
    progress_every: int = 10000,
    max_hash_files: int = 25,
    max_hash_bytes: int = 2 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    if progress_every < 1:
        raise ValueError("Progress interval must be positive.")
    source_scans: list[dict] = []
    for slot in database.list_source_slots():
        item = {
            "slot_id":slot["slot_id"],
            "name":slot["name"],
            "source_id":slot["source_id"],
            "path":slot["path"],
        }
        if slot["source_id"] is None:
            item.update({"status":"unconfigured","inventory":None})
            source_scans.append(item)
            continue
        try:
            checked = test_source(database,slot["source_id"])
            if checked["status"] != "available":
                item.update({
                    "status":"unavailable",
                    "last_error":checked["last_error"],
                    "inventory":None,
                    "previously_recorded_locations":slot["found_files"] or 0,
                    "previously_recorded_bytes":slot["total_size_bytes"] or 0,
                })
            else:
                report = scan_registered_source(
                    database,
                    slot["source_id"],
                    progress_every=progress_every,
                    benchmark_bytes=0,
                )
                item.update({
                    "status":report["status"],
                    "scan_run_id":report["scan_run_id"],
                    "inventory":report,
                })
        except OSError as exc:
            current = database.get_source(slot["source_id"])
            item.update({
                "status":"unavailable" if current and current["status"]!="available"
                    else "scan_warnings",
                "last_error":f"{type(exc).__name__}: {exc}",
                "inventory":None,
                "previously_recorded_locations":slot["found_files"] or 0,
                "previously_recorded_bytes":slot["total_size_bytes"] or 0,
            })
        source_scans.append(item)
        print(
            f"Source {slot['name']}: {item['status']} — "
            f"{(item.get('inventory') or {}).get('found_paths',0):,} paths"
        )

    complete_sources = [
        item for item in source_scans
        if item["status"] == "available" and item.get("inventory")
    ]
    inventory_sources = [
        item for item in source_scans if item.get("inventory") is not None
    ]
    all_configured_complete = all(
        item["status"] == "available"
        for item in source_scans
    )
    benchmark = (
        run_hash_benchmark(
            database,
            source_scans,
            max_files=max_hash_files,
            max_bytes=max_hash_bytes,
        )
        if complete_sources
        else {
            "status":"not_run",
            "reason":"No source completed a metadata inventory in this run.",
            "files_hashed":0,
            "bytes_read":0,
        }
    )
    for item in source_scans:
        inventory = item.get("inventory")
        scan_run_id = item.get("scan_run_id")
        source_id = item.get("source_id")
        if inventory is None or scan_run_id is None or source_id is None:
            continue
        inventory["unhashed_ols_locations"] = database.connection.execute(
            """
            SELECT COUNT(*) FROM file_locations
            WHERE source_id=? AND last_seen_scan_id=? AND is_present=1
              AND extension='ols' AND sha256 IS NULL
            """,
            (source_id,scan_run_id),
        ).fetchone()[0]
    run_ids = [
        item["scan_run_id"] for item in inventory_sources if item.get("scan_run_id")
    ]
    possible_duplicates = database.inventory_duplicate_candidates(run_ids)
    totals = {
        "found_paths":sum(item["inventory"]["found_paths"] for item in inventory_sources),
        "ols_paths":sum(item["inventory"]["ols_paths"] for item in inventory_sources),
        "physical_bytes":sum(
            item["inventory"]["physical_bytes"] for item in inventory_sources
        ),
        "ols_physical_bytes":sum(
            item["inventory"]["ols_physical_bytes"] for item in inventory_sources
        ),
        "known_locations":sum(
            item["inventory"]["known_locations"] for item in inventory_sources
        ),
        "new_locations":sum(
            item["inventory"]["new_locations"] for item in inventory_sources
        ),
        "changed_locations":sum(
            item["inventory"]["changed_locations"] for item in inventory_sources
        ),
        "unhashed_ols_locations":sum(
            item["inventory"]["unhashed_ols_locations"] for item in inventory_sources
        ),
        "unindexed_ols_locations":sum(
            item["inventory"]["unindexed_ols_locations"] for item in inventory_sources
        ),
        "sources_with_inventory":len(inventory_sources),
        "sources_complete":len(complete_sources),
        "sources_incomplete":len(inventory_sources)-len(complete_sources),
        "inaccessible_sources":sum(
            item["status"] in ("unavailable","unconfigured") for item in source_scans
        ),
        "incomplete_directories":sum(
            (item.get("inventory") or {}).get("directories_incomplete",0)
            for item in source_scans
        ),
        "scan_errors":sum(
            (item.get("inventory") or {}).get("errors",0) for item in source_scans
        ),
        "possible_duplicate_groups_by_filename_and_size":len(possible_duplicates),
        "possible_duplicate_locations_beyond_first":sum(
            item["locations_count"]-1 for item in possible_duplicates
        ),
    }
    extension_totals: dict[str,dict[str,int]] = {}
    for item in inventory_sources:
        for extension in item["inventory"]["extensions"]:
            key = extension["extension"] or "(no extension)"
            totals_for_extension = extension_totals.setdefault(
                key,{"files":0,"bytes":0}
            )
            totals_for_extension["files"] += extension["files"]
            totals_for_extension["bytes"] += extension["bytes"]
    report = {
        "workflow":"multi_source_metadata_inventory",
        "generated_at":datetime.now(timezone.utc).isoformat(),
        "status":"complete" if all_configured_complete else "partial",
        "all_configured_sources_complete":all_configured_complete,
        "sources":source_scans,
        "totals":totals,
        "extension_totals":extension_totals,
        "possible_duplicate_groups":possible_duplicates,
        "duplicate_basis":(
            "Filename+size groups are candidates only. Exact duplicates require "
            "matching full-file SHA-256 hashes."
        ),
        "hash_benchmark":benchmark,
        "database_deduplication_summary":database.source_dedup_summary(),
        "reference_validation_changed":False,
        "source_files_modified":False,
    }
    report["inventory_run_id"] = database.store_inventory_run(report)
    return report


def run_source_queue(
    database: Any,
    source_id: int,
    *,
    reader: OlsProjectReader,
    max_file_bytes: int,
    batch_size: int,
) -> dict[str, Any]:
    database.interrupt_active_source_queue(
        source_id, "Previous indexing attempt interrupted; retry after source access is restored."
    )
    source = test_source(database, source_id)
    if source["status"] != "available":
        raise OSError(f"Registered source {source_id} is unavailable: {source['last_error']}")
    if max_file_bytes <= 0:
        raise ValueError("Maximum file size must be positive.")
    parser_reprocess_queued = database.queue_missing_parser_fingerprint(
        source_id=source_id,
        parser_name=getattr(reader, "parser_name", "unknown"),
        parser_version=getattr(reader, "parser_version", "unknown"),
        application_version=APPLICATION_VERSION,
        limit=batch_size,
    )
    items = database.claim_source_queue_items(source_id, batch_size)
    counts: dict[str, int] = {}
    outcomes: list[dict[str, str]] = []
    cached_hashes_reused = 0
    for item in items:
        outcome: ImportOutcome | None = None
        stage = "stat"
        try:
            path = Path(item["path"])
            stats = path.stat()
            if stats.st_size != item["size_bytes"] or stats.st_mtime_ns != item["modified_ns"]:
                database.finish_source_queue_item(
                    item["id"],
                    status="retryable",
                    message="File changed after discovery; requeued with current size/time.",
                )
                database.queue_source_file(
                    source_id=source_id,
                    path=item["path"],
                    size_bytes=stats.st_size,
                    modified_ns=stats.st_mtime_ns,
                    force_recheck=True,
                )
                counts["pending"] = counts.get("pending", 0) + 1
                outcomes.append({"path": item["path"], "status": "pending"})
                continue
            if stats.st_size > max_file_bytes:
                outcome = next(import_sources(
                    sources=[path],
                    reader=reader,
                    database=database,
                    max_file_bytes=max_file_bytes,
                    progress=False,
                ))
                database.finish_source_queue_item(
                    item["id"],
                    status=outcome.status,
                    message=outcome.message,
                )
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
                outcomes.append({"path": item["path"], "status": outcome.status})
                continue
            stage = "hash"
            digest = item["sha256"]
            if digest is None:
                digest = stream_sha256(path)
            else:
                cached_hashes_reused += 1
            after_hash = path.stat()
            if (
                after_hash.st_size != stats.st_size
                or after_hash.st_mtime_ns != stats.st_mtime_ns
            ):
                database.finish_source_queue_item(
                    item["id"],
                    status="retryable",
                    message="File changed while its content was being hashed.",
                )
                database.queue_source_file(
                    source_id=source_id,
                    path=item["path"],
                    size_bytes=after_hash.st_size,
                    modified_ns=after_hash.st_mtime_ns,
                    force_recheck=True,
                )
                counts["pending"] = counts.get("pending", 0) + 1
                outcomes.append({"path": item["path"], "status": "pending"})
                continue
            parser_name = getattr(reader, "parser_name", "unknown")
            parser_version = getattr(reader, "parser_version", "unknown")
            existing = database.find_processed_file_content(
                sha256=digest,
                size_bytes=stats.st_size,
                parser_name=parser_name,
                parser_version=parser_version,
                application_version=APPLICATION_VERSION,
            )
            if existing is not None:
                database.record_file_location(
                    source_id=source_id,
                    path=item["path"],
                    file_kind="ols",
                    size_bytes=stats.st_size,
                    modified_ns=stats.st_mtime_ns,
                    sha256=digest,
                    content_id=existing["content_id"],
                    state="duplicate_content_skipped",
                )
                database.finish_source_queue_item(
                    item["id"],
                    status="duplicate_content_skipped",
                    sha256=digest,
                    message=(
                        f"Exact content already processed as project "
                        f"{existing['project_id']} with this parser fingerprint."
                    ),
                    content_id=existing["content_id"],
                )
                counts["duplicate_content_skipped"] = (
                    counts.get("duplicate_content_skipped", 0) + 1
                )
                outcomes.append(
                    {"path": item["path"], "status": "duplicate_content_skipped"}
                )
                continue
            outcome = next(import_sources(
                sources=[Path(item["path"])],
                reader=reader,
                database=database,
                max_file_bytes=max_file_bytes,
                progress=False,
                known_hashes={item["path"]: digest},
            ))
            stage = "store"
            queue_status = outcome.status
            content_id = None
            project_id = database.project_id_for_source_file(
                item["path"], sha256=outcome.sha256
            )
            if outcome.sha256:
                content_id = database.store_file_content(
                    sha256=outcome.sha256,
                    size_bytes=stats.st_size,
                    file_kind="ols",
                    parser_name=parser_name,
                    parser_version=parser_version,
                    application_version=APPLICATION_VERSION,
                    processing_status=queue_status,
                    project_id=project_id,
                )
                database.record_file_location(
                    source_id=source_id,
                    path=item["path"],
                    file_kind="ols",
                    size_bytes=stats.st_size,
                    modified_ns=stats.st_mtime_ns,
                    sha256=outcome.sha256,
                    content_id=content_id,
                    state=queue_status,
                )
            database.finish_source_queue_item(
                item["id"],
                status=queue_status,
                sha256=outcome.sha256,
                message=outcome.message,
                content_id=content_id,
            )
            if queue_status == "failed" and str(source["path"]).startswith("\\\\"):
                check = test_source(database, source_id)
                path = Path(item["path"])
                file_readable = False
                try:
                    with path.open("rb") as stream:
                        stream.read(1)
                    file_readable = True
                except OSError:
                    file_readable = False
                if check["status"] != "available":
                    queue_status = "retryable"
                    database.finish_source_queue_item(
                        item["id"],
                        status=queue_status,
                        message=outcome.message or check["last_error"],
                    )
                    database.update_source_status(
                        source_id,
                        status="unavailable",
                        error=check["last_error"] or outcome.message,
                    )
                elif not file_readable or outcome.failure_stage in ("stat", "hash"):
                    queue_status = "retryable"
                    database.finish_source_queue_item(
                        item["id"], status=queue_status,
                        message=outcome.message,
                    )
            counts[queue_status] = counts.get(queue_status, 0) + 1
            outcomes.append({"path": item["path"], "status": queue_status})
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            queue_status = "failed"
            if isinstance(exc, OSError):
                check = test_source(database, source_id)
                if check["status"] != "available" or stage in ("stat", "hash"):
                    queue_status = "retryable"
                    database.update_source_status(
                        source_id, status="unavailable", error=check["last_error"]
                    )
                else:
                    try:
                        with Path(item["path"]).open("rb") as stream:
                            stream.read(1)
                    except OSError:
                        queue_status = "retryable"
            database.finish_source_queue_item(
                item["id"], status=queue_status, message=message
            )
            counts[queue_status] = counts.get(queue_status, 0) + 1
            outcomes.append({"path": item["path"], "status": queue_status})
    indexed_path_count = database.update_source_indexed_count(source_id)
    return {
        "source_id": source_id,
        "batch_size": batch_size,
        "processed": len(items),
        "parser_fingerprint_reprocess_queued": parser_reprocess_queued,
        "cached_content_hashes_reused": cached_hashes_reused,
        "counts": counts,
        "indexed_paths_with_stored_records": indexed_path_count,
        "unique_ols_contents": database.source_dedup_summary()["unique_ols_contents"],
        "exact_duplicate_locations": database.source_dedup_summary()[
            "exact_duplicate_locations"
        ],
        "outcomes": outcomes,
        "remaining": database.count_index_queue(
            source_id,
            statuses=("pending", "interrupted", "in_progress", "failed", "retryable"),
        ),
    }
