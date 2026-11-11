from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


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
    original_size = original.stat().st_size
    tuned_size = tuned.stat().st_size
    original_hash = _hash_file(original, chunk_size)
    tuned_hash = _hash_file(tuned, chunk_size)
    changed: list[ChangedRange] = []
    differing_bytes = 0
    in_range = False
    range_start = 0
    common_size = min(original_size, tuned_size)

    with original.open("rb") as old_file, tuned.open("rb") as new_file:
        offset = 0
        while offset < common_size:
            count = min(chunk_size, common_size - offset)
            old_chunk = old_file.read(count)
            new_chunk = new_file.read(count)
            for index, (old_byte, new_byte) in enumerate(zip(old_chunk, new_chunk)):
                absolute = offset + index
                if old_byte != new_byte:
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
            offset += count

    if original_size != tuned_size:
        differing_bytes += abs(original_size - tuned_size)
        if not in_range:
            range_start = common_size
            in_range = True
    if in_range:
        changed.append(ChangedRange(range_start, max(original_size, tuned_size)))

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


def _hash_file(path: Path, chunk_size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
