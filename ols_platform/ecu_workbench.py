from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from . import __version__
from .compare import byte_similarity, compare_binary_files, validate_byte_threshold
from .project_search import search_projects


_CHUNK_SIZE = 1024 * 1024
_MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
_IDENTIFICATION_FIELDS = (
    "ecu_type",
    "ecu_manufacturer",
    "ecu_family",
    "sw_number",
    "hw_number",
    "hw_code",
    "calibration_number",
    "calibration_id",
    "make",
    "model",
    "year",
    "engine",
)


class AnalysisAssistant(Protocol):
    provider_name: str

    def summarize(self, report: dict[str, Any]) -> str: ...


class RawBinaryEvidenceAdapter:
    _FORMATS = {".bin": "raw_bin", ".ori": "raw_ori"}

    def inspect(self, filename: str, sha256: str, size_bytes: int) -> dict[str, Any]:
        suffix = Path(filename).suffix.lower()
        if suffix not in self._FORMATS:
            raise ValueError("Only raw .bin and .ori files are supported.")
        return {
            "adapter": "raw_binary_v1",
            "format": self._FORMATS[suffix],
            "format_status": "extension_only",
            "sha256": sha256,
            "size_bytes": size_bytes,
            "parser_status": "not_run_no_validated_raw_binary_decoder",
        }


def _connect_store(storage_root: Path) -> sqlite3.Connection:
    storage_root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(storage_root / "workbench.sqlite3", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS workbench_analyses (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            created_at TEXT NOT NULL,
            source_filename TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            identifications_json TEXT NOT NULL,
            report_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workbench_analyses_created
            ON workbench_analyses(created_at DESC);
        CREATE TABLE IF NOT EXISTS workbench_comparisons (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            analysis_a_id TEXT NOT NULL,
            analysis_b_id TEXT NOT NULL,
            report_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workbench_comparisons_created
            ON workbench_comparisons(created_at DESC);
        """
    )
    return connection


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _identifications(values: dict[str, Any] | None) -> dict[str, str]:
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ValueError("Identifications must be a JSON object.")
    result: dict[str, str] = {}
    for field in _IDENTIFICATION_FIELDS:
        value = values.get(field, "")
        if not isinstance(value, str):
            raise ValueError(f"Identification {field} must be text.")
        clean = value.strip()
        if clean:
            if len(clean) > 200:
                raise ValueError(f"Identification {field} is too long.")
            result[field] = clean
    return result


def _is_confirmed_reference(reference: dict[str, Any]) -> bool:
    return (
        reference["relation_status"] == "confirmed"
        and reference["representation"] == "full_binary"
        and reference["validation_status"] == "PASS"
        and reference["validation_source_sha256"]
        and reference["live_source_sha256"]
        and reference["validation_source_sha256"].lower()
        == reference["live_source_sha256"].lower()
    )


def _project_identity_evidence(
    connection: sqlite3.Connection, project_id: int, slot_index: int | None
) -> dict[str, Any]:
    project = connection.execute(
        """
        SELECT p.parser_name,p.parser_version,p.format_version,d.processing_status
        FROM projects p
        LEFT JOIN project_diagnostics d ON d.project_id=p.id
        WHERE p.id=?
        """,
        (project_id,),
    ).fetchone()
    metadata_rows = connection.execute(
        """
        SELECT field,value,status FROM project_metadata
        WHERE project_id=? AND field IN ({})
        """.format(",".join("?" for _ in _IDENTIFICATION_FIELDS)),
        (project_id, *_IDENTIFICATION_FIELDS),
    ).fetchall()
    metadata = {
        row["field"]: {
            "value": row["value"],
            "status": row["status"],
            "source": "project_metadata",
        }
        for row in metadata_rows
        if row["value"]
    }
    version = None
    if slot_index is not None:
        version_row = connection.execute(
            """
            SELECT name,description,status,slot_index
            FROM project_versions
            WHERE project_id=? AND slot_index=?
            ORDER BY id
            LIMIT 1
            """,
            (project_id, slot_index),
        ).fetchone()
        if version_row:
            version = dict(version_row)
    if project is None:
        parser = {"name": None, "version": None, "format_version": None, "status": "unknown"}
    else:
        parser = {
            "name": project["parser_name"],
            "version": project["parser_version"],
            "format_version": project["format_version"],
            "status": project["processing_status"] or "unknown",
        }
    return {"parser": parser, "version": version, "metadata": metadata}


def _build_ecu_identity_evidence(
    *,
    file_evidence: dict[str, Any],
    user_provided: dict[str, str],
    exact_references: list[dict[str, Any]],
    slot_candidates: list[dict[str, Any]],
    metadata_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_records = [
        {
            "kind": "exact_binary_reference",
            "artifact_id": item["artifact_id"],
            "project_id": item["project_id"],
            "version_name": item["version_name"],
            "status": (
                "INCONCLUSIVE"
                if len(exact_references) > 1
                else item["status"]
            ),
            "parser": item["project_identity"]["parser"],
            "metadata": item["project_identity"]["metadata"],
        }
        for item in exact_references
    ]
    candidate_records.extend(
        {
            "kind": "project_metadata_candidate",
            "project_id": item["project_id"],
            "path": item["path"],
            "status": item["status"],
            "parser": item["parser"],
            "metadata_status": item["metadata_status"],
            "metadata": item["metadata"],
        }
        for item in metadata_candidates
    )

    fields: dict[str, dict[str, Any]] = {}
    has_conflicts = False
    has_candidate_evidence = bool(exact_references or slot_candidates or metadata_candidates)
    for field in _IDENTIFICATION_FIELDS:
        candidate_values: list[dict[str, Any]] = []
        for candidate in candidate_records:
            raw_value = candidate["metadata"].get(field)
            if isinstance(raw_value, dict):
                value = raw_value.get("value")
                metadata_status = raw_value.get("status", "unknown")
            else:
                value = raw_value
                metadata_status = candidate.get("metadata_status", {}).get(
                    field, "parser_reported"
                )
            if value:
                candidate_values.append(
                    {
                        "value": str(value),
                        "evidence_status": metadata_status,
                        "candidate_kind": candidate["kind"],
                        "project_id": candidate["project_id"],
                        "parser": candidate["parser"],
                    }
                )
        distinct_values = {item["value"].casefold() for item in candidate_values}
        supplied = user_provided.get(field)
        conflicts = len(distinct_values) > 1 or bool(
            supplied
            and any(item["value"].casefold() != supplied.casefold() for item in candidate_values)
        )
        if conflicts:
            has_conflicts = True
            fields[field] = {
                "status": "CONFLICTING",
                "value": None,
                "user_provided": supplied,
                "candidate_values": candidate_values,
            }
            continue

        unique_reference = exact_references[0] if len(exact_references) == 1 else None
        direct_values = [
            item for item in candidate_values
            if item["candidate_kind"] == "exact_binary_reference"
            and item["evidence_status"] == "direct"
        ]
        if (
            unique_reference is not None
            and unique_reference["individually_validated_reference"]
            and direct_values
        ):
            fields[field] = {
                "status": "DERIVED",
                "value": direct_values[0]["value"],
                "evidence": direct_values,
            }
        elif candidate_values:
            fields[field] = {
                "status": "INCONCLUSIVE",
                "value": None,
                "candidate_values": candidate_values,
            }
        elif supplied:
            fields[field] = {
                "status": "UNVERIFIED_USER_INPUT",
                "value": supplied,
                "evidence": [{"source": "user_provided", "status": "unverified"}],
            }
        else:
            fields[field] = {"status": "UNKNOWN", "value": None, "evidence": []}

    if has_conflicts:
        identity_status = "CONFLICTING"
    elif any(item["status"] == "DERIVED" for item in fields.values()):
        identity_status = "DERIVED"
    elif has_candidate_evidence or user_provided:
        identity_status = "INCONCLUSIVE"
    else:
        identity_status = "UNKNOWN"

    return {
        "status": identity_status,
        "input_adapter": file_evidence["adapter"],
        "format": file_evidence["format"],
        "format_status": file_evidence["format_status"],
        "sha256": file_evidence["sha256"],
        "size_bytes": file_evidence["size_bytes"],
        "parser_status": file_evidence["parser_status"],
        "fields": fields,
        "candidates": candidate_records,
        "slot_candidate_count": len(slot_candidates),
    }


def _lookup_references(
    index_database: Any,
    *,
    sha256: str,
    size_bytes: int,
    identifications: dict[str, str],
    filename: str = "input.bin",
) -> dict[str, Any]:
    connection = index_database.connection
    exact_reference_rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT a.id AS artifact_id,a.project_id,a.path,a.sha256,a.size_bytes,
                   a.version_name,a.source_slot_index,a.relation_status,
                   a.representation,
                   COALESCE((
                       SELECT r.status FROM reference_checks r
                       WHERE r.artifact_id=a.id ORDER BY r.id DESC LIMIT 1
                   ),'NOT_TESTED') AS validation_status,
                   (SELECT r.source_sha256 FROM reference_checks r
                    WHERE r.artifact_id=a.id ORDER BY r.id DESC LIMIT 1)
                       AS validation_source_sha256,
                   (SELECT fl.sha256 FROM projects p
                    JOIN source_files s ON s.id=p.source_id
                    JOIN file_locations fl ON fl.path=s.path
                    WHERE p.id=a.project_id AND fl.file_kind='ols'
                      AND fl.is_present=1 AND fl.sha256 IS NOT NULL
                    ORDER BY fl.id DESC LIMIT 1) AS live_source_sha256
            FROM reference_artifacts a
            WHERE a.kind='binary_version' AND a.representation='full_binary'
              AND lower(a.sha256)=? AND a.size_bytes=?
            ORDER BY a.id DESC LIMIT 51
            """,
            (sha256, size_bytes),
        )
    ]
    exact_references = exact_reference_rows[:50]
    for item in exact_references:
        item["individually_validated_reference"] = _is_confirmed_reference(item)
        item["status"] = "CONFIRMED_REFERENCE" if item["individually_validated_reference"] else "EXACT_CONTENT_MATCH"
        item["content_match_status"] = "EXACT_CONTENT_MATCH"
        item["project_identity"] = _project_identity_evidence(
            connection, item["project_id"], item["source_slot_index"]
        )
        item["validation_matches_current_source"] = (
            bool(item["live_source_sha256"])
            and bool(item["validation_source_sha256"])
            and item["live_source_sha256"].lower()
            == item["validation_source_sha256"].lower()
        )
        item["evidence"] = (
            "Complete SHA-256 and size match a full_binary reference export."
        )
        if item["status"] != "CONFIRMED_REFERENCE":
            item["limitation"] = (
                "Byte identity is exact, but reference relation or independent "
                "validation is not confirmed."
            )

    slot_candidate_rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT p.id AS project_id,s.path AS project_path,
                   v.name AS version_name,vs.slot_index,vs.payload_sha256,
                   vs.payload_size,vs.binary_status,vs.name_status,
                   COALESCE(d.reference_status,'not_validated') AS reference_status,
                   (SELECT fl.sha256 FROM file_locations fl
                    WHERE fl.path=s.path AND fl.file_kind='ols'
                      AND fl.is_present=1 AND fl.sha256 IS NOT NULL
                    ORDER BY fl.id DESC LIMIT 1) AS live_sha256,
                   s.sha256 AS parser_sha256
            FROM project_version_slots vs
            JOIN projects p ON p.id=vs.project_id
            JOIN source_files s ON s.id=p.source_id
            LEFT JOIN project_versions v ON v.id=vs.version_id
            LEFT JOIN project_diagnostics d ON d.project_id=p.id
            WHERE lower(vs.payload_sha256)=? AND vs.payload_size=?
            ORDER BY p.id,vs.slot_index LIMIT 51
            """,
            (sha256, size_bytes),
        )
    ]
    slot_candidates = slot_candidate_rows[:50]
    for item in slot_candidates:
        item["parser_snapshot_matches_current_content"] = (
            item["live_sha256"] == item["parser_sha256"]
            if item["live_sha256"] and item["parser_sha256"] else None
        )
        item["status"] = "INCONCLUSIVE"
        item["evidence"] = (
            "A parser-reported slot payload hash and size match, but slot "
            "representation, extent, and version relation are not independently confirmed."
        )

    metadata_candidates: list[dict[str, Any]] = []
    if identifications:
        search_report = search_projects(
            index_database,
            metadata=identifications,
            metadata_match_mode="any",
            limit=20,
        )
        for result in search_report["results"]:
            live_match = result["source"]["parser_snapshot_matches_current_content"]
            matched_identifications = [
                field for field, value in identifications.items()
                if result["metadata"].get(field) == value
            ]
            differing_identifications = [
                {
                    "field": field,
                    "provided": value,
                    "project_value": result["metadata"].get(field),
                }
                for field, value in identifications.items()
                if result["metadata"].get(field)
                and result["metadata"].get(field) != value
            ]
            missing_identifications = [
                field for field in identifications
                if not result["metadata"].get(field)
            ]
            candidate_status = (
                "LIKELY"
                if len(matched_identifications) >= 2
                and not differing_identifications
                and live_match is True
                else "INCONCLUSIVE"
            )
            metadata_candidates.append(
                {
                    "project_id": result["project_id"],
                    "path": result["source"]["path"],
                    "status": candidate_status,
                    "parser": result["parser"],
                    "parser_warnings": result["parser_warnings"],
                    "parser_errors": result["parser_errors"],
                    "metadata_status": result["metadata_status"],
                    "rank_score": result["rank_score"],
                    "rank_reasons": result["rank_reasons"],
                    "matched_identifications": matched_identifications,
                    "differing_identifications": differing_identifications,
                    "missing_identifications": missing_identifications,
                    "metadata": result["metadata"],
                    "hash_provenance": result["source"]["hash_provenance"],
                    "parser_snapshot_matches_current_content": live_match,
                    "reference_validation": result["reference_validation"],
                    "versions": result["versions"][:20],
                    "versions_truncated": len(result["versions"]) > 20,
                    "binary_slots": result["binary_slots"][:30],
                    "slots_truncated": len(result["binary_slots"]) > 30,
                    "maps": result["maps"][:15],
                    "maps_truncated": len(result["maps"]) > 15,
                    "missing_information": result["missing_information"],
                    "uncertainties": result["uncertainties"],
                    "evidence": (
                        "Candidate retrieved using exact user-provided metadata; "
                        "rank_score is the documented search tally, not probability."
                    ),
                }
            )

    file_evidence = RawBinaryEvidenceAdapter().inspect(
        filename, sha256, size_bytes
    )
    ecu_identity = _build_ecu_identity_evidence(
        file_evidence=file_evidence,
        user_provided=identifications,
        exact_references=exact_references,
        slot_candidates=slot_candidates,
        metadata_candidates=metadata_candidates,
    )
    exact_references_truncated = len(exact_reference_rows) > 50
    if len(exact_references) > 1 or exact_references_truncated:
        for item in exact_references:
            item["status"] = "INCONCLUSIVE"
            item["limitation"] = (
                "More than one full-binary reference has the same exact content; "
                "the matching project/version assignment is ambiguous."
            )
        overall_status = "INCONCLUSIVE"
    elif exact_references and exact_references[0]["individually_validated_reference"]:
        overall_status = (
            "INCONCLUSIVE"
            if ecu_identity["status"] == "CONFLICTING"
            else "CONFIRMED_REFERENCE"
        )
    elif exact_references:
        overall_status = "EXACT_CONTENT_MATCH"
    elif any(item["status"] == "LIKELY" for item in metadata_candidates):
        overall_status = "LIKELY_CANDIDATE"
    elif slot_candidates or metadata_candidates:
        overall_status = "INCONCLUSIVE"
    else:
        overall_status = "NO_MATCH"
    return {
        "status": overall_status,
        "exact_references": exact_references,
        "exact_references_truncated": exact_references_truncated,
        "slot_candidates": slot_candidates,
        "slot_candidates_truncated": len(slot_candidate_rows) > 50,
        "metadata_candidates": metadata_candidates,
        "metadata_candidates_truncated": (
            search_report["has_more"] if identifications else False
        ),
        "score_definition": (
            "Metadata candidate rank_score reuses the documented deterministic "
            "project-search tally; it is not a probability or validation result."
        ),
        "ecu_identity": ecu_identity,
    }


def _persist_analysis(
    storage_root: Path,
    *,
    stored_path: Path,
    source_filename: str,
    source_kind: str,
    sha256: str,
    size_bytes: int,
    identifications: dict[str, str],
    report: dict[str, Any],
    parent_id: str | None = None,
    origin_path: str | None = None,
) -> dict[str, Any]:
    analysis_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    analysis = {
        "analysis_id": analysis_id,
        "parent_analysis_id": parent_id,
        "created_at": created_at,
        "source": {
            "filename": source_filename,
            "kind": source_kind,
            "origin_path": origin_path,
            "stored_copy": str(stored_path.resolve()),
            "sha256": sha256,
            "size_bytes": size_bytes,
            "modified": False,
        },
        "identifications": {
            "user_provided": identifications,
            "automatically_extracted": {},
            "evidence": report.get("ecu_identity", {}),
            "missing": [
                field for field in _IDENTIFICATION_FIELDS
                if field not in identifications
            ],
        },
        "report": report,
        "validation": {
            "status": report["status"],
            "errors": [],
            "warnings": report.get("limitations", []),
        },
        "ai": {
            "provider": None,
            "summary": None,
            "status": "not_configured; raw files remain local",
        },
        "engine_versions": {
            "application": __version__,
            "parser": None,
            "parser_status": report.get("ecu_identity", {}).get(
                "parser_status", "not_run_no_validated_raw_binary_decoder"
            ),
        },
    }
    connection = _connect_store(storage_root)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO workbench_analyses(
                    id,parent_id,created_at,source_filename,source_kind,stored_path,
                    sha256,size_bytes,identifications_json,report_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    analysis_id,
                    parent_id,
                    created_at,
                    source_filename,
                    source_kind,
                    str(stored_path.resolve()),
                    sha256,
                    size_bytes,
                    json.dumps(identifications, ensure_ascii=False),
                    json.dumps(analysis, ensure_ascii=False),
                ),
            )
    finally:
        connection.close()
    return analysis


def import_binary_stream(
    source: BinaryIO,
    *,
    filename: str,
    expected_size: int,
    storage_root: Path,
    index_database: Any,
    identifications: dict[str, Any] | None = None,
    max_bytes: int = _MAX_UPLOAD_BYTES,
) -> dict[str, Any]:
    file_suffix = Path(filename).suffix.lower()
    if file_suffix not in {".bin", ".ori"}:
        raise ValueError(
            "Only raw .bin and .ori files are supported. Intel HEX and other "
            "formats need a validated importer and explicit representation handling."
        )
    if expected_size <= 0 or expected_size > max_bytes:
        raise ValueError(f"BIN upload must be 1-{max_bytes} bytes.")
    clean_identifications = _identifications(identifications)
    files_root = storage_root / "files"
    files_root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".upload-", suffix=".tmp", dir=files_root
    )
    temporary_path = Path(temporary_name)
    digest = hashlib.sha256()
    bytes_written = 0
    try:
        with os.fdopen(descriptor, "wb") as target:
            while chunk := source.read(_CHUNK_SIZE):
                bytes_written += len(chunk)
                if bytes_written > max_bytes or bytes_written > expected_size:
                    raise ValueError("Upload exceeded its declared or allowed size.")
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        if bytes_written != expected_size:
            raise ValueError(
                f"Upload ended early: received {bytes_written} of {expected_size} bytes."
            )
        sha256 = digest.hexdigest()
        stored_path = files_root / f"{sha256}.bin"
        if stored_path.exists():
            if _hash_file(stored_path) != (sha256, expected_size):
                raise RuntimeError(
                    "An existing content-addressed Workbench copy failed integrity verification."
                )
            temporary_path.unlink()
        else:
            os.replace(temporary_path, stored_path)
        references = _lookup_references(
            index_database,
            sha256=sha256,
            size_bytes=expected_size,
            identifications=clean_identifications,
            filename=Path(filename).name,
        )
        report = {
            "status": references["status"],
            "file": {
                "format": f"raw_{file_suffix[1:]}",
                "ecu_file_validity": "not established; no content-format decoder is configured",
                "sha256": sha256,
                "size_bytes": expected_size,
                "hash_method": "streaming SHA-256 over every uploaded byte",
                "input_adapter": references["ecu_identity"]["input_adapter"],
                "format_status": references["ecu_identity"]["format_status"],
            },
            "identifications": {
                "automatically_extracted": {},
                "user_provided": clean_identifications,
                "evidence": references["ecu_identity"],
                "missing": [
                    field for field in _IDENTIFICATION_FIELDS
                    if field not in clean_identifications
                ],
            },
            "references": references,
            "ecu_identity": references["ecu_identity"],
            "limitations": [
                "No ECU-identification decoder is configured; no identifiers were inferred from arbitrary BIN bytes.",
                "A matching full-file hash proves byte identity only, not vehicle compatibility.",
                "Parser slot payload hashes are candidates only; their extent and representation may be unverified.",
                "Map and axis definitions remain parser metadata and are not bound here to this BIN or its numeric values.",
                "A NO_MATCH result means no match was found in the available indexed evidence, not that none exists.",
            ],
        }
        return _persist_analysis(
            storage_root,
            stored_path=stored_path,
            source_filename=Path(filename).name,
            source_kind="loopback_browser_upload",
            sha256=sha256,
            size_bytes=expected_size,
            identifications=clean_identifications,
            report=report,
        )
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def list_reference_artifacts(
    index_database: Any,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 1000:
        raise ValueError("Reference artifact limit must be between 1 and 1000.")
    rows = index_database.connection.execute(
        """
        SELECT a.id AS artifact_id,a.project_id,a.path,a.sha256,a.size_bytes,
               a.version_name,a.source_slot_index,a.relation_status,a.representation,
               COALESCE((
                   SELECT r.status FROM reference_checks r
                   WHERE r.artifact_id=a.id ORDER BY r.id DESC LIMIT 1
               ),'NOT_TESTED') AS validation_status,
               (SELECT r.source_sha256 FROM reference_checks r
                WHERE r.artifact_id=a.id ORDER BY r.id DESC LIMIT 1)
                   AS validation_source_sha256,
               (SELECT fl.sha256 FROM projects p
                JOIN source_files s ON s.id=p.source_id
                JOIN file_locations fl ON fl.path=s.path
                WHERE p.id=a.project_id AND fl.file_kind='ols'
                  AND fl.is_present=1 AND fl.sha256 IS NOT NULL
                ORDER BY fl.id DESC LIMIT 1) AS live_source_sha256
        FROM reference_artifacts a
        WHERE a.kind='binary_version' AND a.representation='full_binary'
        ORDER BY a.project_id,a.id DESC LIMIT ?
        """,
        (limit,),
    )
    results = []
    for row in rows:
        item = dict(row)
        item["available"] = Path(item["path"]).is_file()
        item["validation_matches_current_source"] = (
            bool(item["live_source_sha256"])
            and bool(item["validation_source_sha256"])
            and item["live_source_sha256"].lower()
            == item["validation_source_sha256"].lower()
        )
        item["status"] = (
            "CONFIRMED_REFERENCE"
            if _is_confirmed_reference(item)
            else "INCONCLUSIVE"
        )
        item["individually_validated_reference"] = (
            item["status"] == "CONFIRMED_REFERENCE"
        )
        item["project_identity"] = _project_identity_evidence(
            index_database.connection, item["project_id"], item["source_slot_index"]
        )
        results.append(item)
    return results


def import_reference_artifact(
    storage_root: Path,
    artifact_id: int,
    index_database: Any,
) -> dict[str, Any]:
    if artifact_id <= 0:
        raise ValueError("Reference artifact ID must be positive.")
    row = index_database.connection.execute(
        """
        SELECT a.id AS artifact_id,a.project_id,a.path,a.sha256,a.size_bytes,
               a.version_name,a.source_slot_index,a.relation_status,a.representation,
               COALESCE((
                   SELECT r.status FROM reference_checks r
                   WHERE r.artifact_id=a.id ORDER BY r.id DESC LIMIT 1
               ),'NOT_TESTED') AS validation_status,
               (SELECT r.source_sha256 FROM reference_checks r
                WHERE r.artifact_id=a.id ORDER BY r.id DESC LIMIT 1)
                   AS validation_source_sha256,
               (SELECT fl.sha256 FROM projects p
                JOIN source_files s ON s.id=p.source_id
                JOIN file_locations fl ON fl.path=s.path
                WHERE p.id=a.project_id AND fl.file_kind='ols'
                  AND fl.is_present=1 AND fl.sha256 IS NOT NULL
                ORDER BY fl.id DESC LIMIT 1) AS live_source_sha256
        FROM reference_artifacts a
        WHERE a.id=? AND a.kind='binary_version'
          AND a.representation='full_binary'
        """,
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"No full_binary reference artifact exists with ID {artifact_id}."
        )
    reference = dict(row)
    reference["project_identity"] = _project_identity_evidence(
        index_database.connection, reference["project_id"], reference["source_slot_index"]
    )
    source_path = Path(reference["path"]).resolve(strict=True)
    if not source_path.is_file():
        raise ValueError("The indexed binary reference path is not a file.")
    before = source_path.stat()
    files_root = storage_root / "files"
    files_root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".reference-", suffix=".tmp", dir=files_root
    )
    temporary_path = Path(temporary_name)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with source_path.open("rb") as source, os.fdopen(descriptor, "wb") as target:
            while chunk := source.read(_CHUNK_SIZE):
                target.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = source_path.stat()
        sha256 = digest.hexdigest()
        if (
            size_bytes != reference["size_bytes"]
            or sha256.lower() != reference["sha256"].lower()
        ):
            raise ValueError(
                "The reference file no longer matches its stored full-file hash and size."
            )
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError("The reference file changed while it was being copied.")
        stored_path = files_root / f"{sha256}.bin"
        if stored_path.exists():
            if _hash_file(stored_path) != (sha256, size_bytes):
                raise RuntimeError(
                    "An existing content-addressed Workbench copy failed integrity verification."
                )
            temporary_path.unlink()
        else:
            os.replace(temporary_path, stored_path)
        reference["validation_matches_current_source"] = (
            bool(reference["live_source_sha256"])
            and bool(reference["validation_source_sha256"])
            and reference["live_source_sha256"].lower()
            == reference["validation_source_sha256"].lower()
        )
        reference["individually_validated_reference"] = _is_confirmed_reference(reference)
        reference["status"] = (
            "CONFIRMED_REFERENCE"
            if reference["individually_validated_reference"]
            else "EXACT_CONTENT_MATCH"
        )
        references = _lookup_references(
            index_database,
            sha256=sha256,
            size_bytes=size_bytes,
            identifications={},
            filename=source_path.name
            if source_path.suffix.lower() in {".bin", ".ori"}
            else "reference.bin",
        )
        references["selected_reference"] = {
            **reference,
            "content_verified_at_import": True,
        }
        ecu_identity = references["ecu_identity"]
        ecu_identity["input_adapter"] = "indexed_full_binary_reference_v1"
        ecu_identity["format"] = reference["representation"]
        ecu_identity["format_status"] = "manifest_asserted"
        status = references["status"]
        report = {
            "status": status,
            "file": {
                "format": reference["representation"],
                "sha256": sha256,
                "size_bytes": size_bytes,
                "input_adapter": ecu_identity["input_adapter"],
                "format_status": ecu_identity["format_status"],
                "ecu_file_validity": "format is asserted by the indexed reference record, not independently decoded",
            },
            "ecu_identity": ecu_identity,
            "references": references,
            "limitations": [
                "Reference bytes were re-read and matched the stored SHA-256 and size before archiving.",
                "The project/version relationship is no stronger than its stored relation and latest reference-check status.",
                "A raw binary export is not independently decoded as a valid ECU image.",
            ],
        }
        return _persist_analysis(
            storage_root,
            stored_path=stored_path,
            source_filename=source_path.name,
            source_kind="indexed_reference_artifact",
            sha256=sha256,
            size_bytes=size_bytes,
            identifications={},
            report=report,
            origin_path=str(source_path),
        )
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _get_analysis(storage_root: Path, analysis_id: str) -> dict[str, Any]:
    connection = _connect_store(storage_root)
    try:
        row = connection.execute(
            "SELECT report_json FROM workbench_analyses WHERE id=?",
            (analysis_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown Workbench analysis: {analysis_id}.")
        return json.loads(row["report_json"])
    finally:
        connection.close()


def reanalyze(
    storage_root: Path,
    analysis_id: str,
    index_database: Any,
) -> dict[str, Any]:
    previous = _get_analysis(storage_root, analysis_id)
    source = previous["source"]
    stored_path = Path(source["stored_copy"]).resolve(strict=True)
    files_root = (storage_root / "files").resolve()
    if files_root not in stored_path.parents:
        raise ValueError("Stored analysis copy is outside the Workbench data directory.")
    actual_hash, actual_size = _hash_file(stored_path)
    if (actual_hash, actual_size) != (source["sha256"], source["size_bytes"]):
        raise ValueError("Stored analysis copy changed; refusing to reanalyze it.")
    identifications = _identifications(
        previous["identifications"]["user_provided"]
    )
    references = _lookup_references(
        index_database,
        sha256=actual_hash,
        size_bytes=actual_size,
        identifications=identifications,
        filename=source["filename"],
    )
    report = {
        "status": references["status"],
        "file": {
            "format": previous["report"]["file"].get("format", "raw_bin"),
            "sha256": actual_hash,
            "size_bytes": actual_size,
            "hash_method": "streaming SHA-256 over every stored-copy byte",
            "input_adapter": references["ecu_identity"]["input_adapter"],
            "format_status": references["ecu_identity"]["format_status"],
        },
        "identifications": {
            "automatically_extracted": {},
            "user_provided": identifications,
            "evidence": references["ecu_identity"],
            "missing": [
                field for field in _IDENTIFICATION_FIELDS
                if field not in identifications
            ],
        },
        "references": references,
        "ecu_identity": references["ecu_identity"],
        "limitations": [
            "Reanalysis creates a new history record; the prior report is retained.",
            "No ECU-identification decoder is configured; no identifiers were inferred.",
            "A matching full-file hash proves byte identity only, not vehicle compatibility.",
            "Parser slot payload matches are unconfirmed until representation and range are independently validated.",
            "Map definitions are not bound to this binary's numeric values.",
        ],
    }
    return _persist_analysis(
        storage_root,
        stored_path=stored_path,
        source_filename=source["filename"],
        source_kind="workbench_copy_reanalysis",
        sha256=actual_hash,
        size_bytes=actual_size,
        identifications=identifications,
        report=report,
        parent_id=analysis_id,
    )


def compare_analyses(
    storage_root: Path,
    analysis_a_id: str,
    analysis_b_id: str,
    *,
    threshold_percent: float = 95.0,
) -> dict[str, Any]:
    validate_byte_threshold(threshold_percent)
    analysis_a = _get_analysis(storage_root, analysis_a_id)
    analysis_b = _get_analysis(storage_root, analysis_b_id)
    path_a = Path(analysis_a["source"]["stored_copy"]).resolve(strict=True)
    path_b = Path(analysis_b["source"]["stored_copy"]).resolve(strict=True)
    files_root = (storage_root / "files").resolve()
    if any(files_root not in path.parents for path in (path_a, path_b)):
        raise ValueError("Analysis file is outside the Workbench data directory.")
    for analysis, path in ((analysis_a, path_a), (analysis_b, path_b)):
        actual_hash, actual_size = _hash_file(path)
        if (
            actual_hash != analysis["source"]["sha256"]
            or actual_size != analysis["source"]["size_bytes"]
        ):
            raise ValueError("An archived analysis file changed; comparison cancelled.")
    comparison = compare_binary_files(path_a, path_b)
    if (
        comparison.original_sha256 != analysis_a["source"]["sha256"]
        or comparison.original_size != analysis_a["source"]["size_bytes"]
        or comparison.tuned_sha256 != analysis_b["source"]["sha256"]
        or comparison.tuned_size != analysis_b["source"]["size_bytes"]
    ):
        raise RuntimeError("An archived analysis changed during comparison.")

    def reference_context(analysis: dict[str, Any]) -> dict[str, Any]:
        references = analysis["report"].get("references", {})
        return {
            "selected_reference": references.get("selected_reference"),
            "exact_reference_matches": [
                {
                    "artifact_id": item["artifact_id"],
                    "project_id": item["project_id"],
                    "version_name": item["version_name"],
                    "status": item["status"],
                }
                for item in references.get("exact_references", [])[:10]
            ],
            "candidate_project_ids": [
                item["project_id"]
                for item in (
                    references.get("slot_candidates", [])
                    + references.get("metadata_candidates", [])
                )[:20]
            ],
        }

    identifiers_a = analysis_a["identifications"]["user_provided"]
    identifiers_b = analysis_b["identifications"]["user_provided"]
    identity_conflicts = [
        {"field": field, "left": value, "right": identifiers_b[field]}
        for field, value in identifiers_a.items()
        if value and identifiers_b.get(field) and value != identifiers_b[field]
    ]
    report = {
        "comparison_id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "engine_versions": {
            "application": __version__,
            "parser": None,
            "parser_status": "not used for raw binary comparison",
        },
        "analysis_a": {
            "analysis_id": analysis_a_id,
            "filename": analysis_a["source"]["filename"],
            "sha256": comparison.original_sha256,
            "size_bytes": comparison.original_size,
            "origin": analysis_a["source"]["kind"],
            "reference_context": reference_context(analysis_a),
        },
        "analysis_b": {
            "analysis_id": analysis_b_id,
            "filename": analysis_b["source"]["filename"],
            "sha256": comparison.tuned_sha256,
            "size_bytes": comparison.tuned_size,
            "origin": analysis_b["source"]["kind"],
            "reference_context": reference_context(analysis_b),
        },
        "differing_bytes": comparison.differing_bytes,
        "changed_ranges": [
            {"start": item.start, "end_exclusive": item.end_exclusive}
            for item in comparison.changed_ranges
        ],
        "exact_content_match": comparison.exact_match,
        "byte_similarity": byte_similarity(comparison, threshold_percent),
        "user_identification_conflicts": identity_conflicts,
        "semantic_status": "INCONCLUSIVE",
        "reference_selection_reason": (
            "The user explicitly selected these two archived analyses."
        ),
        "limitations": [
            "Byte differences do not establish technical meaning, map identity, or tuning intent.",
            "No checksum correction, address normalization, or format conversion was applied.",
            "Changed ranges are half-open byte offsets in the compared raw files.",
            "User-provided identification fields and their conflicts are not independently validated.",
        ],
    }
    connection = _connect_store(storage_root)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO workbench_comparisons(
                    id,created_at,analysis_a_id,analysis_b_id,report_json
                ) VALUES(?,?,?,?,?)
                """,
                (
                    report["comparison_id"],
                    report["created_at"],
                    analysis_a_id,
                    analysis_b_id,
                    json.dumps(report, ensure_ascii=False),
                ),
            )
    finally:
        connection.close()
    return report


def history(storage_root: Path, *, limit: int = 100) -> dict[str, Any]:
    if not 1 <= limit <= 500:
        raise ValueError("History limit must be between 1 and 500.")
    connection = _connect_store(storage_root)
    try:
        analyses = [
            {
                "analysis_id": row["id"],
                "parent_analysis_id": row["parent_id"],
                "created_at": row["created_at"],
                "source_filename": row["source_filename"],
                "source_kind": row["source_kind"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
                "status": json.loads(row["report_json"])["report"]["status"],
            }
            for row in connection.execute(
                """
                SELECT id,parent_id,created_at,source_filename,source_kind,
                       sha256,size_bytes,report_json
                FROM workbench_analyses ORDER BY created_at DESC,id DESC LIMIT ?
                """,
                (limit,),
            )
        ]
        comparisons = [
            json.loads(row["report_json"])
            for row in connection.execute(
                """
                SELECT report_json FROM workbench_comparisons
                ORDER BY created_at DESC,id DESC LIMIT ?
                """,
                (limit,),
            )
        ]
        return {"analyses": analyses, "comparisons": comparisons}
    finally:
        connection.close()


def get_analysis(storage_root: Path, analysis_id: str) -> dict[str, Any]:
    return _get_analysis(storage_root, analysis_id)


def get_comparison(storage_root: Path, comparison_id: str) -> dict[str, Any]:
    connection = _connect_store(storage_root)
    try:
        row = connection.execute(
            "SELECT report_json FROM workbench_comparisons WHERE id=?",
            (comparison_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown Workbench comparison: {comparison_id}.")
        return json.loads(row["report_json"])
    finally:
        connection.close()


def export_candidates(
    storage_root: Path,
    analysis_id: str,
    index_database: Any,
    *,
    include_other_projects: bool = False,
    artifact_id: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """List indexed binary artifacts without asserting their ECU compatibility."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("Candidate offset must be a nonnegative integer.")
    analysis = _get_analysis(storage_root, analysis_id)
    references = analysis["report"]["references"]
    project_methods: dict[int, list[str]] = {}
    for key, method in (
        ("exact_references", "project_associated_with_exact_original_reference"),
        ("slot_candidates", "project_associated_with_parser_slot_hash"),
        ("metadata_candidates", "project_metadata_match"),
    ):
        for item in references.get(key, []):
            project_methods.setdefault(item["project_id"], []).append(method)
    project_ids = sorted(project_methods)
    if not include_other_projects and not project_ids:
        return {"candidates": [], "truncated": False, "offset": offset}
    where = ""
    parameters: list[Any] = []
    if not include_other_projects:
        where = " AND a.project_id IN ({})".format(",".join("?" for _ in project_ids))
        parameters.extend(project_ids)
    if artifact_id is not None:
        where += " AND a.id=?"
        parameters.append(artifact_id)
    rows = index_database.connection.execute(
        """
        SELECT a.id AS artifact_id,a.project_id,a.path,a.sha256,a.size_bytes,
               a.version_name,a.source_slot_index,a.relation_status,a.representation,
               COALESCE(d.warnings_json,'[]') AS warnings_json,
               COALESCE(d.errors_json,'[]') AS errors_json,
               COALESCE((SELECT r.status FROM reference_checks r
                         WHERE r.artifact_id=a.id ORDER BY r.id DESC LIMIT 1),
                        'NOT_TESTED') AS validation_status
        FROM reference_artifacts a
        LEFT JOIN project_diagnostics d ON d.project_id=a.project_id
        WHERE a.kind='binary_version'
          AND a.representation IN ('full_binary','unknown')
          AND (lower(a.path) LIKE '%.bin' OR lower(a.path) LIKE '%.ori')
        """ + where + " ORDER BY a.project_id,a.id LIMIT 501 OFFSET ?",
        [*parameters, offset],
    ).fetchall()
    candidates = []
    supplied = dict(analysis["identifications"]["user_provided"])
    identification_sources = {field: "USER_PROVIDED_UNVERIFIED" for field in supplied}
    for field, evidence in analysis["identifications"]["evidence"].get("fields", {}).items():
        if field not in supplied and evidence.get("status") == "DERIVED" and evidence.get("value"):
            supplied[field] = evidence["value"]
            identification_sources[field] = "DERIVED_FROM_INDEXED_REFERENCE"
    for row in rows[:500]:
        item = dict(row)
        path = Path(item["path"])
        identity = _project_identity_evidence(
            index_database.connection, item["project_id"], item["source_slot_index"]
        )
        metadata = {key: value["value"] for key, value in identity["metadata"].items()}
        matched = [
            key for key, value in supplied.items()
            if metadata.get(key) and metadata[key].casefold() == value.casefold()
        ]
        differing = [
            {"field": key, "provided": value, "project_value": metadata[key]}
            for key, value in supplied.items()
            if metadata.get(key) and metadata[key].casefold() != value.casefold()
        ]
        missing = [
            key for key in _IDENTIFICATION_FIELDS
            if not supplied.get(key) or not metadata.get(key)
        ]
        metadata_candidate = next(
            (entry for entry in references.get("metadata_candidates", [])
             if entry["project_id"] == item["project_id"]),
            None,
        )
        exact_candidate = any(
            entry["artifact_id"] == item["artifact_id"]
            for entry in references.get("exact_references", [])
        )
        candidates.append({
            "artifact_id": item["artifact_id"],
            "project_id": item["project_id"],
            "path": item["path"],
            "filename": path.name,
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "size_comparison": (
                "MATCH" if item["size_bytes"] == analysis["source"]["size_bytes"]
                else "DIFFERENT"
            ),
            "available": path.is_file(),
            "representation": item["representation"],
            "version_name": item["version_name"] or "UNKNOWN",
            "relation_status": item["relation_status"],
            "original_or_tuned": "UNKNOWN",
            "tuning_type": "UNKNOWN",
            "identifications": metadata,
            "identification_evidence": identity["metadata"],
            "original_identification_sources": identification_sources,
            "matched_features": matched,
            "differing_features": differing,
            "missing_features": missing,
            "parser_warnings": json.loads(item["warnings_json"]),
            "parser_errors": json.loads(item["errors_json"]),
            "reference_validation": item["validation_status"],
            "compatibility": "COMPATIBILITY_NOT_VALIDATED",
            "match_methods": (
                project_methods.get(item["project_id"], ["manual_browse"])
                + (["indexed_candidate_sha256_matches_original"] if exact_candidate else [])
            ),
            "retrieval_score": metadata_candidate["rank_score"] if metadata_candidate else None,
            "retrieval_score_meaning": (
                "Project metadata retrieval points, not a byte similarity percentage or "
                "ECU compatibility score."
            ),
            "similarity": {
                "status": "NOT_COMPUTED",
                "percent": None,
                "meaning": "No byte similarity score was calculated for this candidate.",
            },
            "risk": (
                "Project metadata and reference labels do not establish ECU compatibility "
                "or prove that this artifact is a complete tuning file."
            ),
        })
    return {"candidates": candidates, "truncated": len(rows) > 500, "offset": offset}


def export_preview(
    storage_root: Path,
    analysis_id: str,
    artifact_id: int,
    index_database: Any,
) -> dict[str, Any]:
    if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id <= 0:
        raise ValueError("A positive candidate artifact ID is required.")
    analysis = _get_analysis(storage_root, analysis_id)
    candidates = export_candidates(
        storage_root, analysis_id, index_database,
        include_other_projects=True, artifact_id=artifact_id,
    )["candidates"]
    candidate = next((item for item in candidates if item["artifact_id"] == artifact_id), None)
    if candidate is None:
        raise ValueError("The selected indexed BIN candidate is not available in the candidate list.")
    if not candidate["available"]:
        raise FileNotFoundError(f"Selected candidate does not exist: {candidate['path']}")
    if candidate["size_bytes"] <= 0:
        raise ValueError("The selected candidate is empty; refusing export.")
    original_path = Path(analysis["source"]["stored_copy"]).resolve(strict=True)
    files_root = (storage_root / "files").resolve()
    if files_root not in original_path.parents:
        raise ValueError("Original analysis copy is outside the Workbench data directory.")
    if _hash_file(original_path) != (
        analysis["source"]["sha256"], analysis["source"]["size_bytes"]
    ):
        raise ValueError("The imported original copy no longer matches its recorded hash.")
    candidate_path = Path(candidate["path"]).resolve(strict=True)
    actual_hash, actual_size = _hash_file(candidate_path)
    if actual_hash.lower() != candidate["sha256"].lower() or actual_size != candidate["size_bytes"]:
        raise ValueError("The selected candidate no longer matches its indexed hash and size.")
    preview = {
        "analysis_id": analysis_id,
        "original": {
            "sha256": analysis["source"]["sha256"],
            "size_bytes": analysis["source"]["size_bytes"],
            "identifications": analysis["identifications"]["user_provided"],
            "identification_evidence": analysis["identifications"]["evidence"],
        },
        "candidate": candidate,
        "compatibility": "COMPATIBILITY_NOT_VALIDATED",
    }
    preview["confirmation_token"] = hashlib.sha256(
        json.dumps(preview, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return preview


def export_selected_binary(
    storage_root: Path,
    analysis_id: str,
    artifact_id: int,
    index_database: Any,
    *,
    output_directory: Path,
    output_name: str,
    confirmation_token: str,
    confirmed: bool,
    user_label: str = "",
) -> dict[str, Any]:
    if confirmed is not True:
        raise ValueError("Explicit user confirmation is required.")
    if not isinstance(output_name, str) or not output_name or Path(output_name).name != output_name:
        raise ValueError("Output name must be a filename without a directory.")
    if Path(output_name).suffix.lower() != ".bin":
        raise ValueError("Output filename must end in .bin.")
    if not isinstance(user_label, str) or len(user_label) > 200:
        raise ValueError("User label must be text of at most 200 characters.")
    directory = Path(output_directory).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("The selected output directory is not a directory.")
    preview = export_preview(storage_root, analysis_id, artifact_id, index_database)
    if preview["confirmation_token"] != confirmation_token:
        raise ValueError("The candidate or its evidence changed after confirmation; review again.")
    candidate = preview["candidate"]
    source_path = Path(candidate["path"]).resolve(strict=True)
    output_path = directory / output_name
    manifest_path = directory / (output_name + ".manifest.json")
    if output_path.exists() or manifest_path.exists():
        raise FileExistsError("Output BIN or manifest already exists; choose another name or directory.")
    created: list[tuple[Path, os.stat_result]] = []
    try:
        before = source_path.stat()
        digest = hashlib.sha256()
        size = 0
        with source_path.open("rb") as source, output_path.open("xb") as target:
            created.append((output_path, os.fstat(target.fileno())))
            while chunk := source.read(_CHUNK_SIZE):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = source_path.stat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or size != candidate["size_bytes"]
            or digest.hexdigest().lower() != candidate["sha256"].lower()
        ):
            raise ValueError("The selected source changed or did not match its indexed hash.")
        output_hash, output_size = _hash_file(output_path)
        if output_hash != digest.hexdigest() or output_size != size:
            raise RuntimeError("Export verification failed: output differs from selected source.")
        manifest = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "original": preview["original"],
            "candidate": {
                key: value for key, value in candidate.items() if key != "path"
            },
            "source_artifact_id": artifact_id,
            "user_label": user_label or "UNKNOWN",
            "output": {"filename": output_name, "sha256": output_hash, "size_bytes": output_size},
            "source_output_sha256_equal": True,
            "compatibility": "COMPATIBILITY_NOT_VALIDATED",
            "status": "FILE_EXPORTED",
        }
        with manifest_path.open("x", encoding="utf-8") as target:
            created.append((manifest_path, os.fstat(target.fileno())))
            json.dump(manifest, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        return {"output_path": str(output_path), "manifest_path": str(manifest_path), **manifest}
    except Exception:
        for path, identity in reversed(created):
            try:
                current = path.stat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass
        raise
