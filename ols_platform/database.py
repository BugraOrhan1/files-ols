from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from . import __version__ as APPLICATION_VERSION
from .reader import ProjectSnapshot, ReadIssue


SCHEMA_VERSION = 16


def _is_process_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        # On Windows, os.kill(pid, 0) can terminate the target process.
        SYNCHRONIZE = 0x00100000
        ERROR_ACCESS_DENIED = 5
        ERROR_INVALID_PARAMETER = 87
        WAIT_OBJECT_0 = 0
        WAIT_TIMEOUT = 258
        WAIT_FAILED = 0xFFFFFFFF

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        )
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = open_process(SYNCHRONIZE, False, process_id)
        if not handle:
            error = ctypes.get_last_error()
            if error == ERROR_ACCESS_DENIED:
                return True
            if error == ERROR_INVALID_PARAMETER:
                return False
            raise ctypes.WinError(error)
        try:
            result = wait_for_single_object(handle, 0)
            if result == WAIT_OBJECT_0:
                return False
            if result == WAIT_TIMEOUT:
                return True
            if result == WAIT_FAILED:
                raise ctypes.WinError(ctypes.get_last_error())
            raise OSError(f"Unexpected process wait result: {result}")
        finally:
            if not close_handle(handle):
                raise ctypes.WinError(ctypes.get_last_error())

    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class IndexDatabase:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        if read_only:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True, timeout=30)
            self.connection.execute("PRAGMA query_only = ON")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 30000")
        if not read_only:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self._initialize()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_info (
                version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                file_kind TEXT NOT NULL DEFAULT 'ols',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_source_hash ON source_files(sha256);
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL UNIQUE REFERENCES source_files(id)
                    ON DELETE CASCADE,
                parser_name TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                format_version INTEGER NOT NULL,
                is_complete INTEGER NOT NULL,
                raw_counts_json TEXT NOT NULL,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS project_metadata (
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                field TEXT NOT NULL,
                value TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (project_id, field)
            );
            CREATE INDEX IF NOT EXISTS idx_project_metadata_field_value
                ON project_metadata(field, value);
            CREATE TABLE IF NOT EXISTS project_versions (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                slot_index INTEGER NOT NULL,
                declared_slot_count INTEGER NOT NULL,
                blob_offset INTEGER NOT NULL,
                blob_size INTEGER NOT NULL,
                epk TEXT NOT NULL,
                epk_offset INTEGER NOT NULL,
                status TEXT NOT NULL,
                parent_id INTEGER,
                is_root INTEGER NOT NULL DEFAULT 0,
                UNIQUE (project_id, slot_index)
            );
            CREATE TABLE IF NOT EXISTS project_version_slots (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                version_id INTEGER REFERENCES project_versions(id) ON DELETE SET NULL,
                slot_index INTEGER NOT NULL,
                blob_offset INTEGER NOT NULL,
                blob_size INTEGER NOT NULL,
                marker_hex TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload_size INTEGER NOT NULL,
                binary_status TEXT NOT NULL,
                name_status TEXT NOT NULL,
                UNIQUE (project_id, slot_index)
            );
            CREATE INDEX IF NOT EXISTS idx_version_slots_payload_project
                ON project_version_slots(payload_sha256, project_id);
            CREATE TABLE IF NOT EXISTS project_folders (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                folder_index INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                UNIQUE (project_id, folder_index)
            );
            CREATE TABLE IF NOT EXISTS cal_blocks (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                offset INTEGER NOT NULL,
                cas_id TEXT NOT NULL,
                epk TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS map_definitions (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                map_type TEXT NOT NULL,
                data_type TEXT NOT NULL,
                columns_count INTEGER NOT NULL,
                rows_count INTEGER NOT NULL,
                data_offset INTEGER NOT NULL,
                unit TEXT NOT NULL,
                factor REAL NOT NULL,
                conversion_offset REAL NOT NULL,
                folder_id INTEGER NOT NULL DEFAULT 0,
                type_code INTEGER NOT NULL DEFAULT 0,
                version_relation_status TEXT NOT NULL DEFAULT 'unknown_not_exposed_by_parser',
                status TEXT NOT NULL,
                UNIQUE (project_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS map_axes (
                id INTEGER PRIMARY KEY,
                map_id INTEGER NOT NULL REFERENCES map_definitions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                points INTEGER NOT NULL,
                address INTEGER NOT NULL,
                data_type TEXT NOT NULL,
                unit TEXT NOT NULL,
                factor REAL NOT NULL,
                conversion_offset REAL NOT NULL,
                identifier TEXT NOT NULL,
                description TEXT NOT NULL,
                data_source INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                UNIQUE (map_id, role)
            );
            CREATE INDEX IF NOT EXISTS idx_map_dimensions
                ON map_definitions(columns_count, rows_count, project_id);
            CREATE INDEX IF NOT EXISTS idx_project_versions_name
                ON project_versions(name, project_id);
            CREATE TABLE IF NOT EXISTS project_skipped_elements (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                element_kind TEXT NOT NULL,
                description TEXT NOT NULL,
                raw_metadata_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                UNIQUE (project_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS project_diagnostics (
                project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                application_version TEXT NOT NULL,
                data_structure_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                processing_status TEXT NOT NULL,
                reference_status TEXT NOT NULL,
                reference_details TEXT NOT NULL,
                errors_json TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                error_count INTEGER NOT NULL DEFAULT 0,
                warning_count INTEGER NOT NULL DEFAULT 0,
                generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS import_jobs (
                id INTEGER PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_sha256 TEXT,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_import_jobs_path_status
                ON import_jobs(source_path, status);
            CREATE TABLE IF NOT EXISTS import_errors (
                id INTEGER PRIMARY KEY,
                job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
                code TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS binary_comparisons (
                id INTEGER PRIMARY KEY,
                original_path TEXT NOT NULL,
                tuned_path TEXT NOT NULL,
                original_sha256 TEXT NOT NULL,
                tuned_sha256 TEXT NOT NULL,
                original_size INTEGER NOT NULL,
                tuned_size INTEGER NOT NULL,
                differing_bytes INTEGER NOT NULL,
                changed_ranges_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (original_sha256, tuned_sha256)
            );
            CREATE TABLE IF NOT EXISTS reference_artifacts (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                manifest_path TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                artifact_index INTEGER NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                version_name TEXT NOT NULL DEFAULT '',
                source_slot_index INTEGER,
                relation_status TEXT NOT NULL,
                representation TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, manifest_sha256, artifact_index)
            );
            CREATE INDEX IF NOT EXISTS idx_reference_artifacts_project
                ON reference_artifacts(project_id, kind);
            CREATE TABLE IF NOT EXISTS reference_checks (
                id INTEGER PRIMARY KEY,
                artifact_id INTEGER NOT NULL REFERENCES reference_artifacts(id)
                    ON DELETE CASCADE,
                source_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                comparison_json TEXT NOT NULL,
                checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(artifact_id, source_sha256)
            );
            CREATE TABLE IF NOT EXISTS reference_check_history (
                id INTEGER PRIMARY KEY,
                origin_check_id INTEGER UNIQUE,
                artifact_id INTEGER NOT NULL REFERENCES reference_artifacts(id)
                    ON DELETE CASCADE,
                artifact_kind TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                comparison_json TEXT NOT NULL,
                checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_reference_check_history_artifact
                ON reference_check_history(artifact_id, id);
            CREATE TABLE IF NOT EXISTS binary_analysis_runs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                source_sha256 TEXT NOT NULL,
                algorithm_version TEXT NOT NULL,
                options_json TEXT NOT NULL,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_binary_analysis_runs_project
                ON binary_analysis_runs(project_id, id);
            CREATE TABLE IF NOT EXISTS binary_slot_candidates (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES binary_analysis_runs(id)
                    ON DELETE CASCADE,
                artifact_id INTEGER NOT NULL REFERENCES reference_artifacts(id)
                    ON DELETE CASCADE,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                slot_index INTEGER NOT NULL,
                version_id INTEGER REFERENCES project_versions(id) ON DELETE SET NULL,
                export_sha256 TEXT NOT NULL,
                slot_sha256 TEXT NOT NULL,
                binary_status TEXT NOT NULL,
                version_relation_status TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_binary_slot_candidates_project
                ON binary_slot_candidates(project_id, run_id, binary_status);
            CREATE TABLE IF NOT EXISTS version_match_reviews (
                id INTEGER PRIMARY KEY,
                candidate_id INTEGER NOT NULL REFERENCES binary_slot_candidates(id)
                    ON DELETE CASCADE,
                decision TEXT NOT NULL CHECK(
                    decision IN ('confirm_version', 'reject', 'defer')
                ),
                evidence TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_version_match_reviews_candidate
                ON version_match_reviews(candidate_id, id);
            CREATE TABLE IF NOT EXISTS reference_package_runs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                source_sha256 TEXT NOT NULL,
                manifest_path TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                run_type TEXT NOT NULL,
                status TEXT NOT NULL,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_reference_package_runs_project
                ON reference_package_runs(project_id, run_type, id);
            CREATE TABLE IF NOT EXISTS export_tasks (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                source_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                reason TEXT NOT NULL,
                version_names_json TEXT NOT NULL,
                export_types_json TEXT NOT NULL,
                uncertainty TEXT NOT NULL,
                expected_outcome TEXT NOT NULL,
                priority TEXT NOT NULL CHECK(
                    priority IN (
                        'NO_EXPORT_REQUIRED','LOW_PRIORITY','NORMAL_PRIORITY',
                        'HIGH_PRIORITY','BLOCKING'
                    )
                ),
                status TEXT NOT NULL DEFAULT 'pending' CHECK(
                    status IN ('pending','in_progress','completed','blocked','cancelled')
                ),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_result TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, source_sha256, reason, export_types_json)
            );
            CREATE INDEX IF NOT EXISTS idx_export_tasks_status_priority
                ON export_tasks(status, priority, id);
            CREATE TABLE IF NOT EXISTS export_task_history (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES export_tasks(id) ON DELETE CASCADE,
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                result TEXT NOT NULL,
                occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_export_task_history_task
                ON export_task_history(task_id, id);
            CREATE TABLE IF NOT EXISTS inventory_runs (
                id INTEGER PRIMARY KEY,
                roots_json TEXT NOT NULL,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_inventory_runs_created
                ON inventory_runs(id DESC);
            CREATE TABLE IF NOT EXISTS registered_sources (
                id INTEGER PRIMARY KEY,
                source_key TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                last_scan_at TEXT,
                last_attempt_at TEXT,
                found_files INTEGER NOT NULL DEFAULT 0,
                total_size_bytes INTEGER NOT NULL DEFAULT 0,
                indexed_files INTEGER NOT NULL DEFAULT 0,
                new_files INTEGER NOT NULL DEFAULT 0,
                changed_files INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS index_queue (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES registered_sources(id)
                    ON DELETE CASCADE,
                path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT,
                content_id INTEGER REFERENCES file_contents(id) ON DELETE SET NULL,
                message TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_id, path)
            );
            CREATE INDEX IF NOT EXISTS idx_index_queue_status
                ON index_queue(source_id, status, id);
            CREATE TABLE IF NOT EXISTS source_scan_runs (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES registered_sources(id)
                    ON DELETE CASCADE,
                status TEXT NOT NULL,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_source_scan_runs
                ON source_scan_runs(source_id, id DESC);
            CREATE TABLE IF NOT EXISTS training_candidates (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                version_id INTEGER REFERENCES project_versions(id) ON DELETE SET NULL,
                provenance TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_training_candidates_status
                ON training_candidates(validation_status, project_id);
            CREATE TABLE IF NOT EXISTS source_root_slots (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                registered_source_id INTEGER UNIQUE
                    REFERENCES registered_sources(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'unconfigured',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS file_contents (
                id INTEGER PRIMARY KEY,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                file_kind TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(sha256,size_bytes,file_kind)
            );
            CREATE TABLE IF NOT EXISTS file_processing (
                id INTEGER PRIMARY KEY,
                content_id INTEGER NOT NULL REFERENCES file_contents(id) ON DELETE CASCADE,
                parser_name TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                application_version TEXT NOT NULL,
                processing_status TEXT NOT NULL,
                project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(content_id,parser_name,parser_version,application_version)
            );
            CREATE INDEX IF NOT EXISTS idx_file_contents_digest
                ON file_contents(sha256,size_bytes,file_kind);
            CREATE INDEX IF NOT EXISTS idx_file_processing_project
                ON file_processing(project_id);
            CREATE TABLE IF NOT EXISTS file_locations (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES registered_sources(id)
                    ON DELETE CASCADE,
                path TEXT NOT NULL,
                file_kind TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                filename TEXT NOT NULL DEFAULT '',
                extension TEXT NOT NULL DEFAULT '',
                sha256 TEXT,
                content_id INTEGER REFERENCES file_contents(id) ON DELETE SET NULL,
                state TEXT NOT NULL DEFAULT 'discovered',
                last_seen_state TEXT NOT NULL DEFAULT 'known',
                is_present INTEGER NOT NULL DEFAULT 1,
                last_seen_scan_id INTEGER,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_id,path)
            );
            CREATE INDEX IF NOT EXISTS idx_file_locations_content
                ON file_locations(content_id,is_present);
            CREATE INDEX IF NOT EXISTS idx_file_locations_path
                ON file_locations(path);
            CREATE TABLE IF NOT EXISTS source_scan_directories (
                id INTEGER PRIMARY KEY,
                scan_run_id INTEGER NOT NULL REFERENCES source_scan_runs(id)
                    ON DELETE CASCADE,
                source_id INTEGER NOT NULL REFERENCES registered_sources(id)
                    ON DELETE CASCADE,
                path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                files_seen INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scan_run_id,path)
            );
            CREATE INDEX IF NOT EXISTS idx_source_scan_directories_queue
                ON source_scan_directories(scan_run_id,status,id);
            CREATE TABLE IF NOT EXISTS source_scan_errors (
                id INTEGER PRIMARY KEY,
                scan_run_id INTEGER NOT NULL REFERENCES source_scan_runs(id)
                    ON DELETE CASCADE,
                source_id INTEGER NOT NULL REFERENCES registered_sources(id)
                    ON DELETE CASCADE,
                path TEXT NOT NULL,
                scope TEXT NOT NULL,
                error_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_source_scan_errors_run
                ON source_scan_errors(scan_run_id,source_id,id);
            CREATE TABLE IF NOT EXISTS source_location_backfills (
                source_id INTEGER PRIMARY KEY REFERENCES registered_sources(id)
                    ON DELETE CASCADE,
                completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS hash_runs (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                worker_count INTEGER NOT NULL,
                chunk_size INTEGER NOT NULL,
                report_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS hash_jobs (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES registered_sources(id)
                    ON DELETE CASCADE,
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'HASH_PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                bytes_hashed INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT,
                error TEXT NOT NULL DEFAULT '',
                run_id INTEGER REFERENCES hash_runs(id) ON DELETE SET NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_id,path)
            );
            CREATE INDEX IF NOT EXISTS idx_hash_jobs_status_source
                ON hash_jobs(status,source_id,id);
            CREATE INDEX IF NOT EXISTS idx_hash_jobs_run
                ON hash_jobs(run_id,status);
            CREATE TABLE IF NOT EXISTS hash_attempts (
                id INTEGER PRIMARY KEY,
                job_id INTEGER NOT NULL REFERENCES hash_jobs(id) ON DELETE CASCADE,
                run_id INTEGER REFERENCES hash_runs(id) ON DELETE SET NULL,
                source_id INTEGER NOT NULL REFERENCES registered_sources(id)
                    ON DELETE CASCADE,
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                sha256 TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                bytes_read INTEGER NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_hash_attempts_run
                ON hash_attempts(run_id,id);
            CREATE TABLE IF NOT EXISTS hash_control (
                id INTEGER PRIMARY KEY CHECK(id=1),
                status TEXT NOT NULL DEFAULT 'idle',
                run_id INTEGER REFERENCES hash_runs(id) ON DELETE SET NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT OR IGNORE INTO hash_control(id,status) VALUES(1,'idle');
            CREATE TABLE IF NOT EXISTS hash_worker_state (
                id INTEGER PRIMARY KEY CHECK(id=1),
                status TEXT NOT NULL DEFAULT 'stopped',
                owner_token TEXT,
                process_id INTEGER,
                workers INTEGER NOT NULL DEFAULT 0,
                batch_size INTEGER NOT NULL DEFAULT 0,
                chunk_size INTEGER NOT NULL DEFAULT 0,
                batch_delay_seconds REAL NOT NULL DEFAULT 0,
                retry_limit INTEGER NOT NULL DEFAULT 0,
                retry_delay_seconds REAL NOT NULL DEFAULT 0,
                active_run_id INTEGER REFERENCES hash_runs(id) ON DELETE SET NULL,
                batches_completed INTEGER NOT NULL DEFAULT 0,
                files_completed INTEGER NOT NULL DEFAULT 0,
                bytes_read INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                heartbeat_epoch REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            );
            INSERT OR IGNORE INTO hash_worker_state(id,status) VALUES(1,'stopped');
            """
        )
        rows = self.connection.execute("SELECT version FROM schema_info").fetchall()
        if not rows:
            self.connection.execute(
                "INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,)
            )
        elif rows[0]["version"] in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15):
            self._migrate_to_current()
        elif rows[0]["version"] != SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema {rows[0]['version']} is not supported "
                f"(application schema {SCHEMA_VERSION})."
            )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_file_locations_scan
            ON file_locations(source_id,last_seen_scan_id,extension)
            """
        )
        self._ensure_source_root_slots()
        self._backfill_source_locations()
        self._archive_unrecorded_reference_checks()
        self.connection.commit()

    def _ensure_source_root_slots(self) -> None:
        roots = (
            (1, "Database 1"),
            (2, "Database 2"),
            (3, "Database 3"),
            (4, "Database 4"),
            (5, "default"),
        )
        with self.connection:
            for root_id, name in roots:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO source_root_slots(id,name,status)
                    VALUES(?,?, 'unconfigured')
                    """,
                    (root_id, name),
                )

    def _backfill_source_locations(self) -> None:
        with self.connection:
            sources = self.connection.execute(
                """
                SELECT id FROM registered_sources s
                WHERE NOT EXISTS (
                    SELECT 1 FROM source_location_backfills b WHERE b.source_id=s.id
                )
                """
            ).fetchall()
            for source in sources:
                source_id = source["id"]
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO file_locations(
                        source_id,path,file_kind,size_bytes,modified_ns,sha256,
                        content_id,state,is_present,last_seen_scan_id
                    )
                    SELECT q.source_id,q.path,'ols',q.size_bytes,q.modified_ns,
                           q.sha256,q.content_id,q.status,1,
                           (SELECT r.id FROM source_scan_runs r
                            WHERE r.source_id=q.source_id AND r.status='available'
                            ORDER BY r.id DESC LIMIT 1)
                    FROM index_queue q
                    WHERE q.source_id=? AND q.size_bytes>=0 AND q.modified_ns>=0
                    """,
                    (source_id,),
                )
                self.connection.execute(
                    "INSERT INTO source_location_backfills(source_id) VALUES(?)",
                    (source_id,),
                )

    def _archive_unrecorded_reference_checks(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO reference_check_history(
                    origin_check_id, artifact_id, artifact_kind, artifact_path,
                    artifact_sha256, source_sha256, status, message,
                    comparison_json, checked_at
                )
                SELECT c.id, c.artifact_id, a.kind, a.path, a.sha256,
                       c.source_sha256, c.status, c.message,
                       c.comparison_json, c.checked_at
                FROM reference_checks c
                JOIN reference_artifacts a ON a.id=c.artifact_id
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM reference_check_history h
                    WHERE h.artifact_id=c.artifact_id
                      AND h.artifact_path=a.path
                      AND h.artifact_sha256=a.sha256
                      AND h.source_sha256=c.source_sha256
                      AND h.status=c.status
                      AND h.message=c.message
                      AND h.comparison_json=c.comparison_json
                )
                """
            )

    def _migrate_to_current(self) -> None:
        columns_to_add = {
            "project_versions": {
                "parent_id": "INTEGER",
                "is_root": "INTEGER NOT NULL DEFAULT 0",
            },
            "map_definitions": {
                "folder_id": "INTEGER NOT NULL DEFAULT 0",
                "type_code": "INTEGER NOT NULL DEFAULT 0",
                "version_relation_status": (
                    "TEXT NOT NULL DEFAULT 'unknown_not_exposed_by_parser'"
                ),
            },
            "map_axes": {
                "data_source": "INTEGER NOT NULL DEFAULT 0",
            },
            "project_diagnostics": {
                "error_count": "INTEGER NOT NULL DEFAULT 0",
                "warning_count": "INTEGER NOT NULL DEFAULT 0",
            },
            "registered_sources": {
                "last_attempt_at": "TEXT",
            },
            "index_queue": {
                "content_id": "INTEGER REFERENCES file_contents(id) ON DELETE SET NULL",
            },
            "file_locations": {
                "filename": "TEXT NOT NULL DEFAULT ''",
                "extension": "TEXT NOT NULL DEFAULT ''",
                "last_seen_state": "TEXT NOT NULL DEFAULT 'known'",
            },
        }
        with self.connection:
            for table, columns in columns_to_add.items():
                existing = {
                    row["name"]
                    for row in self.connection.execute(f"PRAGMA table_info({table})")
                }
                for column, definition in columns.items():
                    if column not in existing:
                        self.connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        )
            self.connection.execute(
                "UPDATE schema_info SET version=?", (SCHEMA_VERSION,)
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_file_locations_scan
                ON file_locations(source_id,last_seen_scan_id,extension)
                """
            )
            root_slots = (
                ("Database 1", 1),
                ("Database 2", 2),
                ("Database 3", 3),
                ("Database 4", 4),
                ("default", 5),
            )
            for name, ordinal in root_slots:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO source_root_slots(id,name,status)
                    VALUES(?,?, 'unconfigured')
                    """,
                    (ordinal, name),
                )
            existing_source = self.connection.execute(
                "SELECT id,path,status FROM registered_sources ORDER BY id LIMIT 1"
            ).fetchone()
            if existing_source is not None:
                self.connection.execute(
                    """
                    UPDATE source_root_slots SET registered_source_id=?,status=?,
                        updated_at=CURRENT_TIMESTAMP WHERE id=1
                    """,
                    (
                        existing_source["id"],
                        existing_source["status"],
                    ),
                )

    def store_reference_package_run(
        self,
        *,
        project_id: int,
        source_sha256: str,
        manifest_path: str,
        manifest_sha256: str,
        run_type: str,
        status: str,
        report: dict,
    ) -> int:
        if status not in {"PASS", "FAIL", "INCONCLUSIVE", "NOT_TESTED"}:
            raise ValueError(f"Unsupported reference package status: {status!r}.")
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO reference_package_runs(
                    project_id, source_sha256, manifest_path, manifest_sha256,
                    run_type, status, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    source_sha256,
                    manifest_path,
                    manifest_sha256,
                    run_type,
                    status,
                    json.dumps(report, ensure_ascii=False, sort_keys=True),
                ),
            )
        return int(cursor.lastrowid)

    def list_reference_package_runs(
        self,
        project_id: int,
        *,
        limit: int = 100,
        run_type: str | None = None,
    ) -> list[dict]:
        if not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000.")
        if run_type is None:
            rows = self.connection.execute(
                """
                SELECT id, project_id, source_sha256, manifest_path,
                       manifest_sha256, run_type, status, report_json, created_at
                FROM reference_package_runs
                WHERE project_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT id, project_id, source_sha256, manifest_path,
                       manifest_sha256, run_type, status, report_json, created_at
                FROM reference_package_runs
                WHERE project_id=? AND run_type=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (project_id, run_type, limit),
            ).fetchall()
        return [
            {
                **dict(row),
                "report": json.loads(row["report_json"]),
            }
            for row in rows
        ]

    def store_export_task(
        self,
        *,
        project_id: int,
        source_path: str,
        source_sha256: str,
        reason: str,
        version_names: list[str],
        export_types: list[str],
        uncertainty: str,
        expected_outcome: str,
        priority: str,
    ) -> int:
        allowed_priorities = {
            "NO_EXPORT_REQUIRED",
            "LOW_PRIORITY",
            "NORMAL_PRIORITY",
            "HIGH_PRIORITY",
            "BLOCKING",
        }
        if priority not in allowed_priorities:
            raise ValueError(f"Unsupported export task priority: {priority!r}.")
        versions_json = json.dumps(version_names, ensure_ascii=False, sort_keys=True)
        export_types_json = json.dumps(export_types, ensure_ascii=False, sort_keys=True)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO export_tasks(
                    project_id, source_path, source_sha256, reason,
                    version_names_json, export_types_json, uncertainty,
                    expected_outcome, priority
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, source_sha256, reason, export_types_json)
                DO UPDATE SET
                    priority=excluded.priority,
                    updated_at=CURRENT_TIMESTAMP
                WHERE export_tasks.status='pending'
                """,
                (
                    project_id,
                    source_path,
                    source_sha256,
                    reason,
                    versions_json,
                    export_types_json,
                    uncertainty,
                    expected_outcome,
                    priority,
                ),
            )
            row = self.connection.execute(
                """
                SELECT id FROM export_tasks
                WHERE project_id=? AND source_sha256=? AND reason=?
                  AND export_types_json=?
                """,
                (project_id, source_sha256, reason, export_types_json),
            ).fetchone()
        if row is None:
            raise RuntimeError("Export task was not stored.")
        return int(row["id"])

    def list_export_tasks(self, *, status: str | None = None, limit: int = 500) -> list[dict]:
        if not 1 <= limit <= 5000:
            raise ValueError("Export task limit must be between 1 and 5000.")
        if status is not None and status not in {
            "pending", "in_progress", "completed", "blocked", "cancelled"
        }:
            raise ValueError(f"Unsupported export task status: {status!r}.")
        query = """
            SELECT id, project_id, source_path, source_sha256, reason,
                   version_names_json, export_types_json, uncertainty,
                   expected_outcome, priority, status, attempts, last_result,
                   created_at, updated_at
            FROM export_tasks
        """
        parameters: tuple = ()
        if status is not None:
            query += " WHERE status=?"
            parameters = (status,)
        query += """
            ORDER BY CASE priority
                WHEN 'BLOCKING' THEN 0
                WHEN 'HIGH_PRIORITY' THEN 1
                WHEN 'NORMAL_PRIORITY' THEN 2
                WHEN 'LOW_PRIORITY' THEN 3
                ELSE 4 END, id
            LIMIT ?
        """
        rows = self.connection.execute(query, (*parameters, limit)).fetchall()
        return [
            {
                **dict(row),
                "version_names": json.loads(row["version_names_json"]),
                "export_types": json.loads(row["export_types_json"]),
            }
            for row in rows
        ]

    def update_export_task(
        self,
        task_id: int,
        *,
        status: str,
        result: str,
    ) -> None:
        transitions = {
            "pending": {"in_progress", "blocked", "cancelled"},
            "in_progress": {"pending", "completed", "blocked"},
            "blocked": {"pending", "cancelled"},
            "completed": set(),
            "cancelled": set(),
        }
        with self.connection:
            row = self.connection.execute(
                "SELECT status FROM export_tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"No export task found with id {task_id}.")
            previous = row["status"]
            if status not in transitions.get(previous, set()):
                raise ValueError(
                    f"Invalid export task transition: {previous} -> {status}."
                )
            self.connection.execute(
                """
                UPDATE export_tasks
                SET status=?, attempts=attempts+?, last_result=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (status, int(status == "in_progress"), result, task_id),
            )
            self.connection.execute(
                """
                INSERT INTO export_task_history(task_id,from_status,to_status,result)
                VALUES(?,?,?,?)
                """,
                (task_id, previous, status, result),
            )

    def list_export_task_history(self, task_id: int, *, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("Export task history limit must be between 1 and 1000.")
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT id,task_id,from_status,to_status,result,occurred_at
                FROM export_task_history
                WHERE task_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (task_id, limit),
            )
        ]

    def store_inventory_run(self, report: dict) -> int:
        roots = report.get("roots", [])
        if not isinstance(roots, list):
            raise ValueError("Inventory report roots must be a list.")
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO inventory_runs(roots_json,report_json) VALUES(?,?)",
                (
                    json.dumps(roots, ensure_ascii=False, sort_keys=True),
                    json.dumps(report, ensure_ascii=False, sort_keys=True),
                ),
            )
        return int(cursor.lastrowid)

    def get_latest_inventory_run(self) -> dict | None:
        row = self.connection.execute(
            """
            SELECT id,roots_json,report_json,created_at
            FROM inventory_runs ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "roots": json.loads(row["roots_json"]),
            "report": json.loads(row["report_json"]),
            "created_at": row["created_at"],
        }

    def register_source(self, *, source_key: str, path: str, status: str, error: str = "") -> int:
        with self.connection:
            row = self.connection.execute(
                "SELECT id FROM registered_sources WHERE path=?", (path,)
            ).fetchone()
            if row is not None:
                self.connection.execute(
                    """
                    UPDATE registered_sources
                    SET status=?,last_error=?,last_attempt_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (status, error, row["id"]),
                )
                self.connection.execute(
                    """
                    UPDATE source_root_slots SET status=?,updated_at=CURRENT_TIMESTAMP
                    WHERE registered_source_id=?
                    """,
                    (status, row["id"]),
                )
                return int(row["id"])
            cursor = self.connection.execute(
                """
                INSERT INTO registered_sources(source_key,path,status,last_error)
                VALUES(?,?,?,?)
                """,
                (source_key, path, status, error),
            )
            return int(cursor.lastrowid)

    def configure_source_slot(self, slot_id: int, source_id: int) -> None:
        with self.connection:
            source = self.connection.execute(
                "SELECT status FROM registered_sources WHERE id=?", (source_id,)
            ).fetchone()
            if source is None:
                raise ValueError(f"Unknown registered source ID {source_id}.")
            slot = self.connection.execute(
                "SELECT id FROM source_root_slots WHERE id=?", (slot_id,)
            ).fetchone()
            if slot is None:
                raise ValueError(f"Source slot must be between 1 and 5, got {slot_id}.")
            self.connection.execute(
                """
                UPDATE source_root_slots SET registered_source_id=NULL,
                    status='unconfigured',updated_at=CURRENT_TIMESTAMP
                WHERE registered_source_id=?
                """,
                (source_id,),
            )
            self.connection.execute(
                """
                UPDATE source_root_slots SET registered_source_id=?,status=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (source_id, source["status"], slot_id),
            )

    def list_source_slots(self) -> list[dict]:
        results = []
        for row in self.connection.execute(
                """
                SELECT r.id AS slot_id,r.name,r.status AS slot_status,
                       s.id AS source_id,s.path,s.status AS source_status,
                       s.last_scan_at,s.found_files,s.total_size_bytes,
                       s.indexed_files,s.new_files,s.changed_files,s.last_error,
                       COALESCE((SELECT COUNT(*) FROM index_queue q
                                 WHERE q.source_id=s.id AND q.status IN
                                   ('pending','interrupted','in_progress','retryable')),0)
                           AS queued_files,
                       COALESCE((SELECT SUM(q.size_bytes) FROM index_queue q
                                 WHERE q.source_id=s.id AND q.status IN
                                   ('pending','interrupted','in_progress','retryable')
                                   AND q.size_bytes>0),0) AS queued_bytes,
                       COALESCE((SELECT COUNT(*) FROM index_queue q
                                 WHERE q.source_id=s.id AND q.status='failed'),0)
                           AS failed_files,
                       COALESCE((SELECT COUNT(*) FROM file_locations l
                                 WHERE l.source_id=s.id AND l.file_kind='ols'
                                   AND l.is_present=1),0) AS ols_locations,
                       COALESCE((SELECT SUM(size_bytes) FROM file_locations l
                                 WHERE l.source_id=s.id AND l.file_kind='ols'
                                   AND l.is_present=1),0) AS ols_physical_bytes,
                       (SELECT report_json FROM source_scan_runs r2
                        WHERE r2.source_id=s.id ORDER BY r2.id DESC LIMIT 1)
                           AS latest_scan_json
                FROM source_root_slots r
                LEFT JOIN registered_sources s ON s.id=r.registered_source_id
                ORDER BY r.id
                """
        ):
            result = dict(row)
            scan_json = result.pop("latest_scan_json")
            result["latest_scan"] = json.loads(scan_json) if scan_json else None
            results.append(result)
        return results

    def list_source_scan_progress(self) -> list[dict]:
        results = []
        for row in self.connection.execute(
            """
            SELECT r.id AS slot_id,r.name,s.id AS source_id,s.path,
                   s.status AS source_status,s.last_error,
                   scan.id AS scan_run_id,scan.status AS scan_status,
                   scan.report_json
            FROM source_root_slots r
            LEFT JOIN registered_sources s ON s.id=r.registered_source_id
            LEFT JOIN source_scan_runs scan ON scan.id=(
                SELECT MAX(scan_latest.id) FROM source_scan_runs scan_latest
                WHERE scan_latest.source_id=s.id
            )
            ORDER BY r.id
            """
        ):
            result = dict(row)
            raw_report = result.pop("report_json")
            result["progress"] = json.loads(raw_report) if raw_report else None
            results.append(result)
        return results

    def record_file_location(
        self, *, source_id: int, path: str, file_kind: str, size_bytes: int,
        modified_ns: int, sha256: str | None = None,
        content_id: int | None = None, state: str = "discovered",
        scan_run_id: int | None = None,
        filename: str = "", extension: str = "",
        last_seen_state: str = "known",
    ) -> int:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO file_locations(
                    source_id,path,file_kind,size_bytes,modified_ns,filename,extension,
                    sha256,content_id,state,last_seen_state,is_present,last_seen_scan_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)
                ON CONFLICT(source_id,path) DO UPDATE SET
                    file_kind=excluded.file_kind,
                    filename=excluded.filename,extension=excluded.extension,
                    last_seen_state=excluded.last_seen_state,
                    sha256=CASE
                        WHEN file_locations.size_bytes!=excluded.size_bytes
                          OR file_locations.modified_ns!=excluded.modified_ns
                        THEN excluded.sha256
                        ELSE COALESCE(excluded.sha256,file_locations.sha256) END,
                    content_id=CASE
                        WHEN file_locations.size_bytes!=excluded.size_bytes
                          OR file_locations.modified_ns!=excluded.modified_ns
                        THEN excluded.content_id
                        ELSE COALESCE(excluded.content_id,file_locations.content_id) END,
                    state=CASE
                        WHEN file_locations.size_bytes!=excluded.size_bytes
                          OR file_locations.modified_ns!=excluded.modified_ns
                        THEN excluded.state
                        WHEN excluded.state='discovered' AND file_locations.content_id IS NOT NULL
                        THEN file_locations.state
                        ELSE excluded.state END,
                    size_bytes=excluded.size_bytes,modified_ns=excluded.modified_ns,
                    is_present=1,
                    last_seen_scan_id=COALESCE(excluded.last_seen_scan_id,
                                               file_locations.last_seen_scan_id),
                    last_seen_at=CURRENT_TIMESTAMP
                """,
                (source_id,path,file_kind,size_bytes,modified_ns,filename,extension,
                 sha256,content_id,state,last_seen_state,scan_run_id),
            )
            row = self.connection.execute(
                "SELECT id FROM file_locations WHERE source_id=? AND path=?",
                (source_id, path),
            ).fetchone()
        return int(row["id"])

    def start_or_resume_source_scan(self, source_id: int, root_path: str) -> int:
        with self.connection:
            row = self.connection.execute(
                """
                SELECT r.id FROM source_scan_runs r
                WHERE r.source_id=? AND r.status IN ('scanning','interrupted','scan_warnings')
                  AND EXISTS (
                      SELECT 1 FROM source_scan_directories d
                      WHERE d.scan_run_id=r.id AND d.status!='complete'
                  )
                ORDER BY r.id DESC LIMIT 1
                """,
                (source_id,),
            ).fetchone()
            if row is not None:
                run_id = int(row["id"])
                self.connection.execute(
                    """
                    UPDATE source_scan_directories SET status='pending',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE scan_run_id=?
                      AND status IN ('in_progress','interrupted','retryable')
                    """,
                    (run_id,),
                )
                self.connection.execute(
                    "UPDATE source_scan_runs SET status='scanning' WHERE id=?",
                    (run_id,),
                )
            else:
                cursor = self.connection.execute(
                    """
                    INSERT INTO source_scan_runs(source_id,status,report_json)
                    VALUES(?, 'scanning', '{}')
                    """,
                    (source_id,),
                )
                run_id = int(cursor.lastrowid)
                self.connection.execute(
                    """
                    INSERT INTO source_scan_directories(
                        scan_run_id,source_id,path,status
                    ) VALUES(?,?,?,'pending')
                    """,
                    (run_id,source_id,root_path),
                )
        return run_id

    def claim_source_scan_directory(self, run_id: int) -> dict | None:
        with self.connection:
            row = self.connection.execute(
                """
                SELECT id,source_id,path FROM source_scan_directories
                WHERE scan_run_id=? AND status='pending'
                ORDER BY id LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                """
                UPDATE source_scan_directories SET status='in_progress',
                    attempts=attempts+1,error='',updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (row["id"],),
            )
        return dict(row)

    def enqueue_source_scan_directories(
        self, run_id: int, source_id: int, paths: list[str]
    ) -> None:
        if not paths:
            return
        with self.connection:
            self.connection.executemany(
                """
                INSERT OR IGNORE INTO source_scan_directories(
                    scan_run_id,source_id,path,status
                ) VALUES(?,?,?,'pending')
                """,
                ((run_id,source_id,path) for path in paths),
            )

    def finish_source_scan_directory(
        self, directory_id: int, *, status: str, files_seen: int = 0,
        error: str = "",
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE source_scan_directories SET status=?,files_seen=?,error=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (status,files_seen,error,directory_id),
            )

    def record_source_scan_errors(
        self, scan_run_id: int, source_id: int, errors: list[dict]
    ) -> None:
        if not errors:
            return
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO source_scan_errors(
                    scan_run_id,source_id,path,scope,error_type,message
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    (
                        scan_run_id,source_id,item["path"],item["scope"],
                        item["error_type"],item["message"],
                    )
                    for item in errors
                ),
            )

    def record_source_inventory_batch(
        self, source_id: int, scan_run_id: int, observations: list[dict],
        *, force_recheck: bool = False,
    ) -> dict[str, int]:
        counts = {"new": 0, "known": 0, "changed": 0, "indexed": 0, "needs_hash": 0}
        if not observations:
            return counts
        with self.connection:
            for item in observations:
                old = self.connection.execute(
                    """
                    SELECT size_bytes,modified_ns,content_id FROM file_locations
                    WHERE source_id=? AND path=?
                    """,
                    (source_id,item["path"]),
                ).fetchone()
                if old is None:
                    state = "new"
                elif (
                    old["size_bytes"] != item["size_bytes"]
                    or old["modified_ns"] != item["modified_ns"]
                ):
                    state = "changed"
                else:
                    state = "known"
                counts[state] += 1
                self.connection.execute(
                    """
                    INSERT INTO file_locations(
                        source_id,path,file_kind,size_bytes,modified_ns,filename,
                        extension,state,last_seen_state,is_present,last_seen_scan_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,1,?)
                    ON CONFLICT(source_id,path) DO UPDATE SET
                        file_kind=excluded.file_kind,size_bytes=excluded.size_bytes,
                        modified_ns=excluded.modified_ns,filename=excluded.filename,
                        extension=excluded.extension,
                        sha256=CASE WHEN file_locations.size_bytes!=excluded.size_bytes
                          OR file_locations.modified_ns!=excluded.modified_ns
                          THEN NULL ELSE file_locations.sha256 END,
                        content_id=CASE WHEN file_locations.size_bytes!=excluded.size_bytes
                          OR file_locations.modified_ns!=excluded.modified_ns
                          THEN NULL ELSE file_locations.content_id END,
                        state=CASE WHEN excluded.last_seen_state='changed'
                          THEN 'metadata_changed' ELSE file_locations.state END,
                        last_seen_state=excluded.last_seen_state,is_present=1,
                        last_seen_scan_id=excluded.last_seen_scan_id,
                        last_seen_at=CURRENT_TIMESTAMP
                    """,
                    (
                        source_id,item["path"],item["file_kind"],item["size_bytes"],
                        item["modified_ns"],item["filename"],item["extension"],
                        "discovered",state,scan_run_id,
                    ),
                )
                if item["file_kind"] != "ols":
                    continue
                indexed = self.connection.execute(
                    """
                    SELECT size_bytes,modified_ns FROM source_files WHERE path=?
                    """,
                    (item["path"],),
                ).fetchone()
                if indexed is not None:
                    counts["indexed"] += 1
                queued = self.connection.execute(
                    """
                    SELECT size_bytes,modified_ns,status FROM index_queue
                    WHERE source_id=? AND path=?
                    """,
                    (source_id,item["path"]),
                ).fetchone()
                metadata_changed = (
                    state == "changed"
                    or (indexed is not None and (
                        indexed["size_bytes"] != item["size_bytes"]
                        or indexed["modified_ns"] != item["modified_ns"]
                    ))
                    or (queued is not None and (
                        queued["size_bytes"] != item["size_bytes"]
                        or queued["modified_ns"] != item["modified_ns"]
                    ))
                )
                if metadata_changed or force_recheck:
                    queue_status = "pending"
                    queue_message = "Awaiting streaming content hash and parser verification."
                    counts["needs_hash"] += 1
                elif queued is None:
                    if indexed is not None and (
                        indexed["size_bytes"] == item["size_bytes"]
                        and indexed["modified_ns"] == item["modified_ns"]
                    ):
                        queue_status = "fingerprint_match_unverified"
                        queue_message = (
                            "Size and modification time match an indexed record; "
                            "content hash has not been reverified."
                        )
                    else:
                        queue_status = "pending"
                        queue_message = "Awaiting streaming content hash and parser verification."
                        counts["needs_hash"] += 1
                else:
                    queue_status = queued["status"]
                    queue_message = ""
                    if queue_status in (
                        "pending","interrupted","in_progress","retryable","failed"
                    ):
                        counts["needs_hash"] += 1
                self.connection.execute(
                    """
                    INSERT INTO index_queue(
                        source_id,path,size_bytes,modified_ns,status,message
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(source_id,path) DO UPDATE SET
                        size_bytes=excluded.size_bytes,modified_ns=excluded.modified_ns,
                        status=CASE WHEN ? THEN 'pending' ELSE index_queue.status END,
                        sha256=CASE WHEN ? THEN NULL ELSE index_queue.sha256 END,
                        content_id=CASE WHEN ? THEN NULL ELSE index_queue.content_id END,
                        message=CASE WHEN ? THEN excluded.message ELSE index_queue.message END,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        source_id,item["path"],item["size_bytes"],item["modified_ns"],
                        queue_status,queue_message,int(metadata_changed or force_recheck),
                        int(metadata_changed or force_recheck),
                        int(metadata_changed or force_recheck),
                        int(metadata_changed or force_recheck),
                    ),
                )
        return counts

    def source_inventory_report(
        self, source_id: int, scan_run_id: int, *, error_limit: int = 100
    ) -> dict:
        aggregate = self.connection.execute(
            """
            SELECT COUNT(*) AS found_paths,
                   COALESCE(SUM(size_bytes),0) AS physical_bytes,
                   SUM(CASE WHEN extension='ols' THEN 1 ELSE 0 END) AS ols_paths,
                   COALESCE(SUM(CASE WHEN extension='ols' THEN size_bytes ELSE 0 END),0)
                       AS ols_physical_bytes,
                   SUM(CASE WHEN last_seen_state='new' THEN 1 ELSE 0 END) AS new_locations,
                   SUM(CASE WHEN last_seen_state='known' THEN 1 ELSE 0 END) AS known_locations,
                   SUM(CASE WHEN last_seen_state='changed' THEN 1 ELSE 0 END) AS changed_locations,
                   SUM(CASE WHEN extension='ols' AND last_seen_state='new' THEN 1 ELSE 0 END)
                       AS new_ols_files,
                   SUM(CASE WHEN extension='ols' AND last_seen_state='changed' THEN 1 ELSE 0 END)
                       AS stat_changed_ols_files,
                   SUM(CASE WHEN extension='ols' AND sha256 IS NULL THEN 1 ELSE 0 END)
                       AS unhashed_ols_locations
            FROM file_locations
            WHERE source_id=? AND is_present=1 AND last_seen_scan_id=?
            """,
            (source_id,scan_run_id),
        ).fetchone()
        extensions = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT extension,COUNT(*) AS files,COALESCE(SUM(size_bytes),0) AS bytes
                FROM file_locations
                WHERE source_id=? AND is_present=1 AND last_seen_scan_id=?
                GROUP BY extension ORDER BY files DESC,extension
                """,
                (source_id,scan_run_id),
            )
        ]
        directories = self.connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) AS complete,
                   SUM(CASE WHEN status IN ('retryable','in_progress','pending',
                                            'interrupted') THEN 1 ELSE 0 END) AS incomplete
            FROM source_scan_directories WHERE scan_run_id=?
            """,
            (scan_run_id,),
        ).fetchone()
        errors = self.connection.execute(
            """
            SELECT COUNT(*) FROM source_scan_errors
            WHERE scan_run_id=? AND source_id=?
            """,
            (scan_run_id,source_id),
        ).fetchone()[0]
        error_samples = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT path,scope,error_type,message FROM source_scan_errors
                WHERE scan_run_id=? AND source_id=? ORDER BY id LIMIT ?
                """,
                (scan_run_id,source_id,error_limit),
            )
        ]
        potential_duplicate_groups = self.connection.execute(
            """
            SELECT LOWER(filename) AS filename,size_bytes,COUNT(*) AS locations
            FROM file_locations
            WHERE source_id=? AND is_present=1 AND last_seen_scan_id=?
              AND filename!=''
            GROUP BY LOWER(filename),size_bytes HAVING COUNT(*)>1
            """,
            (source_id,scan_run_id),
        ).fetchall()
        unindexed = self.connection.execute(
            """
            SELECT COUNT(*) FROM file_locations l
            WHERE l.source_id=? AND l.is_present=1 AND l.last_seen_scan_id=?
              AND l.extension='ols'
              AND NOT EXISTS (SELECT 1 FROM source_files s WHERE s.path=l.path)
            """,
            (source_id,scan_run_id),
        ).fetchone()[0]
        cached_fingerprints = self.connection.execute(
            """
            SELECT COUNT(*) FROM file_locations l
            JOIN source_files s ON s.path=l.path
            WHERE l.source_id=? AND l.is_present=1 AND l.last_seen_scan_id=?
              AND l.extension='ols' AND l.size_bytes=s.size_bytes
              AND l.modified_ns=s.modified_ns
            """,
            (source_id,scan_run_id),
        ).fetchone()[0]
        return {
            "found_paths": int(aggregate["found_paths"] or 0),
            "physical_bytes": int(aggregate["physical_bytes"] or 0),
            "ols_paths": int(aggregate["ols_paths"] or 0),
            "ols_physical_bytes": int(aggregate["ols_physical_bytes"] or 0),
            "new_locations": int(aggregate["new_locations"] or 0),
            "known_locations": int(aggregate["known_locations"] or 0),
            "changed_locations": int(aggregate["changed_locations"] or 0),
            "new_ols_files": int(aggregate["new_ols_files"] or 0),
            "stat_changed_ols_files": int(
                aggregate["stat_changed_ols_files"] or 0
            ),
            "cached_fingerprint_files": int(cached_fingerprints),
            "unhashed_ols_locations": int(aggregate["unhashed_ols_locations"] or 0),
            "unindexed_ols_locations": int(unindexed),
            "extensions": extensions,
            "directories_total": int(directories["total"] or 0),
            "directories_complete": int(directories["complete"] or 0),
            "directories_incomplete": int(directories["incomplete"] or 0),
            "errors": int(errors),
            "error_samples": error_samples,
            "possible_duplicate_groups_by_filename_and_size": len(
                potential_duplicate_groups
            ),
            "possible_duplicate_locations_beyond_first": sum(
                int(row["locations"]) - 1 for row in potential_duplicate_groups
            ),
            "duplicate_basis": (
                "Candidate only: same filename and size; not verified content duplicates."
            ),
        }

    def inventory_duplicate_candidates(
        self, scan_run_ids: list[int], *, limit: int = 100
    ) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("Duplicate candidate limit must be between 1 and 1000.")
        if not scan_run_ids:
            return []
        placeholders = ",".join("?" for _ in scan_run_ids)
        groups = self.connection.execute(
            f"""
            SELECT LOWER(filename) AS filename,size_bytes,COUNT(*) AS locations
            FROM file_locations
            WHERE is_present=1 AND filename!='' AND last_seen_scan_id IN ({placeholders})
            GROUP BY LOWER(filename),size_bytes HAVING COUNT(*)>1
            ORDER BY locations DESC,filename,size_bytes LIMIT ?
            """,
            (*scan_run_ids,limit),
        ).fetchall()
        result = []
        for group in groups:
            locations = self.connection.execute(
                f"""
                SELECT l.source_id,r.name AS source_root,l.path,l.size_bytes,
                       l.extension,l.sha256
                FROM file_locations l
                LEFT JOIN source_root_slots r ON r.registered_source_id=l.source_id
                WHERE l.is_present=1 AND LOWER(l.filename)=? AND l.size_bytes=?
                  AND l.last_seen_scan_id IN ({placeholders})
                ORDER BY l.source_id,l.path
                """,
                (group["filename"],group["size_bytes"],*scan_run_ids),
            ).fetchall()
            result.append({
                "filename":group["filename"],
                "size_bytes":group["size_bytes"],
                "locations_count":group["locations"],
                "basis":"same filename and size; content identity not verified",
                "locations":[dict(row) for row in locations],
            })
        return result

    def list_inventory_locations(
        self, *, source_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("Location page limit must be 1-1000 and offset nonnegative.")
        clauses = ["l.is_present=1"]
        params: list[object] = []
        if source_id is not None:
            clauses.append("l.source_id=?")
            params.append(source_id)
        params.extend((limit,offset))
        return [
            dict(row)
            for row in self.connection.execute(
                f"""
                SELECT l.source_id,r.name AS source_root,s.path AS source_root_path,
                       l.path,l.filename,l.extension,l.size_bytes,l.modified_ns,
                       l.state,l.last_seen_state,l.sha256,
                       CASE WHEN l.sha256 IS NULL THEN 1 ELSE 0 END AS needs_hash,
                       CASE WHEN f.id IS NULL THEN 0 ELSE 1 END AS is_indexed,
                       q.status AS queue_status
                FROM file_locations l
                JOIN registered_sources s ON s.id=l.source_id
                LEFT JOIN source_root_slots r ON r.registered_source_id=s.id
                LEFT JOIN source_files f ON f.path=l.path
                LEFT JOIN index_queue q ON q.source_id=l.source_id AND q.path=l.path
                WHERE {' AND '.join(clauses)}
                ORDER BY l.source_id,l.path LIMIT ? OFFSET ?
                """,
                params,
            )
        ]

    def start_source_scan_run(self, source_id: int) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO source_scan_runs(source_id,status,report_json)
                VALUES(?, 'scanning', '{}')
                """,
                (source_id,),
            )
        return int(cursor.lastrowid)

    def finish_source_scan_run(
        self, run_id: int, *, source_id: int, status: str, report: dict,
        complete: bool,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE source_scan_runs SET status=?,report_json=? WHERE id=?",
                (status, json.dumps(report, ensure_ascii=False, sort_keys=True), run_id),
            )
            if complete:
                self.connection.execute(
                    """
                    UPDATE file_locations SET is_present=0
                    WHERE source_id=? AND is_present=1
                      AND (last_seen_scan_id IS NULL OR last_seen_scan_id!=?)
                    """,
                    (source_id, run_id),
                )

    def store_file_content(
            self, *, sha256: str, size_bytes: int, file_kind: str,
            parser_name: str, parser_version: str, application_version: str,
            processing_status: str, project_id: int | None,
    ) -> int:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO file_contents(sha256,size_bytes,file_kind)
                    VALUES(?,?,?) ON CONFLICT(sha256,size_bytes,file_kind) DO NOTHING
                    """,
                    (sha256,size_bytes,file_kind),
                )
                row = self.connection.execute(
                    """
                    SELECT id FROM file_contents
                    WHERE sha256=? AND size_bytes=? AND file_kind=?
                    """,
                    (sha256,size_bytes,file_kind),
                ).fetchone()
                content_id = int(row["id"])
                self.connection.execute(
                    """
                    INSERT INTO file_processing(
                        content_id,parser_name,parser_version,application_version,
                        processing_status,project_id
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(content_id,parser_name,parser_version,application_version)
                    DO UPDATE SET processing_status=excluded.processing_status,
                        project_id=COALESCE(excluded.project_id,file_processing.project_id)
                    """,
                    (content_id,parser_name,parser_version,application_version,
                     processing_status,project_id),
                )
            return content_id

    def store_content_identity(self, *, sha256: str, size_bytes: int, file_kind: str) -> int:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO file_contents(sha256,size_bytes,file_kind)
                    VALUES(?,?,?) ON CONFLICT(sha256,size_bytes,file_kind) DO NOTHING
                    """,
                    (sha256,size_bytes,file_kind),
                )
                row = self.connection.execute(
                    """
                    SELECT id FROM file_contents
                    WHERE sha256=? AND size_bytes=? AND file_kind=?
                    """,
                    (sha256,size_bytes,file_kind),
                ).fetchone()
            return int(row["id"])

    def record_benchmark_hash(
            self, *, source_id: int, path: str, sha256: str,
            size_bytes: int, modified_ns: int,
    ) -> int:
            content_id = self.store_content_identity(
                sha256=sha256,size_bytes=size_bytes,file_kind="ols"
            )
            with self.connection:
                cursor = self.connection.execute(
                    """
                    UPDATE file_locations SET sha256=?,content_id=?,
                        state='hash_benchmark_sample',last_seen_at=CURRENT_TIMESTAMP
                    WHERE source_id=? AND path=? AND size_bytes=? AND modified_ns=?
                    """,
                    (sha256,content_id,source_id,path,size_bytes,modified_ns),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"Inventory location changed before benchmark hash was stored: {path}"
                    )
                self.connection.execute(
                    """
                    UPDATE index_queue SET sha256=?,content_id=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE source_id=? AND path=?
                    """,
                    (sha256,content_id,source_id,path),
                )
            return content_id

    def sync_hash_jobs(
        self, source_ids: list[int], *, retry_failed: bool = False
    ) -> int:
        if not source_ids:
            return 0
        placeholders = ",".join("?" for _ in source_ids)
        retry_clause = (
            "WHEN hash_jobs.status IN ('HASH_FAILED','SOURCE_UNAVAILABLE') "
            "THEN 'HASH_PENDING' "
            if retry_failed else ""
        )
        with self.connection:
            before = self.connection.total_changes
            self.connection.execute(
               f"""
               INSERT INTO hash_jobs(
                   source_id,path,filename,size_bytes,modified_ns,status,sha256
               )
               SELECT source_id,path,filename,size_bytes,modified_ns,
                      CASE WHEN sha256 IS NULL THEN 'HASH_PENDING' ELSE 'HASHED' END,
                      sha256
               FROM file_locations
               WHERE file_kind='ols' AND is_present=1
                 AND source_id IN ({placeholders})
               ON CONFLICT(source_id,path) DO UPDATE SET
                   filename=excluded.filename,
                   status=CASE
                       WHEN hash_jobs.size_bytes!=excluded.size_bytes
                         OR hash_jobs.modified_ns!=excluded.modified_ns
                       THEN 'HASH_PENDING'
                       {retry_clause}
                       WHEN excluded.sha256 IS NOT NULL THEN 'HASHED'
                       WHEN hash_jobs.status='HASHED' THEN 'HASH_PENDING'
                       ELSE hash_jobs.status END,
                   size_bytes=excluded.size_bytes,
                   modified_ns=excluded.modified_ns,
                   sha256=CASE
                       WHEN hash_jobs.size_bytes!=excluded.size_bytes
                         OR hash_jobs.modified_ns!=excluded.modified_ns
                       THEN NULL ELSE excluded.sha256 END,
                   error=CASE
                       WHEN hash_jobs.size_bytes!=excluded.size_bytes
                         OR hash_jobs.modified_ns!=excluded.modified_ns
                       THEN '' ELSE hash_jobs.error END,
                   updated_at=CURRENT_TIMESTAMP
               """,
               source_ids,
            )
            return self.connection.total_changes - before

    def begin_hash_run(
        self, *, worker_count: int, chunk_size: int,
        worker_owner_token: str | None = None,
    ) -> int:
        if not 1 <= worker_count <= 2:
            raise ValueError("Hash workers must be between 1 and 2.")
        if not 64 * 1024 <= chunk_size <= 16 * 1024 * 1024:
            raise ValueError("Hash chunk size must be between 64 KiB and 16 MiB.")
        with self.connection:
            worker = self.connection.execute(
                "SELECT status,owner_token FROM hash_worker_state WHERE id=1"
            ).fetchone()
            if (
                worker and worker["status"] in {"running","paused"}
                and worker["owner_token"] != worker_owner_token
            ):
                raise RuntimeError(
                    "A continuous hash worker owns the queue; stop it before starting a separate run."
                )
            control = self.connection.execute(
               "SELECT status,run_id FROM hash_control WHERE id=1"
            ).fetchone()
            previous_run = (
               self.connection.execute(
                   "SELECT status FROM hash_runs WHERE id=?",
                   (control["run_id"],),
               ).fetchone()
               if control and control["run_id"] is not None else None
            )
            if previous_run and previous_run["status"] == "RUNNING":
               raise RuntimeError(
                   "A hash run is active or interrupted. Use hashing recover only "
                   "after confirming no hashing process is still running."
               )
            cursor = self.connection.execute(
               """
               INSERT INTO hash_runs(status,worker_count,chunk_size)
               VALUES('RUNNING',?,?)
               """,
               (worker_count,chunk_size),
            )
            run_id = int(cursor.lastrowid)
            self.connection.execute(
               """
               INSERT INTO hash_control(id,status,run_id,updated_at)
               VALUES(1,'running',?,CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET status='running',run_id=excluded.run_id,
                   updated_at=CURRENT_TIMESTAMP
               """,
               (run_id,),
            )
        return run_id

    def recover_hash_run(self) -> dict:
        with self.connection:
            worker = self.connection.execute(
                "SELECT status,process_id FROM hash_worker_state WHERE id=1"
            ).fetchone()
            process_id = worker["process_id"] if worker else None
            if process_id is not None:
                process_id = int(process_id)
                if process_id <= 0:
                    raise RuntimeError(
                        "Hash worker process ID is invalid; refusing recovery."
                    )
                if _is_process_alive(process_id):
                    raise RuntimeError(
                        f"Hash worker process {process_id} appears active; "
                        "refusing recovery."
                    )
            elif worker and worker["status"] in {"running", "paused"}:
                raise RuntimeError(
                    "Hash worker status is active but its process ID is unknown; "
                    "refusing recovery."
                )
            control = self.connection.execute(
                "SELECT status,run_id FROM hash_control WHERE id=1"
            ).fetchone()
            run_id = control["run_id"] if control else None
            recovered_run_id = None
            if run_id is not None:
                run = self.connection.execute(
                    "SELECT status FROM hash_runs WHERE id=?",
                    (run_id,),
                ).fetchone()
                if run and run["status"] == "RUNNING":
                    cursor = self.connection.execute(
                        """
                        UPDATE hash_runs SET status='INTERRUPTED',
                            finished_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status='RUNNING'
                        """,
                        (run_id,),
                    )
                    if cursor.rowcount:
                        recovered_run_id = run_id
            cursor = self.connection.execute(
                """
                UPDATE hash_jobs SET status='RETRY_PENDING',run_id=NULL,
                    error='Recovered after interrupted hash process.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE status='HASHING'
                """
            )
            self.connection.execute(
                """
                UPDATE hash_control SET status='idle',run_id=NULL,
                    updated_at=CURRENT_TIMESTAMP WHERE id=1
                """
            )
            self.connection.execute(
                """
                UPDATE hash_worker_state SET status='stopped',owner_token=NULL,
                    process_id=NULL,active_run_id=NULL,heartbeat_epoch=0,
                    last_error='Recovered after confirming no worker is active.'
                WHERE id=1
                """
            )
        return {
            "recovered_run_id":recovered_run_id,
            "jobs_requeued":cursor.rowcount,
            "control":self.hash_control_status(),
        }

    def acquire_hash_worker(
        self, *, owner_token: str, process_id: int, workers: int,
        batch_size: int, chunk_size: int, batch_delay_seconds: float,
        retry_limit: int, retry_delay_seconds: float,
    ) -> None:
        with self.connection:
            current = self.connection.execute(
                "SELECT status,process_id,active_run_id FROM hash_worker_state WHERE id=1"
            ).fetchone()
            if current and current["status"] in {"running", "paused"}:
                previous_pid = current["process_id"]
                if previous_pid:
                    previous_pid = int(previous_pid)
                    if _is_process_alive(previous_pid):
                        raise RuntimeError(
                            f"Hash worker process {previous_pid} is already active."
                        )
                active_run_id = current["active_run_id"]
                if active_run_id is not None:
                    self.connection.execute(
                        """
                        UPDATE hash_runs SET status='INTERRUPTED',
                            finished_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status='RUNNING'
                        """,
                        (active_run_id,),
                    )
                    self.connection.execute(
                        """
                        UPDATE hash_jobs SET status='RETRY_PENDING',run_id=NULL,
                            error='Recovered after hash worker process exit.',
                            updated_at=CURRENT_TIMESTAMP
                        WHERE run_id=? AND status='HASHING'
                        """,
                        (active_run_id,),
                    )
            self.connection.execute(
                """
                UPDATE hash_worker_state SET status='running',owner_token=?,
                    process_id=?,workers=?,batch_size=?,chunk_size=?,
                    batch_delay_seconds=?,retry_limit=?,retry_delay_seconds=?,
                    active_run_id=NULL,batches_completed=0,files_completed=0,
                    bytes_read=0,error_count=0,started_at=CURRENT_TIMESTAMP,
                    heartbeat_epoch=?,last_error=''
                WHERE id=1
                """,
                (
                    owner_token,process_id,workers,batch_size,chunk_size,
                    batch_delay_seconds,retry_limit,retry_delay_seconds,time.time(),
                ),
            )
            self.connection.execute(
                """
                UPDATE hash_control SET status='running',updated_at=CURRENT_TIMESTAMP
                WHERE id=1
                """
            )
            self.connection.execute(
                """
                UPDATE hash_runs SET status='PAUSED',finished_at=CURRENT_TIMESTAMP
                WHERE id=(
                    SELECT run_id FROM hash_control WHERE id=1
                ) AND status='RUNNING'
                """
            )
            self.connection.execute(
                """
                UPDATE hash_control SET status='idle',run_id=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=1 AND status='running' AND run_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM hash_runs r
                      WHERE r.id=hash_control.run_id AND r.status='RUNNING'
                  )
                """
            )

    def update_hash_worker(
        self, *, owner_token: str, status: str | None = None,
        active_run_id: int | None = None, batches_delta: int = 0,
        files_delta: int = 0, bytes_delta: int = 0, errors_delta: int = 0,
        last_error: str | None = None,
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE hash_worker_state SET
                    status=COALESCE(?,status),
                    active_run_id=?,
                    batches_completed=batches_completed+?,
                    files_completed=files_completed+?,
                    bytes_read=bytes_read+?,
                    error_count=error_count+?,
                    heartbeat_epoch=?,
                    last_error=COALESCE(?,last_error)
                WHERE id=1 AND owner_token=?
                """,
                (
                    status,active_run_id,batches_delta,files_delta,bytes_delta,
                    errors_delta,time.time(),last_error,owner_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The continuous hash worker lease was lost.")

    def release_hash_worker(
        self, *, owner_token: str, status: str, error: str = ""
    ) -> None:
        if status not in {"stopped", "complete", "failed", "needs_attention"}:
            raise ValueError(f"Unsupported final hash worker status: {status}.")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE hash_worker_state SET status=?,owner_token=NULL,
                    process_id=NULL,active_run_id=NULL,heartbeat_epoch=?,
                    last_error=?
                WHERE id=1 AND owner_token=?
                """,
                (status,time.time(),error,owner_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The continuous hash worker lease was lost.")

    def hash_worker_status(self) -> dict:
        row = self.connection.execute(
            "SELECT * FROM hash_worker_state WHERE id=1"
        ).fetchone()
        result = dict(row) if row else {"status": "stopped"}
        active = self.connection.execute(
            """
            SELECT COUNT(*) AS workers,COALESCE(SUM(bytes_hashed),0) AS bytes
            FROM hash_jobs WHERE status='HASHING'
            """
        ).fetchone()
        result["active_workers"] = int(active["workers"])
        result["active_bytes_read"] = int(active["bytes"])
        return result

    def record_hash_progress(
        self, *, job_id: int, run_id: int, owner_token: str,
        bytes_hashed: int, bytes_delta: int,
    ) -> None:
        connection = sqlite3.connect(self.path,timeout=30)
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE hash_jobs SET bytes_hashed=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND run_id=? AND status='HASHING'
                    """,
                    (bytes_hashed,job_id,run_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Hash job is no longer active.")
                cursor = connection.execute(
                    """
                    UPDATE hash_worker_state SET bytes_read=bytes_read+?,
                        heartbeat_epoch=? WHERE id=1 AND owner_token=?
                    """,
                    (bytes_delta,time.time(),owner_token),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("The continuous hash worker lease was lost.")
        finally:
            connection.close()

    def refresh_changed_hash_job(
        self, *, job_id: int, size_bytes: int, modified_ns: int,
        retry_limit: int,
    ) -> bool:
        with self.connection:
            job = self.connection.execute(
                """
                SELECT source_id,path,attempts FROM hash_jobs
                WHERE id=? AND status='FILE_CHANGED'
                """,
                (job_id,),
            ).fetchone()
            if job is None:
                return False
            status = (
                "HASH_FAILED" if int(job["attempts"]) >= retry_limit
                else "RETRY_PENDING"
            )
            self.connection.execute(
                """
                UPDATE file_locations SET size_bytes=?,modified_ns=?,sha256=NULL,
                    content_id=NULL,state='discovered',last_seen_at=CURRENT_TIMESTAMP
                WHERE source_id=? AND path=? AND is_present=1
                """,
                (size_bytes,modified_ns,job["source_id"],job["path"]),
            )
            self.connection.execute(
                """
                UPDATE index_queue SET size_bytes=?,modified_ns=?,sha256=NULL,
                    content_id=NULL,status='pending',
                    message='Fingerprint refreshed after file changed during hashing.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE source_id=? AND path=?
                """,
                (size_bytes,modified_ns,job["source_id"],job["path"]),
            )
            self.connection.execute(
                """
                UPDATE hash_jobs SET size_bytes=?,modified_ns=?,sha256=NULL,
                    status=?,error='Fingerprint refreshed after file changed during hashing.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (size_bytes,modified_ns,status,job_id),
            )
            return status == "RETRY_PENDING"

    def mark_hash_job_source_unavailable(self, job_id: int, error: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE hash_jobs SET status='SOURCE_UNAVAILABLE',error=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='FILE_CHANGED'
                """,
                (error,job_id),
            )

    def exhaust_hash_retries(self, source_ids: list[int], retry_limit: int) -> int:
        if not source_ids:
            return 0
        placeholders = ",".join("?" for _ in source_ids)
        with self.connection:
            cursor = self.connection.execute(
                f"""
                UPDATE hash_jobs SET status='HASH_FAILED',
                    error='Maximum automatic hash attempts reached.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE source_id IN ({placeholders})
                  AND attempts>=? AND status IN ('RETRY_PENDING','SOURCE_UNAVAILABLE')
                """,
                (*source_ids,retry_limit),
            )
        return cursor.rowcount

    def hash_control_status(self) -> dict:
        row = self.connection.execute(
            "SELECT status,run_id,updated_at FROM hash_control WHERE id=1"
        ).fetchone()
        return dict(row) if row else {"status": "idle", "run_id": None}

    def set_hash_control(self, status: str) -> dict:
        if status not in {"paused", "running", "cancelled"}:
            raise ValueError("Hash control status must be paused, running, or cancelled.")
        with self.connection:
            worker = self.connection.execute(
                "SELECT status,owner_token FROM hash_worker_state WHERE id=1"
            ).fetchone()
            if status == "running" and (
                worker is None or worker["status"] not in {"running","paused"}
            ):
                status = "idle"
            if status in {"paused","cancelled"} and (
                worker is None or worker["status"] not in {"running","paused"}
            ):
                return self.hash_control_status()
            self.connection.execute(
               """
               UPDATE hash_control SET status=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=1
               """,
               (status,),
            )
        return self.hash_control_status()

    def claim_hash_jobs(
        self, *, run_id: int, source_ids: list[int], limit: int
    ) -> list[dict]:
        if not source_ids or limit < 1:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        with self.connection:
            rows = self.connection.execute(
               f"""
               SELECT j.id,j.source_id,j.path,j.filename,j.size_bytes,j.modified_ns
               FROM hash_jobs j
               JOIN file_locations l ON l.source_id=j.source_id AND l.path=j.path
               JOIN registered_sources s ON s.id=j.source_id
               WHERE j.status IN ('HASH_PENDING','RETRY_PENDING')
                 AND l.is_present=1 AND l.sha256 IS NULL
                 AND s.status='available'
                 AND j.source_id IN ({placeholders})
               ORDER BY CASE WHEN j.status='RETRY_PENDING' THEN 0 ELSE 1 END,
                        j.id
               LIMIT ?
               """,
               (*source_ids,limit),
            ).fetchall()
            for row in rows:
               self.connection.execute(
                   """
                   UPDATE hash_jobs SET status='HASHING',attempts=attempts+1,
                       run_id=?,started_at=CURRENT_TIMESTAMP,finished_at=NULL,
                       bytes_hashed=0,error='',updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status IN ('HASH_PENDING','RETRY_PENDING')
                   """,
                   (run_id,row["id"]),
               )
        return [dict(row) for row in rows]

    def finish_hash_job(
        self, *, job: dict, run_id: int, status: str, started_at: str,
        finished_at: str, bytes_read: int, sha256: str | None = None,
        error: str = "",
    ) -> str:
        allowed = {
            "HASHED", "RETRY_PENDING", "SOURCE_UNAVAILABLE",
            "FILE_CHANGED", "HASH_FAILED",
        }
        if status not in allowed:
            raise ValueError(f"Unsupported hash-job status {status}.")
        final_status = status
        content_id = None
        with self.connection:
            current = self.connection.execute(
               """
               SELECT size_bytes,modified_ns,is_present,sha256
               FROM file_locations WHERE source_id=? AND path=?
               """,
               (job["source_id"],job["path"]),
            ).fetchone()
            if final_status == "HASHED" and (
               sha256 is None or bytes_read != job["size_bytes"]
               or current is None or not current["is_present"]
               or current["size_bytes"] != job["size_bytes"]
               or current["modified_ns"] != job["modified_ns"]
            ):
               final_status = "FILE_CHANGED"
               sha256 = None
               error = error or "Location fingerprint changed before hash commit."
            if final_status == "HASHED":
               self.connection.execute(
                   """
                   INSERT INTO file_contents(sha256,size_bytes,file_kind)
                   VALUES(?,?,'ols')
                   ON CONFLICT(sha256,size_bytes,file_kind) DO NOTHING
                   """,
                   (sha256,job["size_bytes"]),
               )
               content_row = self.connection.execute(
                   """
                   SELECT id FROM file_contents
                   WHERE sha256=? AND size_bytes=? AND file_kind='ols'
                   """,
                   (sha256,job["size_bytes"]),
               ).fetchone()
               content_id = int(content_row["id"])
               self.connection.execute(
                   """
                   UPDATE file_locations SET sha256=?,content_id=?,state='hashed',
                       last_seen_at=CURRENT_TIMESTAMP
                   WHERE source_id=? AND path=? AND size_bytes=? AND modified_ns=?
                   """,
                   (
                       sha256,content_id,job["source_id"],job["path"],
                       job["size_bytes"],job["modified_ns"],
                   ),
               )
               self.connection.execute(
                   """
                   UPDATE index_queue SET sha256=?,content_id=?,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE source_id=? AND path=?
                   """,
                   (sha256,content_id,job["source_id"],job["path"]),
               )
            self.connection.execute(
               """
               UPDATE hash_jobs SET status=?,bytes_hashed=?,sha256=?,error=?,
                   finished_at=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND run_id=?
               """,
               (
                   final_status,bytes_read,sha256,error,finished_at,
                   job["id"],run_id,
               ),
            )
            self.connection.execute(
               """
               INSERT INTO hash_attempts(
                   job_id,run_id,source_id,path,filename,size_bytes,modified_ns,
                   sha256,started_at,finished_at,bytes_read,status,error
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               """,
               (
                   job["id"],run_id,job["source_id"],job["path"],job["filename"],
                   job["size_bytes"],job["modified_ns"],sha256,started_at,
                   finished_at,bytes_read,final_status,error,
               ),
            )
        return final_status

    def finish_hash_run(self, run_id: int, *, status: str, report: dict) -> None:
        with self.connection:
            resulting_control = (
                "paused" if status == "PAUSED"
                else "cancelled" if status == "CANCELLED"
                else "idle"
            )
            if "queue" in report:
                report["queue"]["control"] = {
                    "status":resulting_control,
                    "run_id":run_id,
                }
            self.connection.execute(
               """
               UPDATE hash_runs SET status=?,report_json=?,finished_at=CURRENT_TIMESTAMP
               WHERE id=?
               """,
               (status,json.dumps(report,ensure_ascii=False,sort_keys=True),run_id),
            )
            self.connection.execute(
               """
               UPDATE hash_control SET status=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=1 AND run_id=?
               """,
               (
                   "paused" if status == "PAUSED"
                   else "cancelled" if status == "CANCELLED"
                   else "idle",
                   run_id,
               ),
            )

    def hash_queue_report(self, source_ids: list[int] | None = None) -> dict:
        clauses = ["l.file_kind='ols'", "l.is_present=1"]
        params: list[int] = []
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            clauses.append(f"l.source_id IN ({placeholders})")
            params.extend(source_ids)
        where = " AND ".join(clauses)
        global_row = self.connection.execute(
            f"""
            SELECT COUNT(*) AS ols_locations,
                  SUM(CASE WHEN l.sha256 IS NOT NULL THEN 1 ELSE 0 END) AS hashed,
                  SUM(CASE WHEN l.sha256 IS NULL THEN 1 ELSE 0 END) AS pending,
                  COALESCE(SUM(l.size_bytes),0) AS physical_bytes,
                  COALESCE(SUM(CASE WHEN l.sha256 IS NOT NULL THEN l.size_bytes ELSE 0 END),0)
                      AS hashed_physical_bytes,
                  COALESCE(SUM(CASE WHEN l.sha256 IS NULL THEN l.size_bytes ELSE 0 END),0)
                      AS pending_physical_bytes
            FROM file_locations l WHERE {where}
            """,
            params,
        ).fetchone()
        counts = {
            row["status"]: int(row["count"])
            for row in self.connection.execute(
               f"""
               SELECT j.status,COUNT(*) AS count
               FROM hash_jobs j JOIN file_locations l
                 ON l.source_id=j.source_id AND l.path=j.path
               WHERE {where} GROUP BY j.status
               """,
               params,
            )
        }
        by_source = [
            dict(row)
            for row in self.connection.execute(
               f"""
               SELECT j.source_id,r.name AS source_name,s.path AS source_path,
                      COUNT(*) AS ols_locations,
                      SUM(CASE WHEN l.sha256 IS NOT NULL THEN 1 ELSE 0 END) AS hashed,
                      SUM(CASE WHEN l.sha256 IS NULL THEN 1 ELSE 0 END) AS pending,
                      COALESCE(SUM(l.size_bytes),0) AS physical_bytes,
                      COALESCE(SUM(CASE WHEN l.sha256 IS NOT NULL THEN l.size_bytes ELSE 0 END),0)
                          AS hashed_physical_bytes,
                      COALESCE(SUM(CASE WHEN l.sha256 IS NULL THEN l.size_bytes ELSE 0 END),0)
                          AS pending_physical_bytes
               FROM file_locations l
               JOIN registered_sources s ON s.id=l.source_id
               LEFT JOIN source_root_slots r ON r.registered_source_id=s.id
               LEFT JOIN hash_jobs j ON j.source_id=l.source_id AND j.path=l.path
               WHERE {where} GROUP BY j.source_id,r.name,s.path
               ORDER BY j.source_id
               """,
               params,
            )
        ]
        for source in by_source:
            recent = self.connection.execute(
                """
                SELECT COALESCE(SUM(bytes_read),0) AS bytes_read,
                       COALESCE(SUM(
                           (julianday(finished_at)-julianday(started_at))*86400.0
                       ),0) AS seconds
                FROM (
                    SELECT bytes_read,started_at,finished_at FROM hash_attempts
                    WHERE source_id=? AND status='HASHED'
                    ORDER BY id DESC LIMIT 100
                )
                """,
                (source["source_id"],),
            ).fetchone()
            source["mib_per_second"] = (
                round(
                    recent["bytes_read"]/(1024*1024)/recent["seconds"],3
                ) if recent["seconds"] else 0
            )
        dedup = self.source_dedup_summary()
        return {
            "queue_statuses":counts,
            "totals":dict(global_row),
            "by_source":by_source,
            "deduplication":dedup,
            "control":self.hash_control_status(),
        }

    def hash_run_summary(self, run_id: int) -> dict | None:
        row = self.connection.execute(
            """
            SELECT id,status,worker_count,chunk_size,report_json,started_at,finished_at
            FROM hash_runs WHERE id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["report"] = json.loads(result.pop("report_json"))
        return result

    def retry_failed_hash_jobs(self, source_ids: list[int]) -> int:
        if not source_ids:
            return 0
        placeholders = ",".join("?" for _ in source_ids)
        with self.connection:
            cursor = self.connection.execute(
               f"""
               UPDATE hash_jobs SET status='RETRY_PENDING',error='',
                   updated_at=CURRENT_TIMESTAMP
               WHERE source_id IN ({placeholders})
                 AND status IN ('HASH_FAILED','SOURCE_UNAVAILABLE','FILE_CHANGED')
                 AND EXISTS (
                     SELECT 1 FROM file_locations l
                     WHERE l.source_id=hash_jobs.source_id AND l.path=hash_jobs.path
                       AND l.is_present=1 AND l.sha256 IS NULL
                 )
               """,
               source_ids,
            )
        return cursor.rowcount

    def find_processed_file_content(
            self, *, sha256: str, size_bytes: int, parser_name: str,
            parser_version: str, application_version: str,
    ) -> dict | None:
            row = self.connection.execute(
                """
                SELECT c.id AS content_id,c.sha256,c.size_bytes,c.file_kind,p.project_id,
                       p.processing_status
                FROM file_contents c JOIN file_processing p ON p.content_id=c.id
                WHERE c.sha256=? AND c.size_bytes=? AND c.file_kind='ols'
                  AND p.parser_name=? AND p.parser_version=? AND p.application_version=?
                  AND p.project_id IS NOT NULL
                """,
                (sha256,size_bytes,parser_name,parser_version,application_version),
            ).fetchone()
            return dict(row) if row is not None else None

    def queue_missing_parser_fingerprint(
            self, *, source_id: int, parser_name: str, parser_version: str,
            application_version: str, limit: int,
    ) -> int:
            if not 1 <= limit <= 10000:
                raise ValueError("Parser reprocessing batch must be between 1 and 10000.")
            candidates = self.connection.execute(
                """
                SELECT l.id,l.path,l.size_bytes,l.modified_ns
                FROM file_locations l
                WHERE l.source_id=? AND l.file_kind='ols' AND l.is_present=1
                  AND l.content_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM file_processing p
                      WHERE p.content_id=l.content_id AND p.parser_name=?
                        AND p.parser_version=? AND p.application_version=?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM index_queue q
                      WHERE q.source_id=l.source_id AND q.path=l.path
                        AND q.status IN
                          ('pending','interrupted','in_progress','retryable','failed')
                  )
                  AND l.id=(
                      SELECT MIN(l2.id) FROM file_locations l2
                      WHERE l2.source_id=l.source_id AND l2.file_kind='ols'
                        AND l2.is_present=1
                        AND l2.content_id=l.content_id
                  )
                ORDER BY l.id LIMIT ?
                """,
                (source_id,parser_name,parser_version,application_version,limit),
            ).fetchall()
            with self.connection:
                for row in candidates:
                    self.connection.execute(
                        """
                        INSERT INTO index_queue(
                            source_id,path,size_bytes,modified_ns,status,sha256,content_id,message
                        ) VALUES(?,?,?,?, 'pending',NULL,NULL,?)
                        ON CONFLICT(source_id,path) DO UPDATE SET
                            size_bytes=excluded.size_bytes,
                            modified_ns=excluded.modified_ns,
                            status='pending',sha256=NULL,content_id=NULL,
                            message=excluded.message,updated_at=CURRENT_TIMESTAMP
                        """,
                        (
                            source_id,row["path"],row["size_bytes"],row["modified_ns"],
                            "Reprocessing required: parser/application fingerprint is not recorded.",
                        ),
                    )
            return len(candidates)

    def project_id_for_source_file(
            self, path: str, *, sha256: str | None = None
    ) -> int | None:
            row = self.connection.execute(
                """
                SELECT p.id FROM projects p JOIN source_files s ON s.id=p.source_id
                WHERE s.path=? AND (? IS NULL OR s.sha256=?)
                """,
                (path, sha256, sha256),
            ).fetchone()
            return int(row["id"]) if row else None

    def attach_location_content(
            self, *, source_id: int, path: str, sha256: str, content_id: int,
            state: str,
    ) -> None:
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE file_locations SET sha256=?,content_id=?,state=?,is_present=1,
                        last_seen_at=CURRENT_TIMESTAMP
                    WHERE source_id=? AND path=?
                    """,
                    (sha256,content_id,state,source_id,path),
                )
                self.connection.execute(
                    """
                    UPDATE index_queue SET sha256=?,content_id=?,status=?,message='',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE source_id=? AND path=?
                    """,
                    (sha256,content_id,state,source_id,path),
                )

    def source_dedup_summary(self) -> dict:
            return {
                "unique_ols_contents": self.connection.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT sha256,size_bytes FROM file_locations
                        WHERE file_kind='ols' AND sha256 IS NOT NULL AND is_present=1
                        GROUP BY sha256,size_bytes
                    )
                    """
                ).fetchone()[0],
                "unhashed_ols_locations": self.connection.execute(
                    """
                    SELECT COUNT(*) FROM file_locations
                    WHERE file_kind='ols' AND sha256 IS NULL AND is_present=1
                    """
                ).fetchone()[0],
                "hashed_ols_locations": self.connection.execute(
                    """
                    SELECT COUNT(*) FROM file_locations
                    WHERE file_kind='ols' AND sha256 IS NOT NULL AND is_present=1
                    """
                ).fetchone()[0],
                "exact_duplicate_locations": self.connection.execute(
                    """
                    SELECT COALESCE(SUM(locations-1),0) FROM (
                        SELECT COUNT(*) locations FROM file_locations
                        WHERE file_kind='ols' AND sha256 IS NOT NULL AND is_present=1
                        GROUP BY sha256,size_bytes HAVING COUNT(*)>1
                    )
                    """
                ).fetchone()[0],
                "contents_in_multiple_locations": self.connection.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT sha256,size_bytes FROM file_locations
                        WHERE file_kind='ols' AND sha256 IS NOT NULL AND is_present=1
                        GROUP BY sha256,size_bytes HAVING COUNT(DISTINCT source_id)>1
                    )
                    """
                ).fetchone()[0],
                "unique_ols_bytes": self.connection.execute(
                    """
                    SELECT COALESCE(SUM(size_bytes),0) FROM (
                        SELECT DISTINCT sha256,size_bytes FROM file_locations
                        WHERE file_kind='ols' AND sha256 IS NOT NULL AND is_present=1
                    )
                    """
                ).fetchone()[0],
                "physical_ols_bytes": self.connection.execute(
                    """
                    SELECT COALESCE(SUM(size_bytes),0) FROM file_locations
                    WHERE file_kind='ols' AND is_present=1
                    """
                ).fetchone()[0],
                "partially_processed_projects": self.connection.execute(
                    """
                    SELECT COUNT(*) FROM project_diagnostics
                    WHERE processing_status='partially_processed'
                    """
                ).fetchone()[0],
            }

    def list_duplicate_content_locations(
        self, *, limit: int = 100, source_ids: list[int] | None = None
    ) -> list[dict]:
            if not 1 <= limit <= 1000:
                raise ValueError("Duplicate location limit must be between 1 and 1000.")
            if source_ids is not None and not source_ids:
                return []
            source_clause = ""
            source_params: list[int] = []
            if source_ids is not None:
                source_clause = (
                    " AND source_id IN ("
                    + ",".join("?" for _ in source_ids)
                    + ")"
                )
                source_params.extend(source_ids)
            hashes = self.connection.execute(
                f"""
                SELECT sha256,size_bytes,COUNT(*) AS locations
                FROM file_locations
                WHERE file_kind='ols' AND sha256 IS NOT NULL AND is_present=1
                  {source_clause}
                GROUP BY sha256,size_bytes HAVING COUNT(*)>1
                ORDER BY locations DESC,sha256 LIMIT ?
                """,
                (*source_params,limit),
            ).fetchall()
            groups = []
            for item in hashes:
                locations = self.connection.execute(
                    f"""
                    SELECT r.name AS source_root,s.path AS source_root_path,l.path,
                           l.size_bytes,l.modified_ns
                    FROM file_locations l
                    JOIN registered_sources s ON s.id=l.source_id
                    LEFT JOIN source_root_slots r ON r.registered_source_id=s.id
                    WHERE l.sha256=? AND l.size_bytes=? AND l.is_present=1
                      {source_clause.replace('source_id','l.source_id')}
                    ORDER BY r.id,s.path,l.path
                    """,
                    (
                        item["sha256"],item["size_bytes"],*source_params
                    ),
                ).fetchall()
                groups.append({
                    "sha256": item["sha256"],
                    "size_bytes": item["size_bytes"],
                    "locations_count":int(item["locations"]),
                    "physical_bytes":int(item["size_bytes"])*int(item["locations"]),
                    "unique_bytes":int(item["size_bytes"]),
                    "duplicate_bytes_saved_from_reprocessing":(
                        int(item["size_bytes"])*(int(item["locations"])-1)
                    ),
                    "locations": [dict(location) for location in locations],
                })
            return groups

    def list_sources(self) -> list[dict]:
        results = []
        rows = self.connection.execute(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM index_queue q
                    WHERE q.source_id=s.id AND q.status IN
                        ('pending','interrupted','in_progress','retryable')) AS queued_files,
                   (SELECT COUNT(*) FROM index_queue q
                    WHERE q.source_id=s.id AND q.status='failed') AS failed_files,
                   (SELECT report_json FROM source_scan_runs r
                    WHERE r.source_id=s.id ORDER BY r.id DESC LIMIT 1) AS latest_scan_json
            FROM registered_sources s ORDER BY s.id
            """
        ).fetchall()
        for row in rows:
            result = dict(row)
            scan_json = result.pop("latest_scan_json")
            result["latest_scan"] = json.loads(scan_json) if scan_json else None
            results.append(result)
        return results

    def get_source(self, source_id: int) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM registered_sources WHERE id=?", (source_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def update_source_status(
        self, source_id: int, *, status: str, error: str = ""
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE registered_sources
                SET status=?,last_error=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (status, error, source_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown registered source ID {source_id}.")
            self.connection.execute(
                """
                UPDATE source_root_slots SET status=?,updated_at=CURRENT_TIMESTAMP
                WHERE registered_source_id=?
                """,
                (status, source_id),
            )

    def update_source_path(
        self, source_id: int, *, path: str, status: str, error: str = ""
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE registered_sources SET path=?,status=?,last_error=?,
                    last_scan_at=NULL,found_files=0,total_size_bytes=0,
                    indexed_files=0,new_files=0,changed_files=0,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (path, status, error, source_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown registered source ID {source_id}.")
            self.connection.execute(
                """
                UPDATE source_root_slots SET status=?,updated_at=CURRENT_TIMESTAMP
                WHERE registered_source_id=?
                """,
                (status, source_id),
            )

    def relocate_source_root(
        self, source_id: int, *, old_path: str, new_path: str
    ) -> dict[str, int]:
        return self.relocate_source_roots(
            {source_id:{"old_path":old_path,"new_path":new_path}}
        )[source_id]

    def relocate_source_roots(
        self, roots: dict[int, dict[str, str]]
    ) -> dict[int, dict[str, int]]:
        if not roots:
            raise ValueError("At least one source root must be provided.")
        with self.connection:
            active_worker = self.connection.execute(
                """
                SELECT status FROM hash_worker_state WHERE id=1
                """
            ).fetchone()
            if active_worker and active_worker["status"] in {"running","paused"}:
                raise RuntimeError("Cannot relocate source roots while a hash worker is active.")
            source_rows = self.connection.execute(
                "SELECT id,path FROM registered_sources"
            ).fetchall()
            source_paths = {int(row["id"]):row["path"] for row in source_rows}
            new_roots: set[str] = set()
            for source_id,(mapping) in roots.items():
                old_root = mapping["old_path"].rstrip("\\/")
                new_root = mapping["new_path"].rstrip("\\/")
                if not old_root or not new_root or old_root == new_root:
                    raise ValueError(
                        f"Source {source_id} relocation requires distinct non-empty roots."
                    )
                if source_id not in source_paths:
                    raise ValueError(f"Unknown registered source ID {source_id}.")
                if source_paths[source_id].rstrip("\\/") != old_root:
                    raise RuntimeError(
                        f"Source {source_id} path changed: expected {old_root!r}, "
                        f"found {source_paths[source_id]!r}."
                    )
                if new_root.casefold() in new_roots:
                    raise ValueError("Two source slots cannot use the same local root.")
                new_roots.add(new_root.casefold())

            results: dict[int,dict[str,int]] = {}
            for source_id,mapping in roots.items():
                old_root = mapping["old_path"].rstrip("\\/")
                new_root = mapping["new_path"].rstrip("\\/")
                result: dict[str,int] = {}
                for table in (
                    "file_locations","index_queue","hash_jobs","hash_attempts",
                    "source_scan_directories","source_scan_errors",
                ):
                    cursor = self.connection.execute(
                        f"""
                        UPDATE {table} SET path=? || substr(path,length(?)+1)
                        WHERE source_id=? AND
                            (path=? OR substr(path,1,length(?)+1)=? || '\\')
                        """,
                        (new_root,old_root,source_id,old_root,old_root,old_root),
                    )
                    result[table] = cursor.rowcount
                cursor = self.connection.execute(
                    """
                    UPDATE hash_runs SET report_json=replace(report_json,?,?)
                    WHERE report_json LIKE ?
                    """,
                    (old_root,new_root,"%" + old_root + "%"),
                )
                result["hash_runs"] = cursor.rowcount
                cursor = self.connection.execute(
                    """
                    UPDATE source_scan_runs SET report_json=replace(report_json,?,?)
                    WHERE source_id=? AND report_json LIKE ?
                    """,
                    (old_root,new_root,source_id,"%" + old_root + "%"),
                )
                result["source_scan_runs"] = cursor.rowcount
                cursor = self.connection.execute(
                    """
                    UPDATE source_files SET path=? || substr(path,length(?)+1)
                    WHERE path=? OR substr(path,1,length(?)+1)=? || '\\'
                    """,
                    (new_root,old_root,old_root,old_root,old_root),
                )
                result["source_files"] = cursor.rowcount
                self.connection.execute(
                    """
                    UPDATE registered_sources SET path=?,status='available',
                        last_error='',updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (new_root,source_id),
                )
                self.connection.execute(
                    """
                    UPDATE source_root_slots SET status='available',
                        updated_at=CURRENT_TIMESTAMP WHERE registered_source_id=?
                    """,
                    (source_id,),
                )
                results[source_id] = result
        return results

    def record_training_candidate(
        self, *, project_id: int, version_id: int | None, provenance: str,
        validation_status: str, notes: str = "",
    ) -> int:
        if not provenance.strip():
            raise ValueError("Training candidate provenance is required.")
        if validation_status not in (
            "NOT_VALIDATED", "INCONCLUSIVE", "INDEPENDENTLY_VALIDATED"
        ):
            raise ValueError("Unsupported training candidate validation status.")
        project = self.connection.execute(
            "SELECT 1 FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise ValueError(f"No indexed project found with id {project_id}.")
        if version_id is not None:
            version = self.connection.execute(
                "SELECT 1 FROM project_versions WHERE id=? AND project_id=?",
                (version_id, project_id),
            ).fetchone()
            if version is None:
                raise ValueError("Version does not belong to the specified project.")
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO training_candidates(
                    project_id,version_id,provenance,validation_status,notes
                ) VALUES(?,?,?,?,?)
                """,
                (project_id, version_id, provenance.strip(), validation_status, notes),
            )
        return int(cursor.lastrowid)

    def list_training_candidates(self, *, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("Training candidate limit must be between 1 and 1000.")
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT c.*,s.path,v.name AS version_name,v.slot_index
                FROM training_candidates c
                JOIN projects p ON p.id=c.project_id
                JOIN source_files s ON s.id=p.source_id
                LEFT JOIN project_versions v ON v.id=c.version_id
                ORDER BY c.id DESC LIMIT ?
                """,
                (limit,),
            )
        ]

    def queue_source_file(
        self, *, source_id: int, path: str, size_bytes: int, modified_ns: int,
        force_recheck: bool = False,
    ) -> str:
        indexed = self.connection.execute(
            "SELECT sha256,size_bytes,modified_ns FROM source_files WHERE path=?", (path,)
        ).fetchone()
        queued = self.connection.execute(
            "SELECT size_bytes,modified_ns,status FROM index_queue "
            "WHERE source_id=? AND path=?",
            (source_id, path),
        ).fetchone()
        if (
            not force_recheck
            and queued is not None
            and queued["status"] == "duplicate_content_skipped"
            and queued["size_bytes"] == size_bytes
            and queued["modified_ns"] == modified_ns
        ):
            return "duplicate_content_skipped"
        if (
            not force_recheck
            and indexed is not None
            and indexed["size_bytes"] == size_bytes
            and indexed["modified_ns"] == modified_ns
            and (queued is None or queued["status"] in (
                "imported", "partial", "skipped_unchanged", "fingerprint_match_unverified",
                "duplicate_content_skipped"
            ))
        ):
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO index_queue(
                        source_id,path,size_bytes,modified_ns,status,message
                    ) VALUES(?,?,?,?, 'fingerprint_match_unverified',?)
                    ON CONFLICT(source_id,path) DO UPDATE SET
                        status='fingerprint_match_unverified',
                        sha256=NULL,
                        size_bytes=excluded.size_bytes,modified_ns=excluded.modified_ns,
                        message=excluded.message,updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        source_id, path, size_bytes, modified_ns,
                        "Size and modification time match an indexed record; "
                        "current content was not rehashed.",
                    ),
                )
            return "fingerprint_match_unverified"
        if queued is not None and queued["status"] == "in_progress":
            return "in_progress"
        classification = (
            "new" if indexed is None else
            "stat_changed" if indexed["size_bytes"] != size_bytes
            or indexed["modified_ns"] != modified_ns else "hash_unverified"
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO index_queue(source_id,path,size_bytes,modified_ns,status,message)
                VALUES(?,?,?,?, 'pending',?)
                ON CONFLICT(source_id,path) DO UPDATE SET
                    size_bytes=excluded.size_bytes,
                    modified_ns=excluded.modified_ns,
                    status=CASE WHEN index_queue.status='in_progress'
                                THEN 'in_progress' ELSE 'pending' END,
                    sha256=NULL,
                    message='Awaiting content hash and parser verification.',
                    content_id=NULL,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (source_id, path, size_bytes, modified_ns, classification),
            )
        return classification

    def record_unavailable_source_file(
        self, *, source_id: int, path: str, message: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO index_queue(
                    source_id,path,size_bytes,modified_ns,status,message
                ) VALUES(?,?, -1, -1,'retryable',?)
                ON CONFLICT(source_id,path) DO UPDATE SET
                    status='retryable',message=excluded.message,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (source_id, path, message),
            )

    def finish_source_scan(
        self, *, source_id: int, status: str, found_files: int,
        total_size_bytes: int, indexed_files: int, new_files: int,
        changed_files: int, error: str = "",
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE registered_sources SET status=?,
                    last_scan_at=CASE WHEN ?='available' THEN CURRENT_TIMESTAMP
                                      ELSE last_scan_at END,
                    last_attempt_at=CURRENT_TIMESTAMP,
                    found_files=?,total_size_bytes=?,indexed_files=?,new_files=?,
                    changed_files=?,last_error=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (status, status, found_files, total_size_bytes, indexed_files, new_files,
                 changed_files, error, source_id),
            )
            self.connection.execute(
                """
                UPDATE source_root_slots SET status=?,updated_at=CURRENT_TIMESTAMP
                WHERE registered_source_id=?
                """,
                (status, source_id),
            )

    def store_source_scan_run(self, source_id: int, status: str, report: dict) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO source_scan_runs(source_id,status,report_json) VALUES(?,?,?)",
                (source_id, status, json.dumps(report, ensure_ascii=False, sort_keys=True)),
            )
        return int(cursor.lastrowid)

    def list_source_scan_runs(self, source_id: int, *, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("Source scan history limit must be between 1 and 1000.")
        return [
            {
                "id": row["id"],
                "source_id": row["source_id"],
                "status": row["status"],
                "report": json.loads(row["report_json"]),
                "created_at": row["created_at"],
            }
            for row in self.connection.execute(
                """
                SELECT id,source_id,status,report_json,created_at
                FROM source_scan_runs WHERE source_id=? ORDER BY id DESC LIMIT ?
                """,
                (source_id, limit),
            )
        ]

    def list_index_queue(
        self, source_id: int, *, statuses: tuple[str, ...] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        if not 1 <= limit <= 10000:
            raise ValueError("Queue limit must be between 1 and 10000.")
        clauses = ["source_id=?"]
        params: list[object] = [source_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        params.append(limit)
        return [
            dict(row)
            for row in self.connection.execute(
                f"SELECT * FROM index_queue WHERE {' AND '.join(clauses)} "
                "ORDER BY id LIMIT ?",
                params,
            )
        ]

    def count_index_queue(
        self, source_id: int, *, statuses: tuple[str, ...] | None = None
    ) -> int:
        clauses = ["source_id=?"]
        params: list[object] = [source_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        row = self.connection.execute(
            f"SELECT COUNT(*) FROM index_queue WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        return int(row[0])

    def update_source_indexed_count(self, source_id: int) -> int:
        count = self.connection.execute(
            """
            SELECT COUNT(*) FROM index_queue q
            JOIN source_files f ON f.path=q.path
            WHERE q.source_id=? AND q.status IN
                ('imported','partial','skipped_unchanged','fingerprint_match_unverified',
                 'duplicate_content_skipped')
            """,
            (source_id,),
        ).fetchone()[0]
        with self.connection:
            self.connection.execute(
                """
                UPDATE registered_sources SET indexed_files=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (count, source_id),
            )
        return int(count)

    def claim_source_queue_items(self, source_id: int, limit: int) -> list[dict]:
        if not 1 <= limit <= 10000:
            raise ValueError("Batch size must be between 1 and 10000.")
        with self.connection:
            self.connection.execute(
                """
                UPDATE index_queue SET status='interrupted',
                    message='Recovered an interrupted indexing attempt.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE source_id=? AND status='in_progress'
                """,
                (source_id,),
            )
            rows = self.connection.execute(
                """
                SELECT id,path,size_bytes,modified_ns,sha256,content_id
                FROM index_queue
                WHERE source_id=? AND status IN
                    ('pending','interrupted','failed','retryable')
                ORDER BY id LIMIT ?
                """,
                (source_id, limit),
            ).fetchall()
            for row in rows:
                self.connection.execute(
                    """
                    UPDATE index_queue SET status='in_progress',attempts=attempts+1,
                        message='',updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (row["id"],),
                )
            return [dict(row) for row in rows]

    def interrupt_active_source_queue(self, source_id: int, message: str) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE index_queue SET status='interrupted',message=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE source_id=? AND status='in_progress'
                """,
                (message, source_id),
            )
        return cursor.rowcount

    def finish_source_queue_item(
        self, queue_id: int, *, status: str, sha256: str = "", message: str = "",
        content_id: int | None = None,
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE index_queue SET status=?,sha256=?,message=?,
                    content_id=COALESCE(?,content_id),
                    updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (status, sha256 or None, message, content_id, queue_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown index queue item {queue_id}.")

    def start_job(self, source_path: str) -> int:
        with self.connection:
            self.connection.execute(
                """
                UPDATE import_jobs
                SET status='interrupted',
                    message='A later import attempt resumed this source.',
                    completed_at=CURRENT_TIMESTAMP
                WHERE source_path=? AND status='running'
                """,
                (source_path,),
            )
            cursor = self.connection.execute(
                "INSERT INTO import_jobs(source_path, status) VALUES (?, 'running')",
                (source_path,),
            )
        return int(cursor.lastrowid)

    def job_hash(self, job_id: int, sha256: str) -> None:
        self.connection.execute(
            "UPDATE import_jobs SET source_sha256=? WHERE id=?", (sha256, job_id)
        )
        self.connection.commit()

    def finish_job(self, job_id: int, status: str, message: str = "") -> None:
        self.connection.execute(
            """
            UPDATE import_jobs SET status=?, message=?, completed_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (status, message, job_id),
        )
        self.connection.commit()

    def record_issue(
        self, job_id: int, code: str, message: str, severity: str = "warning"
    ) -> None:
        self.connection.execute(
            "INSERT INTO import_errors(job_id, code, severity, message) "
            "VALUES (?, ?, ?, ?)",
            (job_id, code, severity, message),
        )

    def is_unchanged(
        self,
        source_path: str,
        sha256: str,
        size: int,
        modified_ns: int,
        parser_name: str,
        parser_version: str,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT s.sha256, s.size_bytes, s.modified_ns,
                   p.parser_name, p.parser_version, d.application_version
            FROM source_files s
            JOIN projects p ON p.source_id=s.id
            LEFT JOIN project_diagnostics d ON d.project_id=p.id
            WHERE s.path=?
            """,
            (source_path,),
        ).fetchone()
        return bool(
            row
            and row["sha256"] == sha256
            and row["size_bytes"] == size
            and row["modified_ns"] == modified_ns
            and row["parser_name"] == parser_name
            and row["parser_version"] == parser_version
            and row["application_version"] == APPLICATION_VERSION
        )

    def store_project(
        self,
        *,
        source_path: str,
        sha256: str,
        size: int,
        modified_ns: int,
        snapshot: ProjectSnapshot,
        job_id: int,
        issues: tuple[ReadIssue, ...] = (),
    ) -> None:
        conn = self.connection
        with conn:
            conn.execute(
                """
                INSERT INTO source_files(path, sha256, size_bytes, modified_ns)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    sha256=excluded.sha256,
                    size_bytes=excluded.size_bytes,
                    modified_ns=excluded.modified_ns,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (source_path, sha256, size, modified_ns),
            )
            source = conn.execute(
                "SELECT id FROM source_files WHERE path=?", (source_path,)
            ).fetchone()
            source_id = source["id"]
            conn.execute(
                """
                INSERT INTO projects(
                    source_id, parser_name, parser_version, format_version,
                    is_complete, raw_counts_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    parser_name=excluded.parser_name,
                    parser_version=excluded.parser_version,
                    format_version=excluded.format_version,
                    is_complete=excluded.is_complete,
                    raw_counts_json=excluded.raw_counts_json,
                    imported_at=CURRENT_TIMESTAMP
                """,
                (
                    source_id,
                    snapshot.parser_name,
                    snapshot.parser_version,
                    snapshot.format_version,
                    int(not (issues or snapshot.issues)),
                    json.dumps(snapshot.raw_counts, sort_keys=True),
                ),
            )
            project = conn.execute(
                "SELECT id FROM projects WHERE source_id=?", (source_id,)
            ).fetchone()
            project_id = project["id"]
            conn.execute("DELETE FROM project_metadata WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM project_version_slots WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM project_versions WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM project_folders WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM cal_blocks WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM map_definitions WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM project_skipped_elements WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM project_diagnostics WHERE project_id=?", (project_id,))

            for field, value in snapshot.metadata.items():
                conn.execute(
                    "INSERT INTO project_metadata(project_id, field, value, status) "
                    "VALUES (?, ?, ?, ?)",
                    (project_id, field, value, "direct" if value else "absent"),
                )
            version_ids: dict[int, int] = {}
            for version in snapshot.versions:
                cursor = conn.execute(
                    """
                    INSERT INTO project_versions(
                        project_id, name, description, slot_index, declared_slot_count,
                        blob_offset, blob_size, epk, epk_offset, status, parent_id, is_root
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        version.name,
                        version.description,
                        version.slot_index,
                        version.declared_slot_count,
                        version.blob_offset,
                        version.blob_size,
                        version.epk,
                        version.epk_offset,
                        "named_record_binary_unverified",
                        version.parent_id,
                        int(version.is_root),
                    ),
                )
                version_ids[version.slot_index] = int(cursor.lastrowid)
            for slot in snapshot.version_slots:
                conn.execute(
                    """
                    INSERT INTO project_version_slots(
                        project_id, version_id, slot_index, blob_offset, blob_size,
                        marker_hex, raw_sha256, payload_sha256, payload_size,
                        binary_status, name_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        version_ids.get(slot.slot_index),
                        slot.slot_index,
                        slot.blob_offset,
                        slot.blob_size,
                        slot.marker_hex,
                        slot.raw_sha256,
                        slot.payload_sha256,
                        slot.payload_size,
                        slot.binary_status,
                        slot.name_status,
                    ),
                )
            for folder in snapshot.folders:
                conn.execute(
                    """
                    INSERT INTO project_folders(
                        project_id, folder_index, name, description, status
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        folder.index,
                        folder.name,
                        folder.description,
                        "unverified",
                    ),
                )
            for cal_block in snapshot.cal_blocks:
                conn.execute(
                    """
                    INSERT INTO cal_blocks(
                        project_id, offset, cas_id, epk, size_bytes, status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        cal_block.offset,
                        cal_block.cas_id,
                        cal_block.epk,
                        cal_block.size_bytes,
                        "unverified",
                    ),
                )
            for ordinal, map_item in enumerate(snapshot.maps):
                map_status = "incomplete" if not map_item.name else "unverified_definition"
                cursor = conn.execute(
                    """
                    INSERT INTO map_definitions(
                        project_id, ordinal, name, description, map_type, data_type,
                        columns_count, rows_count, data_offset, unit, factor,
                        conversion_offset, folder_id, type_code,
                        version_relation_status, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        ordinal,
                        map_item.name,
                        map_item.description,
                        map_item.map_type,
                        map_item.data_type,
                        map_item.columns,
                        map_item.rows,
                        map_item.data_offset,
                        map_item.unit,
                        map_item.factor,
                        map_item.conversion_offset,
                        map_item.folder_id,
                        map_item.type_code,
                        map_item.version_relation_status,
                        map_status,
                    ),
                )
                map_id = cursor.lastrowid
                for axis in map_item.axes:
                    axis_status = (
                        "incomplete"
                        if not axis.unit or not axis.identifier
                        else "unverified_definition"
                    )
                    conn.execute(
                        """
                        INSERT INTO map_axes(
                            map_id, role, points, address, data_type, unit, factor,
                            conversion_offset, identifier, description, data_source, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            map_id,
                            axis.role,
                            axis.points,
                            axis.address,
                            axis.data_type,
                            axis.unit,
                            axis.factor,
                            axis.offset,
                            axis.identifier,
                            axis.description,
                            axis.data_source,
                            axis_status,
                        ),
                    )
            for element in snapshot.skipped_elements:
                conn.execute(
                    """
                    INSERT INTO project_skipped_elements(
                        project_id, ordinal, element_kind, description,
                        raw_metadata_json, reason
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        element.ordinal,
                        element.element_kind,
                        element.description,
                        element.raw_metadata_json,
                        element.reason,
                    ),
                )
            for issue in (*snapshot.issues, *issues):
                self.record_issue(
                    job_id,
                    issue.code,
                    issue.message,
                    issue.severity,
                )
            stored_counts = {
                "named_version_records": conn.execute(
                    "SELECT COUNT(*) FROM project_versions WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                "enumerated_binary_slots": conn.execute(
                    "SELECT COUNT(*) FROM project_version_slots WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                "available_binary_slots": conn.execute(
                    """
                    SELECT COUNT(*) FROM project_version_slots
                    WHERE project_id=? AND binary_status='present_nonempty_unverified'
                    """,
                    (project_id,),
                ).fetchone()[0],
                "unlabeled_binary_slots": conn.execute(
                    """
                    SELECT COUNT(*) FROM project_version_slots
                    WHERE project_id=? AND name_status='unresolved'
                    """,
                    (project_id,),
                ).fetchone()[0],
                "map_definitions": conn.execute(
                    "SELECT COUNT(*) FROM map_definitions m WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                "map_axes": conn.execute(
                    """
                    SELECT COUNT(*) FROM map_axes a
                    JOIN map_definitions m ON m.id=a.map_id
                    WHERE m.project_id=?
                    """,
                    (project_id,),
                ).fetchone()[0],
                "folders": conn.execute(
                    "SELECT COUNT(*) FROM project_folders WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                "cal_blocks": conn.execute(
                    "SELECT COUNT(*) FROM cal_blocks WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                "skipped_elements": conn.execute(
                    "SELECT COUNT(*) FROM project_skipped_elements WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
            }
            metrics: dict[str, int | str | bool | None] = {
                **snapshot.raw_counts,
                "adapter_map_definitions_received": len(snapshot.maps),
                "adapter_axes_received": sum(len(item.axes) for item in snapshot.maps),
                **{f"stored_{key}": value for key, value in stored_counts.items()},
                "unsupported_element_count": None,
                "unsupported_element_count_status": "not_reported_by_parser",
            }
            errors = [
                {"code": issue.code, "message": issue.message}
                for issue in (*snapshot.issues, *issues)
                if issue.severity == "error"
            ]
            warnings = [
                {"code": issue.code, "message": issue.message}
                for issue in (*snapshot.issues, *issues)
                if issue.severity != "error"
            ]
            processing_status = (
                "partially_processed" if snapshot.issues or issues else "fully_processed"
            )
            conn.execute(
                """
                INSERT INTO project_diagnostics(
                    project_id, application_version, data_structure_json, metrics_json,
                    processing_status, reference_status, reference_details,
                    errors_json, warnings_json, error_count, warning_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    APPLICATION_VERSION,
                    json.dumps(snapshot.data_structure, ensure_ascii=False, sort_keys=True),
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    processing_status,
                    "not_validated",
                    "No independent WinOLS export has been compared with this project.",
                    json.dumps(errors, ensure_ascii=False),
                    json.dumps(warnings, ensure_ascii=False),
                    len(errors),
                    len(warnings),
                ),
            )

    def summary(self) -> dict[str, int]:
        conn = self.connection
        return {
            "source_files": conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
            "indexed_source_bytes": conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM source_files"
            ).fetchone()[0],
            "projects": conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
            "complete_projects": conn.execute(
                "SELECT COUNT(*) FROM projects WHERE is_complete=1"
            ).fetchone()[0],
            "versions_found": conn.execute("SELECT COUNT(*) FROM project_versions").fetchone()[0],
            "binary_slots_enumerated": conn.execute(
                "SELECT COUNT(*) FROM project_version_slots"
            ).fetchone()[0],
            "binary_slots_available": conn.execute(
                "SELECT COUNT(*) FROM project_version_slots "
                "WHERE binary_status='present_nonempty_unverified'"
            ).fetchone()[0],
            "unlabeled_binary_slots": conn.execute(
                "SELECT COUNT(*) FROM project_version_slots WHERE name_status='unresolved'"
            ).fetchone()[0],
            "projects_needing_reference_validation": conn.execute(
                "SELECT COUNT(*) FROM project_diagnostics "
                "WHERE reference_status!='validated'"
            ).fetchone()[0],
            "folders_found": conn.execute("SELECT COUNT(*) FROM project_folders").fetchone()[0],
            "cal_blocks_found": conn.execute("SELECT COUNT(*) FROM cal_blocks").fetchone()[0],
            "map_definitions": conn.execute("SELECT COUNT(*) FROM map_definitions").fetchone()[0],
            "axes_found": conn.execute("SELECT COUNT(*) FROM map_axes").fetchone()[0],
            "duplicate_file_paths": conn.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT sha256) FROM source_files"
            ).fetchone()[0],
            "latest_failed_files": conn.execute(
                """
                SELECT COUNT(*) FROM import_jobs j
                WHERE j.status='failed' AND NOT EXISTS (
                    SELECT 1 FROM import_jobs newer
                    WHERE newer.source_path=j.source_path AND newer.id>j.id
                )
                """
            ).fetchone()[0],
            "latest_blocked_files": conn.execute(
                """
                SELECT COUNT(*) FROM import_jobs j
                WHERE j.status='blocked' AND NOT EXISTS (
                    SELECT 1 FROM import_jobs newer
                    WHERE newer.source_path=j.source_path AND newer.id>j.id
                )
                """
            ).fetchone()[0],
            "import_errors": conn.execute(
                "SELECT COUNT(*) FROM import_errors WHERE severity='error'"
            ).fetchone()[0],
            "import_warnings": conn.execute(
                "SELECT COUNT(*) FROM import_errors WHERE severity='warning'"
            ).fetchone()[0],
        }

    def list_projects(self, *, query: str = "", limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("Project list limit must be between 1 and 1000.")
        filters = ""
        parameters: list = []
        if query:
            filters = """
                WHERE s.path LIKE ? OR EXISTS (
                    SELECT 1 FROM project_metadata m
                    WHERE m.project_id=p.id
                      AND m.field IN ('make', 'model', 'engine', 'ecu_type',
                                      'sw_number', 'hw_number')
                      AND m.value LIKE ?
                )
            """
            pattern = f"%{query}%"
            parameters.extend((pattern, pattern))
        parameters.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT p.id, s.path, s.sha256, s.size_bytes, p.format_version,
                   p.is_complete, p.parser_name, p.parser_version,
                   (SELECT COUNT(*) FROM project_versions v WHERE v.project_id=p.id)
                       AS version_count,
                   (SELECT COUNT(*) FROM map_definitions m WHERE m.project_id=p.id)
                       AS map_count,
                   d.processing_status, d.reference_status,
                   d.error_count, d.warning_count,
                   CAST(json_extract(d.metrics_json, '$.declared_binary_slots')
                        AS INTEGER) AS declared_binary_slots,
                   CAST(json_extract(d.metrics_json, '$.unlabeled_binary_slots')
                        AS INTEGER) AS unlabeled_binary_slots
            FROM projects p
            JOIN source_files s ON s.id=p.source_id
            LEFT JOIN project_diagnostics d ON d.project_id=p.id
            {filters}
            ORDER BY p.id
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_project(self, project_id: int) -> dict | None:
        project = self.connection.execute(
            """
            SELECT p.id, p.parser_name, p.parser_version, p.format_version,
                   p.is_complete, p.raw_counts_json, p.imported_at,
                   s.path, s.sha256, s.size_bytes, s.modified_ns
            FROM projects p
            JOIN source_files s ON s.id=p.source_id
            WHERE p.id=?
            """,
            (project_id,),
        ).fetchone()
        if project is None:
            return None
        result = dict(project)
        result["metadata"] = {
            row["field"]: {"value": row["value"], "status": row["status"]}
            for row in self.connection.execute(
                "SELECT field, value, status FROM project_metadata "
                "WHERE project_id=? ORDER BY field",
                (project_id,),
            )
        }
        result["versions"] = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT id, name, description, slot_index, declared_slot_count,
                       blob_offset, blob_size, epk, epk_offset, status,
                       parent_id, is_root
                FROM project_versions WHERE project_id=? ORDER BY slot_index
                """,
                (project_id,),
            )
        ]
        result["folders"] = [
            dict(row)
            for row in self.connection.execute(
                "SELECT folder_index, name, description, status "
                "FROM project_folders WHERE project_id=? ORDER BY folder_index",
                (project_id,),
            )
        ]
        result["version_slots"] = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT slot_index, version_id, blob_offset, blob_size, marker_hex,
                       raw_sha256, payload_sha256, payload_size, binary_status,
                       name_status
                FROM project_version_slots WHERE project_id=? ORDER BY slot_index
                """,
                (project_id,),
            )
        ]
        result["cal_blocks"] = [
            dict(row)
            for row in self.connection.execute(
                "SELECT offset, cas_id, epk, size_bytes, status "
                "FROM cal_blocks WHERE project_id=? ORDER BY offset",
                (project_id,),
            )
        ]
        maps = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT id, ordinal, name, description, map_type, data_type,
                       columns_count, rows_count, data_offset, unit, factor,
                       conversion_offset, folder_id, type_code,
                       version_relation_status, status
                FROM map_definitions WHERE project_id=? ORDER BY ordinal
                """,
                (project_id,),
            )
        ]
        for map_item in maps:
            map_item["axes"] = [
                dict(axis)
                for axis in self.connection.execute(
                    """
                    SELECT role, points, address, data_type, unit, factor,
                           conversion_offset, identifier, description,
                           data_source, status
                    FROM map_axes WHERE map_id=? ORDER BY role
                    """,
                    (map_item.pop("id"),),
                )
            ]
        result["maps"] = maps
        result["skipped_elements"] = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT ordinal, element_kind, description, raw_metadata_json, reason
                FROM project_skipped_elements WHERE project_id=? ORDER BY ordinal
                """,
                (project_id,),
            )
        ]
        result["diagnostics"] = self.get_diagnostic(project_id)
        return result

    def get_diagnostic(self, project_id: int) -> dict | None:
        row = self.connection.execute(
            """
            SELECT p.id AS project_id, p.format_version, p.parser_name,
                   p.parser_version, p.is_complete, p.raw_counts_json,
                   p.imported_at, s.path, s.sha256, s.size_bytes, s.modified_ns,
                   d.application_version, d.data_structure_json, d.metrics_json,
                   d.processing_status, d.reference_status, d.reference_details,
                   d.errors_json, d.warnings_json
            FROM projects p
            JOIN source_files s ON s.id=p.source_id
            LEFT JOIN project_diagnostics d ON d.project_id=p.id
            WHERE p.id=?
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        latest_job = self.connection.execute(
            """
            SELECT status, source_sha256, message, started_at
            FROM import_jobs WHERE source_path=?
            ORDER BY id DESC LIMIT 1
            """,
            (row["path"],),
        ).fetchone()

        metrics = (
            json.loads(row["metrics_json"])
            if row["metrics_json"]
            else json.loads(row["raw_counts_json"] or "{}")
        )
        errors = json.loads(row["errors_json"]) if row["errors_json"] else []
        warnings = json.loads(row["warnings_json"]) if row["warnings_json"] else [
            {
                "code": "legacy_import_without_diagnostic",
                "message": "This project predates structured import diagnostics.",
            }
        ]
        versions = [
            dict(item)
            for item in self.connection.execute(
                """
                SELECT id, name, description, slot_index, declared_slot_count,
                       blob_offset, blob_size, epk, epk_offset, status,
                       parent_id, is_root
                FROM project_versions WHERE project_id=? ORDER BY slot_index
                """,
                (project_id,),
            )
        ]
        slots = [
            dict(item)
            for item in self.connection.execute(
                """
                SELECT slot_index, version_id, blob_offset, blob_size, marker_hex, raw_sha256,
                       payload_sha256, payload_size, binary_status, name_status
                FROM project_version_slots WHERE project_id=? ORDER BY slot_index
                """,
                (project_id,),
            )
        ]
        map_rows = self.connection.execute(
            """
            SELECT m.id, m.ordinal, m.name, m.description, m.map_type, m.data_type,
                   m.columns_count, m.rows_count, m.data_offset, m.unit, m.factor,
                   m.conversion_offset, m.folder_id, m.type_code,
                   m.version_relation_status, m.status,
                   COUNT(a.id) AS axis_count,
                   SUM(CASE WHEN a.status='incomplete' THEN 1 ELSE 0 END)
                       AS incomplete_axes
            FROM map_definitions m
            LEFT JOIN map_axes a ON a.map_id=m.id
            WHERE m.project_id=?
            GROUP BY m.id
            ORDER BY m.ordinal
            """,
            (project_id,),
        ).fetchall()
        maps = []
        for item in map_rows:
            map_data = dict(item)
            map_data["axes"] = [
                dict(axis)
                for axis in self.connection.execute(
                    """
                    SELECT role, points, address, data_type, unit, factor,
                           conversion_offset, identifier, description, data_source,
                           status
                    FROM map_axes WHERE map_id=? ORDER BY role
                    """,
                    (item["id"],),
                )
            ]
            maps.append(map_data)
        structure = (
            json.loads(row["data_structure_json"])
            if row["data_structure_json"]
            else {}
        )
        return {
            "project_id": row["project_id"],
            "source": {
                "path": row["path"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
                "modified_ns": row["modified_ns"],
            },
            "application_version": row["application_version"] or "unknown",
            "parser": {
                "name": row["parser_name"],
                "version": row["parser_version"],
            },
            "format_version": row["format_version"],
            "data_structure": structure,
            "metrics": metrics,
            "metadata": {
                item["field"]: {"value": item["value"], "status": item["status"]}
                for item in self.connection.execute(
                    """
                    SELECT field, value, status FROM project_metadata
                    WHERE project_id=? ORDER BY field
                    """,
                    (project_id,),
                )
            },
            "processing_status": (
                "failed"
                if latest_job and latest_job["status"] in ("failed", "blocked")
                else row["processing_status"] or "partially_processed"
            ),
            "stored_processing_status": row["processing_status"] or "partially_processed",
            "latest_import_attempt": (
                {
                    "status": latest_job["status"],
                    "sha256": latest_job["source_sha256"],
                    "message": latest_job["message"],
                    "started_at": latest_job["started_at"],
                    "source_changed": latest_job["source_sha256"] not in (
                        None,
                        row["sha256"],
                    ),
                }
                if latest_job
                else None
            ),
            "reference_validation": {
                "status": row["reference_status"] or "not_validated",
                "details": row["reference_details"]
                or "No independent reference result is recorded.",
            },
            "version_inventory": {
                "declared_binary_slots": metrics.get("declared_binary_slots"),
                "named_version_records": len(versions),
                "enumerated_binary_slots": len(slots),
                "available_nonempty_unverified_slots": sum(
                    item["binary_status"] == "present_nonempty_unverified"
                    for item in slots
                ),
                "unlabeled_slots": sum(item["name_status"] == "unresolved" for item in slots),
                "versions": versions,
                "slots": slots,
            },
            "element_counts": {
                "map_list_records_declared": metrics.get("map_list_records_declared"),
                "map_records_returned_by_parser": metrics.get("map_records_returned_by_parser"),
                "hexdump_records_skipped": metrics.get("hexdump_records_skipped"),
                "map_record_parse_failures": metrics.get("map_record_parse_failures"),
                "map_definitions_returned_by_parser": metrics.get("map_definitions_returned"),
                "adapter_map_definitions_received": metrics.get(
                    "adapter_map_definitions_received"
                ),
                "adapter_axes_received": metrics.get("adapter_axes_received"),
                "map_definitions_stored": metrics.get("stored_map_definitions"),
                "axes_stored": metrics.get("stored_map_axes"),
                "folders_stored": metrics.get("stored_folders"),
                "cal_blocks_stored": metrics.get("stored_cal_blocks"),
                "unsupported_element_count": metrics.get("unsupported_element_count"),
                "unsupported_element_count_status": metrics.get(
                    "unsupported_element_count_status", "not_reported_by_parser"
                ),
                "skipped_elements_stored": metrics.get("stored_skipped_elements"),
            },
            "map_definitions": maps,
            "skipped_elements": [
                dict(item)
                for item in self.connection.execute(
                    """
                    SELECT ordinal, element_kind, description, raw_metadata_json, reason
                    FROM project_skipped_elements WHERE project_id=? ORDER BY ordinal
                    """,
                    (project_id,),
                )
            ],
            "errors": errors,
            "warnings": warnings,
            "generated_at": row["imported_at"],
        }

    def list_diagnostics(
        self,
        *,
        processing_status: str = "",
        parser_errors: bool = False,
        needs_reference: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("Diagnostic list limit must be between 1 and 1000.")
        if processing_status and processing_status not in {
            "fully_processed",
            "partially_processed",
            "failed",
        }:
            raise ValueError("Unsupported processing status.")
        results: list[dict] = []
        remaining = limit
        if processing_status in {
            "",
            "fully_processed",
            "partially_processed",
            "failed",
        }:
            clauses = ["1=1"]
            params: list[object] = []
            effective_status = (
                "CASE WHEN j.status IN ('failed', 'blocked') THEN 'failed' "
                "ELSE COALESCE(d.processing_status, 'partially_processed') END"
            )
            if processing_status:
                clauses.append(f"({effective_status})=?")
                params.append(processing_status)
            if parser_errors:
                clauses.append(
                    "(COALESCE(d.error_count, 0)>0 OR "
                    "j.status IN ('failed', 'blocked'))"
                )
            if needs_reference:
                clauses.append(
                    "(d.reference_status IS NULL OR d.reference_status!='validated')"
                )
            params.append(remaining)
            rows = self.connection.execute(
                f"""
                SELECT p.id, s.path, s.sha256, s.size_bytes, p.format_version,
                       {effective_status} AS processing_status,
                       COALESCE(d.reference_status, 'not_validated') AS reference_status,
                       COALESCE(d.error_count, 0) +
                           CASE WHEN j.status IN ('failed', 'blocked') THEN 1 ELSE 0 END
                           AS error_count,
                       COALESCE(d.warning_count, 1) AS warning_count,
                       (SELECT COUNT(*) FROM project_versions v WHERE v.project_id=p.id)
                           AS named_versions,
                       (SELECT COUNT(*) FROM project_version_slots v WHERE v.project_id=p.id)
                           AS binary_slots,
                       (SELECT COUNT(*) FROM project_version_slots v
                        WHERE v.project_id=p.id AND v.name_status='unresolved')
                           AS unlabeled_binary_slots,
                       (SELECT COUNT(*) FROM map_definitions m WHERE m.project_id=p.id)
                           AS map_count,
                       CAST(json_extract(d.metrics_json, '$.map_definitions_returned')
                            AS INTEGER) AS parser_map_count,
                       j.status AS latest_import_status,
                       j.source_sha256 AS latest_import_sha256,
                       j.message AS latest_import_message
                FROM projects p
                JOIN source_files s ON s.id=p.source_id
                LEFT JOIN project_diagnostics d ON d.project_id=p.id
                LEFT JOIN import_jobs j ON j.id=(
                    SELECT MAX(j2.id) FROM import_jobs j2 WHERE j2.source_path=s.path
                )
                WHERE {' AND '.join(clauses)}
                ORDER BY p.id DESC LIMIT ?
                """,
                params,
            ).fetchall()
            results.extend(dict(row) for row in rows)
            remaining = limit - len(results)

        if remaining and processing_status in ("", "failed"):
            failed_rows = self.connection.execute(
                """
                SELECT j.id AS job_id, j.source_path AS path,
                       j.source_sha256 AS sha256, j.status AS processing_status,
                       j.message, j.started_at
                FROM import_jobs j
                WHERE j.status IN ('failed', 'blocked')
                  AND NOT EXISTS (
                    SELECT 1 FROM import_jobs newer
                    WHERE newer.source_path=j.source_path AND newer.id>j.id
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM projects p
                    JOIN source_files s ON s.id=p.source_id
                    WHERE s.path=j.source_path
                  )
                ORDER BY j.id DESC LIMIT ?
                """,
                (remaining,),
            ).fetchall()
            for row in failed_rows:
                results.append(
                    {
                        "project_id": None,
                        "job_id": row["job_id"],
                        "path": row["path"],
                        "sha256": row["sha256"],
                        "processing_status": "failed",
                        "reference_status": "not_validated",
                        "error_count": 1,
                        "warning_count": 0,
                        "named_versions": 0,
                        "binary_slots": 0,
                        "map_count": 0,
                        "message": row["message"],
                        "started_at": row["started_at"],
                    }
                )
        if parser_errors:
            results = [row for row in results if row["error_count"] > 0]
        if needs_reference:
            results = [
                row for row in results if row["reference_status"] != "validated"
            ]
        return results[:limit]

    def store_reference_artifact(
        self,
        *,
        project_id: int,
        manifest_path: str,
        manifest_sha256: str,
        ordinal: int,
        kind: str,
        path: str,
        sha256: str,
        size_bytes: int,
        version_name: str,
        source_slot_index: int | None,
        relation_status: str,
        representation: str,
        metadata: dict,
    ) -> tuple[int, str]:
        existing = self.connection.execute(
            """
            SELECT id, manifest_sha256, sha256, path, metadata_json
            FROM reference_artifacts
            WHERE project_id=? AND manifest_path=? AND artifact_index=?
            ORDER BY id LIMIT 1
            """,
            (project_id, manifest_path, ordinal),
        ).fetchone()
        action = "imported" if existing is None else "already_imported"
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        if existing is not None and (
            existing["manifest_sha256"] != manifest_sha256
            or existing["sha256"] != sha256
            or existing["path"] != path
            or existing["metadata_json"] != metadata_json
        ):
            action = "updated"
        with self.connection:
            if existing is not None:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO reference_check_history(
                        origin_check_id, artifact_id, artifact_kind, artifact_path,
                        artifact_sha256, source_sha256, status, message,
                        comparison_json, checked_at
                    )
                    SELECT c.id, c.artifact_id, a.kind, a.path, a.sha256,
                           c.source_sha256, c.status, c.message,
                           c.comparison_json, c.checked_at
                    FROM reference_checks c
                    JOIN reference_artifacts a ON a.id=c.artifact_id
                    WHERE c.artifact_id=?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM reference_check_history h
                          WHERE h.artifact_id=c.artifact_id
                            AND h.artifact_path=a.path
                            AND h.artifact_sha256=a.sha256
                            AND h.source_sha256=c.source_sha256
                            AND h.status=c.status
                            AND h.message=c.message
                            AND h.comparison_json=c.comparison_json
                      )
                    """,
                    (existing["id"],),
                )
                self.connection.execute(
                    """
                    UPDATE reference_artifacts SET
                        manifest_sha256=?, kind=?, path=?, sha256=?, size_bytes=?,
                        version_name=?, source_slot_index=?, relation_status=?,
                        representation=?, metadata_json=?, imported_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        manifest_sha256,
                        kind,
                        path,
                        sha256,
                        size_bytes,
                        version_name,
                        source_slot_index,
                        relation_status,
                        representation,
                        metadata_json,
                        existing["id"],
                    ),
                )
                if action == "updated":
                    self.connection.execute(
                        "DELETE FROM reference_checks WHERE artifact_id=?",
                        (existing["id"],),
                    )
                artifact_id = int(existing["id"])
            else:
                cursor = self.connection.execute(
                    """
                    INSERT INTO reference_artifacts(
                        project_id, manifest_path, manifest_sha256, artifact_index,
                        kind, path, sha256, size_bytes, version_name,
                        source_slot_index, relation_status, representation, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        manifest_path,
                        manifest_sha256,
                        ordinal,
                        kind,
                        path,
                        sha256,
                        size_bytes,
                        version_name,
                        source_slot_index,
                        relation_status,
                        representation,
                        metadata_json,
                    ),
                )
                artifact_id = int(cursor.lastrowid)
        return artifact_id, action

    def list_reference_artifacts(self, project_id: int) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT id, project_id, manifest_path, manifest_sha256, artifact_index,
                   kind, path, sha256, size_bytes, version_name, source_slot_index,
                   relation_status, representation, metadata_json, imported_at
            FROM reference_artifacts WHERE project_id=?
            ORDER BY artifact_index, id
            """,
            (project_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def store_reference_check(
        self,
        *,
        artifact_id: int,
        source_sha256: str,
        status: str,
        message: str,
        comparison: dict | None,
    ) -> None:
        with self.connection:
            artifact = self.connection.execute(
                """
                SELECT kind, path, sha256 FROM reference_artifacts WHERE id=?
                """,
                (artifact_id,),
            ).fetchone()
            if artifact is None:
                raise ValueError(f"No reference artifact found with id {artifact_id}.")
            comparison_json = json.dumps(
                comparison or {}, ensure_ascii=False, sort_keys=True
            )
            self.connection.execute(
                """
                INSERT INTO reference_check_history(
                    artifact_id, artifact_kind, artifact_path, artifact_sha256,
                    source_sha256, status, message, comparison_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    artifact["kind"],
                    artifact["path"],
                    artifact["sha256"],
                    source_sha256,
                    status,
                    message,
                    comparison_json,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO reference_checks(
                    artifact_id, source_sha256, status, message, comparison_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id, source_sha256) DO UPDATE SET
                    status=excluded.status,
                    message=excluded.message,
                    comparison_json=excluded.comparison_json,
                    checked_at=CURRENT_TIMESTAMP
                """,
                (
                    artifact_id,
                    source_sha256,
                    status,
                    message,
                    comparison_json,
                ),
            )

    def list_reference_checks(self, project_id: int) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT c.artifact_id, a.kind, a.path, a.version_name,
                   a.source_slot_index, a.relation_status, a.representation,
                   c.status, c.message, c.comparison_json, c.checked_at
            FROM reference_checks c
            JOIN reference_artifacts a ON a.id=c.artifact_id
            WHERE a.project_id=?
            ORDER BY a.artifact_index, c.id
            """,
            (project_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["comparison"] = json.loads(item.pop("comparison_json"))
            result.append(item)
        return result

    def list_reference_check_history(
        self,
        project_id: int,
        *,
        limit: int = 500,
    ) -> list[dict]:
        if not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000.")
        rows = self.connection.execute(
            """
            SELECT h.id, h.origin_check_id, h.artifact_id, h.artifact_kind,
                   h.artifact_path, h.artifact_sha256, h.source_sha256,
                   h.status, h.message, h.comparison_json, h.checked_at
            FROM reference_check_history h
            JOIN reference_artifacts a ON a.id=h.artifact_id
            WHERE a.project_id=?
            ORDER BY h.id DESC
            LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
        return [
            {
                **dict(row),
                "comparison": json.loads(row["comparison_json"]),
            }
            for row in rows
        ]

    def store_binary_analysis_run(
        self,
        *,
        project_id: int,
        source_sha256: str,
        algorithm_version: str,
        options: dict,
        report: dict,
    ) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO binary_analysis_runs(
                    project_id, source_sha256, algorithm_version,
                    options_json, report_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    source_sha256,
                    algorithm_version,
                    json.dumps(options, sort_keys=True),
                    json.dumps(report, ensure_ascii=False, sort_keys=True),
                ),
            )
        return int(cursor.lastrowid)

    def store_binary_slot_candidate(
        self,
        *,
        run_id: int,
        artifact_id: int,
        project_id: int,
        slot_index: int,
        version_id: int | None,
        export_sha256: str,
        slot_sha256: str,
        binary_status: str,
        version_relation_status: str,
        evidence: dict,
    ) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO binary_slot_candidates(
                    run_id, artifact_id, project_id, slot_index, version_id,
                    export_sha256, slot_sha256, binary_status,
                    version_relation_status, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    artifact_id,
                    project_id,
                    slot_index,
                    version_id,
                    export_sha256,
                    slot_sha256,
                    binary_status,
                    version_relation_status,
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                ),
            )
        return int(cursor.lastrowid)

    def list_binary_analysis_runs(self, project_id: int) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT id, project_id, source_sha256, algorithm_version,
                   options_json, report_json, created_at
            FROM binary_analysis_runs
            WHERE project_id=?
            ORDER BY id DESC
            """,
            (project_id,),
        ).fetchall()
        return [
            {
                **dict(row),
                "options": json.loads(row["options_json"]),
                "report": json.loads(row["report_json"]),
            }
            for row in rows
        ]

    def list_binary_slot_candidates(
        self,
        project_id: int | None = None,
        *,
        status: str = "all",
        limit: int = 500,
    ) -> list[dict]:
        if not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000.")
        if status not in {"all", "confirmed", "unresolved", "exact_binary"}:
            raise ValueError(
                "status must be all, confirmed, unresolved, or exact_binary."
            )
        filters = []
        parameters: list[object] = []
        if project_id is not None:
            filters.append("c.project_id=?")
            parameters.append(project_id)
        confirmed_expression = """
            (
                c.version_relation_status='PASS'
                OR EXISTS (
                    SELECT 1 FROM version_match_reviews vr
                    WHERE vr.candidate_id=c.id
                      AND vr.decision='confirm_version'
                      AND vr.id=(
                          SELECT MAX(vr2.id) FROM version_match_reviews vr2
                          WHERE vr2.candidate_id=c.id
                      )
                )
            )
        """
        if status == "confirmed":
            filters.append(confirmed_expression)
        elif status == "unresolved":
            filters.append(f"NOT {confirmed_expression}")
        elif status == "exact_binary":
            filters.append("c.binary_status='PASS'")
        filter_clause = "WHERE " + " AND ".join(filters) if filters else ""
        parameters.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT c.id, c.run_id, c.artifact_id, c.project_id,
                   c.slot_index, c.version_id, v.name AS parser_version_name,
                   c.export_sha256, c.slot_sha256, c.binary_status,
                   c.version_relation_status, c.evidence_json, c.created_at,
                   a.path AS export_path, a.version_name AS export_version_name,
                   r.decision AS latest_review_decision,
                   r.evidence AS latest_review_evidence,
                   r.reviewer AS latest_review_by
            FROM binary_slot_candidates c
            JOIN reference_artifacts a ON a.id=c.artifact_id
            LEFT JOIN project_versions v ON v.id=c.version_id
            LEFT JOIN version_match_reviews r ON r.id=(
                SELECT MAX(r2.id) FROM version_match_reviews r2
                WHERE r2.candidate_id=c.id
            )
            {filter_clause}
            ORDER BY c.id DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        return [
            {
                **dict(row),
                "evidence": json.loads(row["evidence_json"]),
            }
            for row in rows
        ]

    def review_version_match(
        self,
        candidate_id: int,
        decision: str,
        evidence: str,
        reviewer: str = "local-operator",
    ) -> int:
        if decision not in {"confirm_version", "reject", "defer"}:
            raise ValueError("decision must be confirm_version, reject, or defer.")
        if len(evidence.strip()) < 12:
            raise ValueError("Review evidence must contain at least 12 characters.")
        if not reviewer.strip():
            raise ValueError("reviewer must not be empty.")
        candidate = self.connection.execute(
            """
            SELECT version_id, binary_status
            FROM binary_slot_candidates WHERE id=?
            """,
            (candidate_id,),
        ).fetchone()
        if candidate is None:
            raise ValueError(f"No binary slot candidate found with id {candidate_id}.")
        if decision == "confirm_version" and (
            candidate["version_id"] is None
            or candidate["binary_status"] != "PASS"
        ):
            raise ValueError(
                "A version can be confirmed only for an exact binary match "
                "to a named parser version."
            )
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO version_match_reviews(
                    candidate_id, decision, evidence, reviewer
                ) VALUES (?, ?, ?, ?)
                """,
                (candidate_id, decision, evidence.strip(), reviewer.strip()),
            )
        return int(cursor.lastrowid)
