from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ols_platform.database import IndexDatabase
from database_integrity import sha256_file, snapshot_database


def verify_integrity(path: Path) -> str:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro",uri=True)
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore, migrate and rebase a validated SQLite snapshot on Dell."
    )
    parser.add_argument("--database-backup",type=Path,required=True)
    parser.add_argument("--database-path",type=Path,required=True)
    parser.add_argument("--manifest",type=Path,required=True)
    parser.add_argument("--root-config",type=Path,required=True)
    args = parser.parse_args()

    backup = args.database_backup.resolve()
    target = args.database_path.resolve()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    roots_by_name = json.loads(args.root_config.read_text(encoding="utf-8"))
    expected = manifest["database_backup"]
    if not backup.is_file() or backup.stat().st_size != expected["size_bytes"]:
        raise RuntimeError("Database backup is missing or has an unexpected size.")
    if sha256_file(backup) != expected["sha256"]:
        raise RuntimeError("Database backup SHA-256 does not match its transfer manifest.")
    if set(roots_by_name) != {
        "Database 1","Database 2","Database 3","Database 4","default"
    }:
        raise ValueError("Local root configuration must name all five registered roots.")
    resolved_roots = {}
    for name,value in roots_by_name.items():
        path = Path(value).resolve()
        if not path.is_dir():
            raise OSError(f"Local OLS directory is not available: {path}")
        with os.scandir(path):
            pass
        resolved_roots[name] = str(path)
    if len({path.casefold() for path in resolved_roots.values()}) != 5:
        raise ValueError("Each source slot must use a distinct local directory.")
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite an existing index: {target}")
    target.parent.mkdir(parents=True,exist_ok=True)

    source = sqlite3.connect(backup.as_uri() + "?mode=ro",uri=True)
    try:
        source_snapshot = snapshot_database(source)
        if source_snapshot != expected["snapshot"]:
            raise RuntimeError("Database backup data differs from its manifest snapshot.")
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database backup failed SQLite integrity_check.")
        destination = sqlite3.connect(target,timeout=60)
        try:
            source.backup(destination,pages=2048,sleep=0.05)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Restored Dell database failed SQLite integrity_check.")
        finally:
            destination.close()
    finally:
        source.close()

    before_migration_connection = sqlite3.connect(
        target.as_uri() + "?mode=ro",uri=True
    )
    try:
        before_migration = snapshot_database(before_migration_connection)
    finally:
        before_migration_connection.close()
    if before_migration != expected["snapshot"]:
        raise RuntimeError("Restored database differs from its source backup.")

    database = IndexDatabase(target)
    try:
        slots = database.list_source_slots()
        if len(slots) != 5 or any(slot["source_id"] is None for slot in slots):
            raise RuntimeError("The restored database does not have five configured source slots.")
        proposed = {}
        for slot in slots:
            proposed[int(slot["source_id"])] = {
                "old_path":slot["path"],
                "new_path":resolved_roots[slot["name"]],
            }
        database.relocate_source_roots(proposed)
    finally:
        database.close()

    restored_connection = sqlite3.connect(target.as_uri() + "?mode=ro",uri=True)
    restored_connection.row_factory = sqlite3.Row
    try:
        after = snapshot_database(restored_connection)
        integrity = restored_connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        restored_connection.close()
    if integrity != "ok":
        raise RuntimeError(f"Final Dell database integrity check failed: {integrity}")
    if after["table_counts"] != expected["snapshot"]["table_counts"]:
        raise RuntimeError("Project, reference, analysis, hash or queue record counts changed.")
    if after["reference_check_statuses"] != expected["snapshot"]["reference_check_statuses"]:
        raise RuntimeError("Reference validation statuses changed during deployment.")
    if after["record_digests"] != expected["snapshot"]["record_digests"]:
        raise RuntimeError("Protected project, hash, reference or audit record hashes changed.")
    if after["schema_version"] < expected["snapshot"]["schema_version"]:
        raise RuntimeError("Restored database schema unexpectedly moved backwards.")
    expected_roots = {
        name:resolved_roots[name]
        for name in ("Database 1","Database 2","Database 3","Database 4","default")
    }
    actual_roots = {source["name"]:source["path"] for source in after["sources"]}
    if actual_roots != expected_roots:
        raise RuntimeError("One or more database source roots were not rebased locally.")
    print(json.dumps({
        "status":"restored_and_verified",
        "database_path":str(target),
        "sqlite_integrity":integrity,
        "source_backup_sha256":expected["sha256"],
        "before":before_migration,
        "after":after,
        "source_paths_rebased":actual_roots,
        "hash_attempt_history_preserved":after["table_counts"]["hash_attempts"],
    },indent=2,ensure_ascii=False,sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
