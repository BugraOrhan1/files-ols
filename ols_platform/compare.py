from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class ChangedRange:
    start: int
    end_exclusive: int


@dataclass(frozen=True)
class BinaryComparison:
    original_path: str
    tuned_path: str
    original_sha256: str
    tuned_sha256: str
    original_size: int
    tuned_size: int
    differing_bytes: int
    changed_ranges: tuple[ChangedRange, ...]
    exact_match: bool


def compare_binary_files(
    original: Path,
    tuned: Path,
    *,
    chunk_size: int = 1024 * 1024,
    max_ranges: int = 100_000,
) -> BinaryComparison:
    """Read-only byte comparison using bounded chunks and absolute offsets."""
    if chunk_size <= 0 or max_ranges <= 0:
        raise ValueError("chunk_size and max_ranges must be positive.")
    original_before = original.stat()
    tuned_before = tuned.stat()
    original_digest = hashlib.sha256()
    tuned_digest = hashlib.sha256()
    changed: list[ChangedRange] = []
    differing_bytes = 0
    in_range = False
    range_start = 0
    original_size = 0
    tuned_size = 0

    with original.open("rb") as old_file, tuned.open("rb") as new_file:
        offset = 0
        while True:
            old_chunk = _read_chunk(old_file, chunk_size)
            new_chunk = _read_chunk(new_file, chunk_size)
            if not old_chunk and not new_chunk:
                break
            original_digest.update(old_chunk)
            tuned_digest.update(new_chunk)
            original_size += len(old_chunk)
            tuned_size += len(new_chunk)
            shared_size = min(len(old_chunk), len(new_chunk))
            for index in range(shared_size):
                absolute = offset + index
                if old_chunk[index] != new_chunk[index]:
                    differing_bytes += 1
                    if not in_range:
                        if len(changed) >= max_ranges:
                            raise RuntimeError(
                                f"Comparison exceeds the configured {max_ranges} "
                                "changed-range limit."
                            )
                        range_start = absolute
                        in_range = True
                elif in_range:
                    changed.append(ChangedRange(range_start, absolute))
                    in_range = False
            if len(old_chunk) != len(new_chunk):
                tail_size = abs(len(old_chunk) - len(new_chunk))
                differing_bytes += tail_size
                if not in_range:
                    if len(changed) >= max_ranges:
                        raise RuntimeError(
                            f"Comparison exceeds the configured {max_ranges} "
                            "changed-range limit."
                        )
                    range_start = offset + shared_size
                    in_range = True
            offset += max(len(old_chunk), len(new_chunk))

    if in_range:
        changed.append(ChangedRange(range_start, max(original_size, tuned_size)))
    original_after = original.stat()
    tuned_after = tuned.stat()
    if (
        original_size != original_before.st_size
        or tuned_size != tuned_before.st_size
        or original_after.st_size != original_before.st_size
        or tuned_after.st_size != tuned_before.st_size
        or original_after.st_mtime_ns != original_before.st_mtime_ns
        or tuned_after.st_mtime_ns != tuned_before.st_mtime_ns
    ):
        raise RuntimeError("A binary file changed during comparison.")
    original_hash = original_digest.hexdigest()
    tuned_hash = tuned_digest.hexdigest()

    return BinaryComparison(
        original_path=str(original.resolve()),
        tuned_path=str(tuned.resolve()),
        original_sha256=original_hash,
        tuned_sha256=tuned_hash,
        original_size=original_size,
        tuned_size=tuned_size,
        differing_bytes=differing_bytes,
        changed_ranges=tuple(changed),
        exact_match=original_hash == tuned_hash and original_size == tuned_size,
    )


def store_comparison(database_path: Path, result: BinaryComparison) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO binary_comparisons(
                original_path, tuned_path, original_sha256, tuned_sha256,
                original_size, tuned_size, differing_bytes, changed_ranges_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(original_sha256, tuned_sha256) DO UPDATE SET
                original_path=excluded.original_path,
                tuned_path=excluded.tuned_path,
                original_size=excluded.original_size,
                tuned_size=excluded.tuned_size,
                differing_bytes=excluded.differing_bytes,
                changed_ranges_json=excluded.changed_ranges_json,
                created_at=CURRENT_TIMESTAMP
            """,
            (
                result.original_path,
                result.tuned_path,
                result.original_sha256,
                result.tuned_sha256,
                result.original_size,
                result.tuned_size,
                result.differing_bytes,
                json.dumps([asdict(item) for item in result.changed_ranges]),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _read_chunk(file: BinaryIO, chunk_size: int) -> bytes:
    parts = []
    remaining = chunk_size
    while remaining:
        chunk = file.read(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)
