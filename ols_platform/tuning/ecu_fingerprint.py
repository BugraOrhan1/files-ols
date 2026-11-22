"""ECU identity and binary fingerprints with explicit provenance.

Everything here is *measurement*, never a verdict:

* :func:`extract_identifiers` finds identification strings in the bytes with
  a small set of documented, versioned rules. Every hit keeps the raw bytes,
  the offset and the rule that produced it. Rule hits are marked
  ``pattern_unvalidated``: a pattern match says "this string is in the file",
  not "this is the ECU's software number".
* :func:`normalize_id` only removes separators (space . - _ /) and upper-cases;
  letters and digits - including revision suffixes - are never dropped.
  :func:`compare_ids` reports ``exact``, ``same_base_other_revision`` (VAG
  style part numbers whose 9-character base is equal) or ``different``.
* :func:`block_digests` / :func:`compare_blocks` compare two images in fixed
  blocks: hash-identical at the same offset, hash-identical at another offset,
  different, uninformative (uniform fill) and missing (size difference).
  A partial block match is never reported as a full binary match.
* :class:`FingerprintIndex` streams files of a large corpus into a SQLite
  index (bounded memory, resumable, checkpointed) and re-verifies the stored
  SHA-256 before a fingerprint is trusted again.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

ALGORITHM_VERSION = "ecu-fp/1"
BLOCK_SIZE = 4096
_READ_CHUNK = 1024 * 1024
_SEPARATORS = re.compile(r"[\s.\-_/]+")

# (rule id, field, manufacturer hint, compiled pattern). Patterns are matched
# on raw bytes. Kept deliberately small: only widely documented formats.
RULES: list[tuple[str, str, str, re.Pattern]] = [
    ("bosch_sw_1037", "sw_number", "Bosch", re.compile(rb"(?<![0-9])1037\d{6}(?![0-9])")),
    ("bosch_hw_0281", "hw_number", "Bosch", re.compile(rb"(?<![0-9])0281\d{6}(?![0-9])")),
    ("bosch_hw_0261", "hw_number", "Bosch", re.compile(rb"(?<![0-9])0261\d{6}(?![0-9])")),
    ("vag_part_number", "part_number", "VAG",
     re.compile(rb"(?<![A-Z0-9])\d[A-Z0-9]{2} ?9\d{2} ?\d{3} ?[A-Z]{0,3}(?![A-Za-z0-9])")),
    ("ecu_family_name", "ecu_type", "",
     re.compile(rb"(?<![A-Za-z0-9])(?:EDC1[567][A-Z]{0,2}\d{0,2}|MEDG?17(?:\.\d{1,2}){0,3}|MEVD17(?:\.\d{1,2}){0,3}"
                rb"|MED9(?:\.\d{1,2}){0,2}|ME7(?:\.\d{1,2}){0,2}|MSD8\d|MSV\d\d|SIMOS ?\d{1,2}(?:\.\d{1,2})?"
                rb"|MD1C[A-Z]\d{3}|MG1C[A-Z]\d{3}|PCR2\.1|DCM\d\.\d[A-Z]?|SID\d{3})(?![A-Za-z0-9])")),
]
MAX_HITS_PER_RULE = 16


@dataclass(frozen=True)
class Identifier:
    field: str
    raw: str            # exactly as in the file (latin-1 decoded)
    normalized: str     # search form only
    offset: int
    rule: str
    manufacturer: str
    status: str = "pattern_unvalidated"
    algorithm: str = ALGORITHM_VERSION


def normalize_id(raw: str) -> str:
    """Search form: separators removed, upper case. Letters/digits are kept."""

    return _SEPARATORS.sub("", raw).upper()


def vag_base(normalized: str) -> str | None:
    """9-character base of a VAG part number (e.g. 8P0907115 of 8P0907115A)."""

    if re.fullmatch(r"\d[A-Z0-9]{2}9\d{2}\d{3}[A-Z]{0,3}", normalized):
        return normalized[:9]
    return None


def compare_ids(a: str, b: str) -> str:
    """``exact`` | ``same_base_other_revision`` | ``different`` (on the normalized form)."""

    na, nb = normalize_id(a), normalize_id(b)
    if na == nb:
        return "exact"
    ba, bb = vag_base(na), vag_base(nb)
    if ba is not None and ba == bb:
        return "same_base_other_revision"
    return "different"


def extract_identifiers(data: bytes) -> list[Identifier]:
    """Identification strings found in ``data`` (deduplicated per rule and raw spelling)."""

    out: list[Identifier] = []
    for rule, field_name, maker, pattern in RULES:
        seen: set[str] = set()
        for match in pattern.finditer(data):
            raw = match.group(0).decode("latin-1")
            norm = normalize_id(raw)
            if raw in seen:
                continue  # each spelling is kept: "03L906018JL" and "03L 906 018 JL" are both reported
            seen.add(raw)
            out.append(Identifier(field_name, raw, norm, match.start(), rule, maker))
            if len(seen) >= MAX_HITS_PER_RULE:
                break
    return out


def identifiers_by_field(ids: Iterable[Identifier]) -> dict[str, list[Identifier]]:
    out: dict[str, list[Identifier]] = {}
    for item in ids:
        out.setdefault(item.field, []).append(item)
    return out


def identity_summary(data: bytes) -> dict[str, Any]:
    ids = extract_identifiers(data)
    return {
        "algorithm": ALGORITHM_VERSION,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "identifiers": [asdict(i) for i in ids],
    }


# --------------------------------------------------------------------------- blocks

def _uniform(block: bytes) -> bool:
    return not block or block.count(block[:1]) == len(block)


def block_digests(data: bytes, block_size: int = BLOCK_SIZE) -> list[bytes | None]:
    """16-byte BLAKE2b digest per block; ``None`` for uniform (fill) blocks."""

    out: list[bytes | None] = []
    for start in range(0, len(data), block_size):
        block = data[start:start + block_size]
        out.append(None if _uniform(block) else hashlib.blake2b(block, digest_size=16).digest())
    return out


def _ranges(indices: list[int], block_size: int, limit: int = 40) -> list[list[int]]:
    ranges: list[list[int]] = []
    for i in indices:
        start = i * block_size
        if ranges and ranges[-1][1] == start:
            ranges[-1][1] = start + block_size
        else:
            ranges.append([start, start + block_size])
    return ranges[:limit]


def compare_blocks(a: list[bytes | None], b: list[bytes | None], block_size: int = BLOCK_SIZE) -> dict[str, Any]:
    """Block comparison of input ``a`` against reference ``b``.

    ``identical_same_offset`` and ``identical_moved`` are hash-identical
    blocks (proven equal content); ``different`` blocks exist in both at the
    offset but differ; ``uninformative`` blocks are uniform fill in ``a``;
    ``missing_in_reference`` are blocks beyond the reference's size.
    """

    where: dict[bytes, int] = {}
    for index, digest in enumerate(b):
        if digest is not None:
            where.setdefault(digest, index)
    same, moved, different, uninformative, missing = [], [], [], [], []
    for index, digest in enumerate(a):
        if digest is None:
            uninformative.append(index)
        elif index < len(b) and b[index] == digest:
            same.append(index)
        elif digest in where:
            moved.append(index)
        elif index < len(b):
            different.append(index)
        else:
            missing.append(index)
    informative = len(a) - len(uninformative)
    proven = len(same) + len(moved)
    return {
        "block_size": block_size,
        "blocks": len(a),
        "informative_blocks": informative,
        "identical_same_offset": len(same),
        "identical_moved": len(moved),
        "different": len(different),
        "uninformative": len(uninformative),
        "missing_in_reference": len(missing),
        "proven_identical_share": proven / informative if informative else 0.0,
        "full_binary_match": proven == informative and not different and not missing and len(a) == len(b),
        "different_ranges": _ranges(different, block_size),
        "missing_ranges": _ranges(missing, block_size),
    }


# --------------------------------------------------------------------------- corpus index

INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS fp_files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    identifiers TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fp_blocks (
    sha256 TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    digest BLOB NOT NULL,
    PRIMARY KEY (sha256, block_index)
);
CREATE INDEX IF NOT EXISTS fp_blocks_digest ON fp_blocks(digest);
CREATE TABLE IF NOT EXISTS fp_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def stream_file(path: Path, block_size: int = BLOCK_SIZE) -> tuple[str, int, list[bytes | None], list[Identifier]]:
    """SHA-256, size, block digests and identifiers of a file, read in chunks.

    Identifier rules run per chunk with an overlap so strings on a chunk
    border are found; memory stays bounded by the chunk size."""

    sha = hashlib.sha256()
    digests: list[bytes | None] = []
    ids: dict[tuple[str, str], Identifier] = {}
    overlap = 64
    carry = b""
    carry_offset = 0
    pending = b""
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                break
            sha.update(chunk)
            size += len(chunk)
            pending += chunk
            whole = len(pending) - len(pending) % block_size
            digests.extend(block_digests(pending[:whole], block_size))
            pending = pending[whole:]
            window = carry + chunk
            for item in extract_identifiers(window):
                key = (item.rule, item.raw)
                if key not in ids:
                    ids[key] = Identifier(item.field, item.raw, item.normalized, carry_offset + item.offset,
                                          item.rule, item.manufacturer)
            carry_offset += len(window) - min(overlap, len(window))
            carry = window[-overlap:]
    if pending:
        digests.extend(block_digests(pending, block_size))
    return sha.hexdigest(), size, digests, sorted(ids.values(), key=lambda i: (i.offset, i.rule))


class FingerprintIndex:
    """Resumable fingerprint index for a large corpus (one SQLite file)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(INDEX_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def is_current(self, path: Path) -> bool:
        stat = path.stat()
        row = self.db.execute("SELECT size,mtime_ns,algorithm FROM fp_files WHERE path=?", (str(path),)).fetchone()
        return row is not None and row[0] == stat.st_size and row[1] == stat.st_mtime_ns and row[2] == ALGORITHM_VERSION

    def add(self, path: Path) -> str:
        import json

        stat = path.stat()
        sha, size, digests, ids = stream_file(path)
        self.db.execute(
            "INSERT OR REPLACE INTO fp_files(path,size,mtime_ns,sha256,algorithm,identifiers,indexed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(path), size, stat.st_mtime_ns, sha, ALGORITHM_VERSION,
             json.dumps([asdict(i) for i in ids]), time.strftime("%Y-%m-%dT%H:%M:%S")))
        if self.db.execute("SELECT 1 FROM fp_blocks WHERE sha256=? LIMIT 1", (sha,)).fetchone() is None:
            self.db.executemany("INSERT OR IGNORE INTO fp_blocks(sha256,block_index,digest) VALUES(?,?,?)",
                                [(sha, i, d) for i, d in enumerate(digests) if d is not None])
        return sha

    def index_tree(self, root: Path, *, patterns: tuple[str, ...] = (".bin", ".ori", ".mod", ".dtf", ".hex"),
                   checkpoint_every: int = 50, stop: Callable[[], bool] | None = None,
                   progress: Callable[[str], None] | None = None) -> dict[str, int]:
        """Index every matching file below ``root``; unchanged files are skipped,
        a commit is made every ``checkpoint_every`` files so an interrupted run
        resumes where it stopped."""

        stats = {"seen": 0, "indexed": 0, "skipped_unchanged": 0, "errors": 0}
        for path in _walk(root, patterns):
            if stop is not None and stop():
                break
            stats["seen"] += 1
            try:
                if self.is_current(path):
                    stats["skipped_unchanged"] += 1
                    continue
                self.add(path)
                stats["indexed"] += 1
            except OSError as exc:
                stats["errors"] += 1
                if progress:
                    progress(f"{path}: {exc}")
            if (stats["indexed"] + stats["errors"]) % checkpoint_every == 0:
                self.db.commit()
        self.db.commit()
        return stats

    def verify(self, path: Path) -> dict[str, Any]:
        """Re-hash ``path`` and compare with the stored fingerprint."""

        row = self.db.execute("SELECT sha256,algorithm FROM fp_files WHERE path=?", (str(path),)).fetchone()
        if row is None:
            return {"status": "not_indexed"}
        if not path.exists():
            return {"status": "source_missing", "stored_sha256": row[0]}
        sha = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
                sha.update(chunk)
        current = sha.hexdigest()
        if row[1] != ALGORITHM_VERSION:
            return {"status": "algorithm_outdated", "stored_sha256": row[0], "current_sha256": current}
        return {"status": "current" if current == row[0] else "stale", "stored_sha256": row[0],
                "current_sha256": current}

    def candidates_by_blocks(self, digests: list[bytes | None], limit: int = 20,
                             sample: int = 256) -> list[tuple[str, int]]:
        """References sharing the most sampled informative blocks with ``digests``."""

        informative = [d for d in digests if d is not None]
        if not informative:
            return []
        step = max(1, len(informative) // sample)
        counts: dict[str, int] = {}
        for digest in informative[::step]:
            for (sha,) in self.db.execute("SELECT DISTINCT sha256 FROM fp_blocks WHERE digest=?", (digest,)):
                counts[sha] = counts.get(sha, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def _walk(root: Path, patterns: tuple[str, ...]) -> Iterator[Path]:
    for folder, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.lower().endswith(patterns):
                yield Path(folder) / name
