from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import ExitStack
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .paths import canonical_path


_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_REPORTED_ERRORS = 1000


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_entries(roots: list[Path]) -> Iterator[tuple[Path, os.stat_result, str]]:
    pending: list[Path] = []
    for root in roots:
        try:
            info = root.lstat()
        except OSError:
            continue
        if root.is_symlink():
            yield root, info, "symlink"
        elif root.is_dir():
            pending.append(root)
        elif root.is_file():
            yield root, info, "file"

    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if entry.is_symlink():
                            yield path, info, "symlink"
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append(path)
                        elif entry.is_file(follow_symlinks=False):
                            yield path, info, "file"
                    except OSError:
                        yield path, None, "unreadable"
        except OSError:
            yield directory, None, "unreadable_directory"


def scan_inventory(
    *,
    roots: list[Path],
    database: Any,
    hash_duplicate_candidates: bool = False,
    verify_indexed_hashes: bool = False,
    file_list_path: Path | None = None,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Scan filesystem metadata without changing sources or loading the corpus into RAM."""
    if not roots:
        raise ValueError("At least one inventory root is required.")

    normalized_roots = [Path(canonical_path(root)) for root in roots]
    for output_path in (file_list_path, report_path):
        if output_path is None:
            continue
        resolved_output = output_path.resolve()
        for root in normalized_roots:
            resolved_root = root.resolve()
            if resolved_output == resolved_root or resolved_root in resolved_output.parents:
                raise ValueError(
                    f"Inventory output {resolved_output} must not be inside source root {resolved_root}."
                )
    root_results = []
    for root in normalized_roots:
        if not root.exists():
            root_results.append({"path": str(root), "status": "missing"})
        elif not os.access(root, os.R_OK):
            root_results.append({"path": str(root), "status": "unreadable"})
        else:
            root_results.append({"path": str(root), "status": "available"})

    errors: list[dict[str, str]] = []
    error_count = 0
    skipped_symlinks = 0
    repeated_paths = 0
    file_count = 0
    ols_count = 0
    other_count = 0
    total_bytes = 0
    indexed_match_count = 0
    indexed_changed_count = 0
    indexed_unverified_count = 0
    not_indexed_count = 0
    hashed_count = 0
    hash_errors = 0

    with (
        tempfile.TemporaryDirectory(prefix="ols-inventory-") as temporary_directory,
        ExitStack() as cleanup,
    ):
        connection = sqlite3.connect(Path(temporary_directory) / "inventory.sqlite3")
        cleanup.callback(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                file_kind TEXT NOT NULL,
                scan_status TEXT NOT NULL,
                indexed_status TEXT NOT NULL,
                sha256 TEXT,
                duplicate_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE indexed_files (
                path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL
            )
            """
        )
        indexed_cursor = database.connection.execute(
            "SELECT path, sha256, size_bytes, modified_ns FROM source_files"
        )
        connection.executemany(
            "INSERT OR REPLACE INTO indexed_files VALUES (?, ?, ?, ?)",
            (
                (
                    os.path.normcase(os.path.abspath(row["path"])),
                    row["sha256"],
                    row["size_bytes"],
                    row["modified_ns"],
                )
                for row in indexed_cursor
            ),
        )
        connection.commit()

        for path, info, scan_status in _walk_entries(normalized_roots):
            if scan_status == "unreadable":
                error_count += 1
                if len(errors) < _MAX_REPORTED_ERRORS:
                    errors.append({"path": str(path), "error": "Entry could not be stat'ed."})
                continue
            if scan_status == "unreadable_directory":
                error_count += 1
                if len(errors) < _MAX_REPORTED_ERRORS:
                    errors.append({"path": str(path), "error": "Directory could not be scanned."})
                continue
            normalized_path = os.path.normcase(os.path.abspath(path))
            if connection.execute(
                "SELECT 1 FROM files WHERE path=?",
                (normalized_path,),
            ).fetchone():
                repeated_paths += 1
                continue
            if scan_status == "symlink":
                skipped_symlinks += 1
            file_kind = (
                "symlink"
                if scan_status == "symlink"
                else "ols"
                if path.suffix.casefold() == ".ols"
                else "other"
            )
            if scan_status == "file":
                file_count += 1
                total_bytes += info.st_size
                if file_kind == "ols":
                    ols_count += 1
                else:
                    other_count += 1
            indexed = connection.execute(
                "SELECT sha256,size_bytes,modified_ns FROM indexed_files WHERE path=?",
                (normalized_path,),
            ).fetchone()
            sha256 = None
            if file_kind == "ols" and indexed is None:
                indexed_status = "not_indexed"
                not_indexed_count += 1
            elif file_kind == "ols" and (
                indexed["size_bytes"] != info.st_size
                or indexed["modified_ns"] != info.st_mtime_ns
            ):
                indexed_status = "changed_stat"
                indexed_changed_count += 1
            elif file_kind == "ols" and verify_indexed_hashes:
                try:
                    sha256 = _sha256_file(path)
                    hashed_count += 1
                    if sha256 == indexed["sha256"]:
                        indexed_status = "hash_match"
                        indexed_match_count += 1
                    else:
                        indexed_status = "changed_content"
                        indexed_changed_count += 1
                except OSError as exc:
                    indexed_status = "hash_error"
                    hash_errors += 1
                    if len(errors) < _MAX_REPORTED_ERRORS:
                        errors.append({"path": str(path), "error": f"Hash failed: {exc}"})
            elif file_kind == "ols":
                indexed_status = "stat_match_hash_unverified"
                indexed_unverified_count += 1
            else:
                indexed_status = "not_applicable"

            try:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO files(
                        path,size_bytes,modified_ns,file_kind,scan_status,indexed_status,sha256
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        normalized_path,
                        info.st_size,
                        info.st_mtime_ns,
                        file_kind,
                        scan_status,
                        indexed_status,
                        sha256,
                    ),
                )
                if cursor.rowcount == 0:
                    repeated_paths += 1
            except sqlite3.Error as exc:
                raise RuntimeError(f"Could not record inventory entry {path}: {exc}") from exc

        possible_duplicate_groups = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(file_count), 0)
            FROM (
                SELECT size_bytes, COUNT(*) AS file_count
                FROM files
                WHERE scan_status='file'
                GROUP BY size_bytes HAVING COUNT(*) > 1
            )
            """
        ).fetchone()
        exact_duplicate_groups = 0
        exact_duplicate_files = 0
        if hash_duplicate_candidates:
            candidate_sizes = connection.execute(
                """
                SELECT size_bytes
                FROM files
                WHERE scan_status='file'
                GROUP BY size_bytes HAVING COUNT(*) > 1
                """
            )
            while size_rows := candidate_sizes.fetchmany(1000):
                for (size_bytes,) in size_rows:
                    last_path = ""
                    while True:
                        candidates = connection.execute(
                            """
                            SELECT path FROM files
                            WHERE scan_status='file' AND size_bytes=?
                              AND sha256 IS NULL AND path>?
                            ORDER BY path LIMIT 1000
                            """,
                            (size_bytes, last_path),
                        ).fetchall()
                        if not candidates:
                            break
                        for (candidate_path,) in candidates:
                            last_path = candidate_path
                            try:
                                digest = _sha256_file(Path(candidate_path))
                                hashed_count += 1
                                connection.execute(
                                    "UPDATE files SET sha256=? WHERE path=?",
                                    (digest, candidate_path),
                                )
                            except OSError as exc:
                                hash_errors += 1
                                if len(errors) < _MAX_REPORTED_ERRORS:
                                    errors.append(
                                        {
                                            "path": candidate_path,
                                            "error": f"Hash failed: {exc}",
                                        }
                                    )
                        connection.commit()
            connection.commit()
            duplicate_rows = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(file_count-1),0)
                FROM (
                    SELECT size_bytes,sha256,COUNT(*) AS file_count
                    FROM files
                    WHERE sha256 IS NOT NULL AND scan_status='file'
                    GROUP BY size_bytes,sha256 HAVING COUNT(*) > 1
                )
                """
            ).fetchone()
            exact_duplicate_groups, exact_duplicate_files = duplicate_rows
            connection.execute(
                """
                UPDATE files
                SET duplicate_count=(
                    SELECT COUNT(*) FROM files same
                    WHERE same.size_bytes=files.size_bytes
                      AND same.sha256=files.sha256
                      AND same.sha256 IS NOT NULL
                )
                """
            )
            connection.commit()

        indexed_not_found_count = connection.execute(
            """
            SELECT COUNT(*) FROM indexed_files i
            WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.path=i.path)
            """
        ).fetchone()[0]

        if file_list_path is not None:
            file_list_path.parent.mkdir(parents=True, exist_ok=True)
            with file_list_path.open("w", encoding="utf-8", newline="\n") as output:
                cursor = connection.execute(
                    """
                    SELECT path,size_bytes,modified_ns,file_kind,scan_status,
                           indexed_status,sha256,duplicate_count
                    FROM files ORDER BY path
                    """
                )
                while rows := cursor.fetchmany(1000):
                    for row in rows:
                        output.write(
                            json.dumps(
                                {
                                    "path": row[0],
                                    "size_bytes": row[1],
                                    "modified_ns": row[2],
                                    "kind": row[3],
                                    "scan_status": row[4],
                                    "indexed_status": row[5],
                                    "sha256": row[6],
                                    "exact_duplicate_count": row[7],
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
    report = {
        "mode": "dry_run",
        "roots": root_results,
        "scan": {
            "files": file_count,
            "ols_projects": ols_count,
            "other_files": other_count,
            "total_bytes": total_bytes,
            "total_gib": round(total_bytes / (1024**3), 3),
            "symlinks_skipped": skipped_symlinks,
            "repeated_paths_from_overlapping_roots": repeated_paths,
            "unreadable_entries": error_count,
        },
        "indexed_comparison": {
            "indexed_source_files_not_observed_in_selected_roots": indexed_not_found_count,
            "not_indexed_ols_files": not_indexed_count,
            "stat_changed": indexed_changed_count,
            "stat_matched_hash_unverified": indexed_unverified_count,
            "full_hash_match": indexed_match_count,
            "hash_errors": hash_errors,
        },
        "duplicates": {
            "possible_same_size_groups": possible_duplicate_groups[0],
            "files_in_possible_same_size_groups": possible_duplicate_groups[1],
            "hashing_enabled_for_candidates": hash_duplicate_candidates,
            "exact_duplicate_groups": exact_duplicate_groups,
            "exact_duplicate_files_beyond_first": exact_duplicate_files,
        },
        "hashing": {
            "full_file_hashes_computed": hashed_count,
            "verify_indexed_hashes_enabled": verify_indexed_hashes,
            "hash_strategy": (
                "Full SHA-256 for indexed OLS files and repeated-size candidates."
                if verify_indexed_hashes and hash_duplicate_candidates
                else "Full SHA-256 only for repeated-size candidates."
                if hash_duplicate_candidates
                else "Full SHA-256 for indexed OLS files."
                if verify_indexed_hashes
                else "No content hashes computed."
            ),
        },
        "errors": errors,
        "omitted_error_count": max(0, error_count + hash_errors - len(errors)),
        "file_list_path": str(file_list_path.resolve()) if file_list_path else None,
        "report_path": str(report_path.resolve()) if report_path else None,
    }
    report["inventory_run_id"] = database.store_inventory_run(report)
    return report
