from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3


TABLES = (
    "projects",
    "project_versions",
    "project_version_slots",
    "map_definitions",
    "reference_artifacts",
    "reference_checks",
    "reference_check_history",
    "binary_analysis_runs",
    "hash_jobs",
    "hash_attempts",
)

DIGEST_QUERIES = {
    "projects":"SELECT * FROM projects ORDER BY id",
    "project_versions":"SELECT * FROM project_versions ORDER BY id",
    "project_version_slots":"SELECT * FROM project_version_slots ORDER BY id",
    "map_definitions":"SELECT * FROM map_definitions ORDER BY id",
    "reference_artifacts":"SELECT * FROM reference_artifacts ORDER BY id",
    "reference_checks":"SELECT * FROM reference_checks ORDER BY id",
    "reference_check_history":"SELECT * FROM reference_check_history ORDER BY id",
    "binary_analysis_runs":"SELECT * FROM binary_analysis_runs ORDER BY id",
    "hash_jobs":"""
        SELECT id,source_id,filename,size_bytes,modified_ns,status,attempts,
               bytes_hashed,sha256,error,run_id,started_at,finished_at
        FROM hash_jobs ORDER BY id
    """,
    "hash_attempts":"""
        SELECT id,job_id,run_id,source_id,filename,size_bytes,modified_ns,
               sha256,started_at,finished_at,bytes_read,status,error
        FROM hash_attempts ORDER BY id
    """,
    "file_locations":"""
        SELECT id,source_id,file_kind,size_bytes,modified_ns,filename,extension,
               sha256,content_id,state,is_present,last_seen_scan_id,last_seen_at
        FROM file_locations ORDER BY id
    """,
    "source_files":"""
        SELECT id,sha256,size_bytes,modified_ns,file_kind,created_at,updated_at
        FROM source_files ORDER BY id
    """,
}


def digest_query(connection: sqlite3.Connection, query: str) -> str:
    digest = hashlib.sha256()
    for row in connection.execute(query):
        digest.update(json.dumps(
            list(row),ensure_ascii=False,separators=(",",":"),default=str
        ).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def snapshot_database(connection: sqlite3.Connection) -> dict:
    counts = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in TABLES
    }
    counts["hashed_locations"] = int(connection.execute(
        "SELECT COUNT(*) FROM file_locations WHERE file_kind='ols' AND sha256 IS NOT NULL"
    ).fetchone()[0])
    counts["pending_hash_locations"] = int(connection.execute(
        "SELECT COUNT(*) FROM file_locations WHERE file_kind='ols' AND is_present=1 AND sha256 IS NULL"
    ).fetchone()[0])
    statuses = {
        row[0]: int(row[1])
        for row in connection.execute(
            "SELECT status,COUNT(*) FROM reference_checks GROUP BY status"
        )
    }
    sources = [
        {"id":int(row[0]),"name":row[1],"path":row[2]}
        for row in connection.execute(
            """
            SELECT s.id,r.name,s.path
            FROM source_root_slots r
            JOIN registered_sources s ON s.id=r.registered_source_id
            ORDER BY r.id
            """
        )
    ]
    return {
        "schema_version": int(connection.execute(
            "SELECT version FROM schema_info"
        ).fetchone()[0]),
        "table_counts": counts,
        "reference_check_statuses": statuses,
        "sources": sources,
        "record_digests": {
            name:digest_query(connection,query)
            for name,query in DIGEST_QUERIES.items()
        },
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
