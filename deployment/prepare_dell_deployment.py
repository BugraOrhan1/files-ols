from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import zipfile

from database_integrity import sha256_file, snapshot_database


def _add_tree(archive: zipfile.ZipFile, root: Path, path: Path) -> None:
    for item in sorted(path.rglob("*")):
        if not item.is_file() or "__pycache__" in item.parts or item.suffix == ".pyc":
            continue
        archive.write(item, Path("OlsHashWorker") / item.relative_to(root))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a source-only Dell deployment bundle and consistent SQLite backup."
    )
    parser.add_argument(
        "--database", type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "ols-index.sqlite3",
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "dell-deployment",
    )
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    source_db = args.database.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    backup_path = output / "ols-index.sqlite3.backup"
    manifest_path = output / "database-manifest.json"
    archive_path = output / "ols-hash-worker.zip"
    for target in (backup_path, manifest_path, archive_path):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite deployment artifact: {target}")
    if not source_db.is_file():
        raise FileNotFoundError(f"Source SQLite database not found: {source_db}")

    source = sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Source database integrity check failed: {integrity}")
        snapshot = snapshot_database(source)
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination, pages=2048, sleep=0.05)
            backup_integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
            if backup_integrity != "ok":
                raise RuntimeError(f"Backup integrity check failed: {backup_integrity}")
            backup_snapshot = snapshot_database(destination)
        finally:
            destination.close()
    finally:
        source.close()
    if snapshot != backup_snapshot:
        raise RuntimeError("SQLite backup verification differs from the source snapshot.")

    with zipfile.ZipFile(
        archive_path,mode="w",compression=zipfile.ZIP_DEFLATED,compresslevel=6
    ) as archive:
        _add_tree(archive,workspace,workspace / "ols_platform")
        _add_tree(archive,workspace,workspace / "tests")
        _add_tree(archive,workspace,workspace / "deployment")
        archive.write(workspace / "README.md","OlsHashWorker/README.md")

    manifest = {
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database_backup": {
            "filename": backup_path.name,
            "size_bytes": backup_path.stat().st_size,
            "sha256": sha256_file(backup_path),
            "snapshot": snapshot,
        },
        "application_bundle": {
            "filename": archive_path.name,
            "size_bytes": archive_path.stat().st_size,
            "sha256": sha256_file(archive_path),
        },
        "notes": [
            "The database backup is independent of the source database and must be copied to Dell.",
            "The worker uses only Python standard-library modules; py-ols remains a separate optional parser dependency.",
            "The UNC paths are metadata in this snapshot. The Dell installer verifies and rebases them to local share paths.",
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest,indent=2,ensure_ascii=False,sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status":"ready_for_manual_transfer",
        "database_backup":str(backup_path),
        "database_backup_bytes":backup_path.stat().st_size,
        "database_backup_sha256":manifest["database_backup"]["sha256"],
        "application_bundle":str(archive_path),
        "application_bundle_bytes":archive_path.stat().st_size,
        "database_manifest":str(manifest_path),
        "snapshot":snapshot,
        "remote_deployment_performed":False,
    },indent=2,ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
