from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .database import IndexDatabase
from .reader import OlsProjectReader, ReadIssue
from .paths import canonical_path


@dataclass(frozen=True)
class ImportOutcome:
    path: str
    status: str
    sha256: str = ""
    message: str = ""
    failure_stage: str = ""


def stream_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def iter_ols_files(sources: list[Path]) -> Iterator[Path]:
    for source in sources:
        # Resolve each top-level input once (symlinks, junctions, Windows 8.3
        # aliases) so every path recorded beneath it is in canonical form.
        source = Path(canonical_path(source))
        if source.is_file():
            candidates = (source,) if source.suffix.lower() == ".ols" else ()
        elif source.is_dir():
            candidates = _walk_ols_files(source)
        else:
            raise FileNotFoundError(f"Source path does not exist: {source}")
        for candidate in candidates:
            yield candidate


def _walk_ols_files(root: Path) -> Iterator[Path]:
    def on_error(error: OSError) -> None:
        raise error

    for current, dirs, files in os.walk(root, onerror=on_error):
        dirs.sort()
        for name in sorted(files):
            path = Path(current) / name
            if path.suffix.lower() == ".ols":
                yield path


def import_sources(
    *,
    sources: list[Path],
    reader: OlsProjectReader,
    database: IndexDatabase,
    max_file_bytes: int,
    progress: bool = True,
    known_hashes: dict[str, str] | None = None,
) -> Iterator[ImportOutcome]:
    for source in iter_ols_files(sources):
        source_path = os.path.abspath(source)
        job_id = database.start_job(source_path)
        stage = "stat"
        sha256 = ""
        try:
            stats = source.stat()
            if stats.st_size > max_file_bytes:
                issue = ReadIssue(
                    "file_size_limit",
                    f"File is {stats.st_size} bytes, above the configured "
                    f"{max_file_bytes}-byte limit.",
                    "error",
                )
                database.record_issue(job_id, issue.code, issue.message, issue.severity)
                database.finish_job(job_id, "blocked", issue.message)
                yield ImportOutcome(source_path, "blocked", message=issue.message)
                continue

            stage = "hash"
            sha256 = (
                known_hashes[source_path]
                if known_hashes is not None and source_path in known_hashes
                else stream_sha256(source)
            )
            database.job_hash(job_id, sha256)
            parser_name = getattr(reader, "parser_name", "unknown")
            parser_version = getattr(reader, "parser_version", "unknown")
            if database.is_unchanged(
                source_path,
                sha256,
                stats.st_size,
                stats.st_mtime_ns,
                parser_name,
                parser_version,
            ):
                database.finish_job(job_id, "skipped_unchanged")
                yield ImportOutcome(source_path, "skipped_unchanged", sha256)
                continue

            stage = "parse"
            snapshot = reader.inspect(source)
            if snapshot.format_version <= 0:
                message = "Parser did not recognize the OLS header."
                for issue in snapshot.issues:
                    database.record_issue(job_id, issue.code, issue.message, issue.severity)
                database.finish_job(job_id, "failed", message)
                yield ImportOutcome(
                    source_path, "failed", sha256, message, failure_stage="parse"
                )
                continue

            stage = "store"
            database.store_project(
                source_path=source_path,
                sha256=sha256,
                size=stats.st_size,
                modified_ns=stats.st_mtime_ns,
                snapshot=snapshot,
                job_id=job_id,
            )
            status = "partial" if snapshot.issues else "imported"
            message = "; ".join(issue.code for issue in snapshot.issues)
            database.finish_job(job_id, status, message)
            if progress:
                print(
                    f"{status}: {source.name} (format {snapshot.format_version}, "
                    f"{len(snapshot.versions)} version(s), {len(snapshot.maps)} map(s))"
                )
            yield ImportOutcome(source_path, status, sha256, message)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            database.record_issue(job_id, "import_exception", message, "error")
            database.finish_job(job_id, "failed", message)
            if progress:
                print(f"failed: {source.name}: {message}")
            yield ImportOutcome(
                source_path, "failed", sha256, message, failure_stage=stage
            )
