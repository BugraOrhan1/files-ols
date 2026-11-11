from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Protocol


_LOGGER = logging.getLogger(__name__)
_SLOW_SEARCH_SECONDS = 0.25
_SEARCH_QUERY_TIMEOUT_SECONDS = 5.0
_METADATA_FIELDS = (
    "ecu_type",
    "ecu_manufacturer",
    "ecu_family",
    "sw_number",
    "hw_number",
    "hw_code",
    "calibration_number",
    "calibration_id",
    "engine",
    "make",
    "model",
    "year",
    "fuel_type",
    "displacement",
    "power",
    "gearbox",
    "memory_type",
)
_IDENTIFIER_FIELDS = {
    "ecu_type",
    "ecu_manufacturer",
    "ecu_family",
    "sw_number",
    "hw_number",
    "hw_code",
    "calibration_number",
    "calibration_id",
}


class SearchRanker(Protocol):
    provider_name: str

    def rank(
        self, result: dict[str, Any], criteria: dict[str, Any]
    ) -> tuple[int, list[str]]: ...


class EvidenceRanker:
    provider_name = "deterministic-evidence"

    def rank(
        self, result: dict[str, Any], criteria: dict[str, Any]
    ) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        metadata = criteria.get("metadata", {})
        result_metadata = result.get("metadata", {})
        exact_identifiers = [
            field for field, value in metadata.items()
            if field in _IDENTIFIER_FIELDS
            and result_metadata.get(field) == value
        ]
        if exact_identifiers:
            score += len(exact_identifiers) * 10
            reasons.append(
                f"{len(exact_identifiers)} exact ECU/calibration identifier filter(s) matched."
            )
        if (
            criteria.get("file_hash")
            and result.get("source", {}).get("live_sha256")
            and result["source"]["live_sha256"].lower()
            == criteria["file_hash"].lower()
        ):
            score += 100
            reasons.append(
                "The complete OLS source SHA-256 matches exactly; this confirms "
                "file-content identity only."
            )
        if criteria.get("binary_hash"):
            score += 12
            reasons.append(
                "Parser-reported binary hash matched; slot extent is not independently verified."
            )
        if criteria.get("map_name") or criteria.get("columns") is not None or criteria.get("rows") is not None:
            score += 4
            reasons.append(
                "Parser-reported map structure matched; equal names/dimensions do not prove map semantics."
            )
        if criteria.get("version_name"):
            score += 4
            reasons.append(
                "Named project-version record matched; binary slot relationship may remain unknown."
            )
        if result.get("reference_validation") == "validated":
            score += 5
            reasons.append("Project has an independently validated reference status.")
        if result["counts"]["maps"] and result["counts"]["axes"]:
            score += 2
            reasons.append("Map and axis definitions are available in the parser snapshot.")
        if result.get("parser", {}).get("status") == "fully_processed":
            score += 1
            reasons.append("The parser marked this project fully processed.")
        uncertainty = [
            "Search ranking is evidence-based and does not establish a tune relationship.",
            *(
                ["Some project/version/map information is missing."]
                if result["missing_information"] else []
            ),
        ]
        result["ranking_uncertainties"] = uncertainty
        return score, reasons


def _contains_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_projects(
    database: Any,
    *,
    text: str = "",
    metadata: dict[str, str] | None = None,
    metadata_match_mode: str = "all",
    map_name: str = "",
    columns: int | None = None,
    rows: int | None = None,
    version_name: str = "",
    binary_hash: str = "",
    format_version: int | None = None,
    validation_status: str = "",
    parser_status: str = "",
    source_path: str = "",
    file_hash: str = "",
    file_size: int | None = None,
    min_file_size: int | None = None,
    max_file_size: int | None = None,
    map_text: str = "",
    limit: int = 100,
    offset: int = 0,
    ranker: SearchRanker | None = None,
) -> dict[str, Any]:
    if not 1 <= limit <= 1000:
        raise ValueError("Search limit must be between 1 and 1000.")
    if not 0 <= offset <= 10_000_000:
        raise ValueError("Search offset must be between 0 and 10000000.")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("Metadata filters must be a field-to-text mapping.")
    filters = metadata or {}
    if metadata_match_mode not in {"all", "any"}:
        raise ValueError("Metadata match mode must be 'all' or 'any'.")
    unknown = set(filters) - set(_METADATA_FIELDS)
    if unknown:
        raise ValueError(f"Unsupported metadata filter(s): {sorted(unknown)}")
    if any(not isinstance(value, str) for value in filters.values()):
        raise ValueError("Metadata filters must be text values.")
    filters = {field: value.strip() for field, value in filters.items()}
    for name, value in (
        ("text", text),
        ("map name", map_name),
        ("version name", version_name),
        ("binary hash", binary_hash),
        ("file hash", file_hash),
        ("validation status", validation_status),
        ("parser status", parser_status),
        ("source path", source_path),
        ("map text", map_text),
    ):
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a text value.")
    if (columns is not None and columns < 0) or (rows is not None and rows < 0):
        raise ValueError("Map dimensions cannot be negative.")
    if any(
        value is not None and value < 0
        for value in (file_size, min_file_size, max_file_size)
    ):
        raise ValueError("File sizes cannot be negative.")
    if file_size is not None and (
        min_file_size is not None or max_file_size is not None
    ):
        raise ValueError("Use exact file size or a size range, not both.")
    if (
        min_file_size is not None and max_file_size is not None
        and min_file_size > max_file_size
    ):
        raise ValueError("Minimum file size cannot exceed maximum file size.")
    if file_hash and (
        len(file_hash) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in file_hash)
    ):
        raise ValueError("A complete file SHA-256 must contain 64 hexadecimal characters.")
    started = time.perf_counter()
    clauses: list[str] = []
    parameters: list[object] = []
    matched_features: list[tuple[str, str]] = []
    metadata_clauses: list[str] = []
    metadata_parameters: list[object] = []
    for field, value in filters.items():
        if not value.strip():
            continue
        metadata_clauses.append(
            "EXISTS (SELECT 1 FROM project_metadata pm "
            "WHERE pm.project_id=p.id AND pm.field=? AND pm.value=?)"
        )
        metadata_parameters.extend((field, value.strip()))
        matched_features.append((field, value.strip()))
    if metadata_clauses:
        operator = " AND " if metadata_match_mode == "all" else " OR "
        clauses.append(f"({operator.join(metadata_clauses)})")
        parameters.extend(metadata_parameters)
    if text.strip():
        clauses.append(
            """
            (
              s.path LIKE ? ESCAPE '\' OR EXISTS (
                SELECT 1 FROM project_metadata pm
                WHERE pm.project_id=p.id AND pm.value LIKE ? ESCAPE '\'
              ) OR EXISTS (
                SELECT 1 FROM map_definitions m
                WHERE m.project_id=p.id AND
                  (m.name LIKE ? ESCAPE '\' OR m.description LIKE ? ESCAPE '\')
              ) OR EXISTS (
                SELECT 1 FROM project_versions v
                WHERE v.project_id=p.id AND
                  (v.name LIKE ? ESCAPE '\' OR v.description LIKE ? ESCAPE '\')
              )
            )
            """
        )
        pattern = _contains_pattern(text.strip())
        parameters.extend((pattern,) * 6)
        matched_features.append(("text", text.strip()))
    if source_path.strip():
        clauses.append("s.path LIKE ? ESCAPE '\\'")
        parameters.append(_contains_pattern(source_path.strip()))
        matched_features.append(("source_path", source_path.strip()))
    if map_name.strip():
        clauses.append(
            "EXISTS (SELECT 1 FROM map_definitions m WHERE m.project_id=p.id "
            "AND m.name=?)"
        )
        parameters.append(map_name.strip())
        matched_features.append(("map_name", map_name.strip()))
    if map_text.strip():
        clauses.append(
            "EXISTS (SELECT 1 FROM map_definitions m WHERE m.project_id=p.id "
            "AND (m.name LIKE ? ESCAPE '\\' OR m.description LIKE ? ESCAPE '\\'))"
        )
        pattern = _contains_pattern(map_text.strip())
        parameters.extend((pattern, pattern))
        matched_features.append(("map_text", map_text.strip()))
    for key, value in (("columns_count", columns), ("rows_count", rows)):
        if value is not None:
            clauses.append(
                f"EXISTS (SELECT 1 FROM map_definitions m WHERE m.project_id=p.id AND m.{key}=?)"
            )
            parameters.append(value)
            matched_features.append((key, str(value)))
    if version_name.strip():
        clauses.append(
            "EXISTS (SELECT 1 FROM project_versions v WHERE v.project_id=p.id "
            "AND v.name=?)"
        )
        parameters.append(version_name.strip())
        matched_features.append(("version_name", version_name.strip()))
    if binary_hash.strip():
        clauses.append(
            "EXISTS (SELECT 1 FROM project_version_slots vs WHERE vs.project_id=p.id "
            "AND (vs.payload_sha256=? OR vs.raw_sha256=?))"
        )
        parameters.extend((binary_hash.strip().lower(),) * 2)
        matched_features.append(("binary_hash", binary_hash.strip().lower()))
    if file_hash:
        clauses.append(
            "EXISTS (SELECT 1 FROM file_locations fl "
            "WHERE fl.path=s.path AND fl.file_kind='ols' "
            "AND fl.is_present=1 AND fl.sha256=? "
            "ORDER BY fl.id DESC LIMIT 1)"
        )
        parameters.append(file_hash.strip().lower())
        matched_features.append(("file_hash", file_hash.strip().lower()))
    if file_size is not None:
        clauses.append("s.size_bytes=?")
        parameters.append(file_size)
        matched_features.append(("file_size", str(file_size)))
    if min_file_size is not None:
        clauses.append("s.size_bytes>=?")
        parameters.append(min_file_size)
        matched_features.append(("min_file_size", str(min_file_size)))
    if max_file_size is not None:
        clauses.append("s.size_bytes<=?")
        parameters.append(max_file_size)
        matched_features.append(("max_file_size", str(max_file_size)))
    if format_version is not None:
        clauses.append("p.format_version=?")
        parameters.append(format_version)
        matched_features.append(("format_version", str(format_version)))
    if validation_status:
        clauses.append("COALESCE(d.reference_status,'not_validated')=?")
        parameters.append(validation_status)
        matched_features.append(("validation_status", validation_status))
    if parser_status:
        clauses.append("COALESCE(d.processing_status,'partially_processed')=?")
        parameters.append(parser_status)
        matched_features.append(("parser_status", parser_status))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.extend((limit + 1, offset))
    search_connection = database.connection
    deadline = started + _SEARCH_QUERY_TIMEOUT_SECONDS
    search_connection.set_progress_handler(
        lambda: int(time.perf_counter() >= deadline), 5000
    )
    try:
        uses_default_ranker = ranker is None or type(ranker) is EvidenceRanker
        order_clause = (
            """
            ORDER BY (
                CASE WHEN d.reference_status='validated' THEN 5 ELSE 0 END
                + CASE WHEN map_count>0 AND axis_count>0 THEN 2 ELSE 0 END
                + CASE WHEN d.processing_status='fully_processed' THEN 1 ELSE 0 END
            ) DESC,p.id DESC
            """
            if uses_default_ranker
            else "ORDER BY p.id DESC"
        )
        rows_found = search_connection.execute(
            f"""
        SELECT p.id,p.format_version,p.parser_name,p.parser_version,p.is_complete,
               s.path,s.sha256 AS parser_sha256,(
                   SELECT fl.sha256 FROM file_locations fl
                   WHERE fl.path=s.path AND fl.file_kind='ols'
                     AND fl.is_present=1 AND fl.sha256 IS NOT NULL
                   ORDER BY fl.id DESC LIMIT 1
               ) AS live_sha256,
               COALESCE((
                   SELECT fl.sha256 FROM file_locations fl
                   WHERE fl.path=s.path AND fl.file_kind='ols'
                     AND fl.is_present=1 AND fl.sha256 IS NOT NULL
                   ORDER BY fl.id DESC LIMIT 1
               ),NULLIF(s.sha256,'')) AS sha256,s.size_bytes,
               d.processing_status,d.reference_status,
               d.metrics_json,d.errors_json,d.warnings_json,
               (SELECT COUNT(*) FROM project_versions v WHERE v.project_id=p.id) version_count,
               (SELECT COUNT(*) FROM project_version_slots vs WHERE vs.project_id=p.id) slot_count,
               (SELECT COUNT(*) FROM map_definitions m WHERE m.project_id=p.id) map_count,
               (SELECT COUNT(*) FROM map_axes a JOIN map_definitions m ON m.id=a.map_id
                WHERE m.project_id=p.id) axis_count
        FROM projects p
        JOIN source_files s ON s.id=p.source_id
        LEFT JOIN project_diagnostics d ON d.project_id=p.id
        {where}
        {order_clause}
        LIMIT ? OFFSET ?
        """,
            parameters,
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise TimeoutError(
                "Project search exceeded 5 seconds; refine the search filters."
            ) from exc
        raise
    finally:
        search_connection.set_progress_handler(None, 0)
    has_more = len(rows_found) > limit
    rows_found = rows_found[:limit]
    project_ids = [int(row["id"]) for row in rows_found]
    metadata_by_project: dict[int, dict[str, dict[str, str]]] = {}
    versions_by_project: dict[int, list[dict[str, Any]]] = {}
    slots_by_project: dict[int, list[dict[str, Any]]] = {}
    maps_by_project: dict[int, list[dict[str, Any]]] = {}
    if project_ids:
        for batch_start in range(0, len(project_ids), 400):
            project_batch = project_ids[batch_start:batch_start + 400]
            placeholders = ",".join("?" for _ in project_batch)
            for item in database.connection.execute(
                f"""
                SELECT project_id,field,value,status FROM project_metadata
                WHERE project_id IN ({placeholders}) ORDER BY project_id,field
                """,
                project_batch,
            ):
                metadata_by_project.setdefault(int(item["project_id"]), {})[
                    item["field"]
                ] = {"value": item["value"], "status": item["status"]}
            for item in database.connection.execute(
                f"""
                SELECT id,project_id,name,description,slot_index,status,parent_id,is_root
                FROM project_versions WHERE project_id IN ({placeholders})
                ORDER BY project_id,slot_index
                """,
                project_batch,
            ):
                versions_by_project.setdefault(int(item["project_id"]), []).append(
                    dict(item)
                )
            for item in database.connection.execute(
                f"""
                SELECT s.slot_index,s.project_id,s.version_id,
                       COALESCE(v.name,'') AS name,s.payload_sha256,s.payload_size,
                       s.binary_status,s.name_status
                FROM project_version_slots s
                LEFT JOIN project_versions v ON v.id=s.version_id
                WHERE s.project_id IN ({placeholders})
                ORDER BY s.project_id,s.slot_index
                """,
                project_batch,
            ):
                slots_by_project.setdefault(int(item["project_id"]), []).append(
                    dict(item)
                )
            for item in database.connection.execute(
                f"""
                SELECT m.project_id,m.ordinal,m.name,m.description,m.map_type,
                       m.data_type,m.columns_count,m.rows_count,m.unit,m.status,
                       COUNT(a.id) AS axes_count
                FROM map_definitions m
                LEFT JOIN map_axes a ON a.map_id=m.id
                WHERE m.project_id IN ({placeholders})
                GROUP BY m.id ORDER BY m.project_id,m.ordinal
                """,
                project_batch,
            ):
                maps_by_project.setdefault(int(item["project_id"]), []).append(
                    dict(item)
                )
    results = []
    for row in rows_found:
        project_id = int(row["id"])
        metadata_rows = metadata_by_project.get(project_id, {})
        metadata_values = {
            field: item["value"].strip()
            for field, item in metadata_rows.items()
        }
        versions = versions_by_project.get(project_id, [])
        binary_slots = slots_by_project.get(project_id, [])
        map_summaries = maps_by_project.get(project_id, [])
        missing: list[str] = []
        missing.extend(
            field for field in _METADATA_FIELDS if not metadata_values.get(field)
        )
        if not versions:
            missing.append("project_versions")
        if any(not item["name"] for item in binary_slots):
            missing.append("version_names_or_slot_relations")
        if row["map_count"] == 0:
            missing.append("map_definitions")
        elif row["axis_count"] == 0:
            missing.append("map_axes")
        reference = row["reference_status"] or "not_validated"
        exact_ecu_filters = sum(
            1 for field, value in filters.items()
            if field in _IDENTIFIER_FIELDS
            and value.strip()
            and metadata_values.get(field) == value.strip()
        )
        source_hash_match = bool(
            file_hash and row["live_sha256"]
            and row["live_sha256"].lower() == file_hash.strip().lower()
        )
        parser_snapshot_matches_current = (
            row["live_sha256"] == row["parser_sha256"]
            if row["live_sha256"] and row["parser_sha256"] else None
        )
        if parser_snapshot_matches_current is False:
            missing.append("parser_snapshot_matches_current_source_hash")
        if not row["live_sha256"]:
            missing.append("live_full_file_hash")
        confidence = (
            "LIKELY" if exact_ecu_filters >= 2 else "INCONCLUSIVE"
        )
        match_status = (
            "CONFIRMED" if source_hash_match else confidence
        )
        reasons = [
            {"field": key, "matched_value": value} for key, value in matched_features
        ]
        map_limit = 25
        diagnostics_errors = json.loads(row["errors_json"] or "[]")
        diagnostics_warnings = json.loads(row["warnings_json"] or "[]")
        has_values_reliably_decoded = False
        results.append(
            {
                "project_id": project_id,
                "source": {
                    "path": row["path"],
                    "sha256": row["sha256"],
                    "live_sha256": row["live_sha256"],
                    "parser_snapshot_sha256": row["parser_sha256"],
                    "hash_provenance": (
                        "live_file_location"
                        if row["live_sha256"] else
                        "parser_import"
                        if row["parser_sha256"] else "unavailable"
                    ),
                    "parser_snapshot_matches_current_content":
                        parser_snapshot_matches_current,
                    "size_bytes": row["size_bytes"],
                },
                "format_version": row["format_version"],
                "parser": {
                    "name": row["parser_name"],
                    "version": row["parser_version"],
                    "status": row["processing_status"] or "unknown",
                },
                "reference_validation": reference,
                "versions": versions,
                "binary_slots": binary_slots,
                "metadata": metadata_values,
                "metadata_status": {
                    field: item["status"] for field, item in metadata_rows.items()
                },
                "maps": map_summaries[:map_limit],
                "maps_truncated": len(map_summaries) > map_limit,
                "counts": {
                    "versions": row["version_count"],
                    "binary_slots": row["slot_count"],
                    "maps": row["map_count"],
                    "axes": row["axis_count"],
                },
                "matched_features": reasons,
                "match_reason": (
                    "All supplied filters matched; this ranks retrieval candidates, "
                    "not a technical tune or version relationship."
                ),
                "confidence": confidence,
                "match_status": match_status,
                "missing_information": sorted(set(missing)),
                "parser_errors": diagnostics_errors,
                "parser_warnings": diagnostics_warnings,
                "values_reliably_decoded": has_values_reliably_decoded,
                "uncertainties": [
                    "Parser-established version-to-map relationships are not independent validation.",
                    "Map definitions and dimensions do not prove that values decode correctly in each binary version.",
                    *(
                        ["This project has parser warnings or errors."]
                        if diagnostics_errors or diagnostics_warnings else []
                    ),
                    *(
                        ["Current file content hash differs from the parser-import snapshot; parser metadata may be stale."]
                        if parser_snapshot_matches_current is False else []
                    ),
                    *(
                        ["No live file-location hash is available; only the parser-import hash is known."]
                        if not row["live_sha256"] else []
                    ),
                ],
                "source_record": {"table": "projects", "id": project_id},
            }
        )
    active_ranker = ranker or EvidenceRanker()
    criteria = {
        "metadata": filters,
        "map_name": map_name,
        "columns": columns,
        "rows": rows,
        "version_name": version_name,
        "binary_hash": binary_hash,
        "file_hash": file_hash,
        "format_version": format_version,
        "validation_status": validation_status,
        "parser_status": parser_status,
        "source_path": source_path,
        "file_size": file_size,
        "min_file_size": min_file_size,
        "max_file_size": max_file_size,
        "map_text": map_text,
    }
    for result in results:
        score, rank_reasons = active_ranker.rank(result, criteria)
        result["rank_score"] = score
        result["rank_reasons"] = rank_reasons
    results.sort(
        key=lambda item: (-item["rank_score"], item["project_id"])
    )
    elapsed = time.perf_counter() - started
    if elapsed >= _SLOW_SEARCH_SECONDS:
        _LOGGER.warning(
            "Slow project search: %.3fs limit=%d offset=%d filters=%s",
            elapsed,
            limit,
            offset,
            sorted(key for key, value in criteria.items() if value),
        )
    return {
        "filters": {
            "text": text,
            "metadata": filters,
            "map_name": map_name,
            "columns": columns,
            "rows": rows,
            "version_name": version_name,
            "binary_hash": binary_hash,
            "file_hash": file_hash,
            "format_version": format_version,
            "validation_status": validation_status,
            "parser_status": parser_status,
            "source_path": source_path,
            "file_size": file_size,
            "min_file_size": min_file_size,
            "max_file_size": max_file_size,
            "map_text": map_text,
        },
        "result_count": len(results),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "results": results,
        "ranker": active_ranker.provider_name,
        "rank_score_definition": (
            "Retrieval ordering only: +10 per exact ECU/calibration identifier, "
            "+100 exact complete source SHA-256, +12 parser payload SHA-256, "
            "+4 matching map structure, +4 matching version name, "
            "+5 validated reference, +2 available maps and axes, "
            "+1 fully processed parser status. It is not a confidence probability."
        ),
        "ranking_scope": (
            "whole_filtered_result_set"
            if uses_default_ranker
            else "current_page_only_for_custom_ranker"
        ),
        "query_seconds": round(elapsed, 6),
        "semantic_search": "not_configured",
    }


def parser_coverage(database: Any, *, limit: int = 1000) -> dict[str, Any]:
    if not 1 <= limit <= 10000:
        raise ValueError("Coverage limit must be between 1 and 10000.")
    formats = [
        dict(row)
        for row in database.connection.execute(
            """
            SELECT p.format_version,COUNT(*) AS projects,
                   SUM(CASE WHEN d.processing_status='fully_processed' THEN 1 ELSE 0 END)
                       AS fully_processed,
                   SUM(CASE WHEN d.processing_status='partially_processed' THEN 1 ELSE 0 END)
                       AS partially_processed,
                   SUM(CASE WHEN d.reference_status='validated' THEN 1 ELSE 0 END)
                       AS independently_validated
            FROM projects p LEFT JOIN project_diagnostics d ON d.project_id=p.id
            GROUP BY p.format_version ORDER BY p.format_version
            """
        )
    ]
    current_problems = [
        dict(row)
        for row in database.connection.execute(
            """
            SELECT json_extract(issue.value,'$.code') AS code,
                   'error' AS severity,COUNT(*) AS occurrences,
                   COUNT(DISTINCT d.project_id) AS projects
            FROM project_diagnostics d,json_each(d.errors_json) issue
            GROUP BY code
            UNION ALL
            SELECT json_extract(issue.value,'$.code') AS code,
                   'warning' AS severity,COUNT(*) AS occurrences,
                   COUNT(DISTINCT d.project_id) AS projects
            FROM project_diagnostics d,json_each(d.warnings_json) issue
            GROUP BY code
            ORDER BY severity,projects DESC,code
            """
        )
    ]
    project_rows = database.connection.execute(
        """
        SELECT p.id,p.format_version,s.path,p.parser_name,p.parser_version,
               d.processing_status,d.reference_status,d.metrics_json,
               d.errors_json,d.warnings_json,
               (SELECT COUNT(*) FROM project_versions v WHERE v.project_id=p.id) versions,
               (SELECT COUNT(*) FROM project_version_slots vs WHERE vs.project_id=p.id) slots,
               (SELECT COUNT(*) FROM map_definitions m WHERE m.project_id=p.id) maps,
               (SELECT COUNT(*) FROM map_axes a JOIN map_definitions m ON m.id=a.map_id
                WHERE m.project_id=p.id) axes
        FROM projects p JOIN source_files s ON s.id=p.source_id
        LEFT JOIN project_diagnostics d ON d.project_id=p.id
        ORDER BY p.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    import json
    projects = []
    for row in project_rows:
        errors = json.loads(row["errors_json"] or "[]")
        warnings = json.loads(row["warnings_json"] or "[]")
        metrics = json.loads(row["metrics_json"] or "{}")
        projects.append(
            {
                "project_id": row["id"],
                "path": row["path"],
                "format_version": row["format_version"],
                "parser": f"{row['parser_name']} {row['parser_version']}",
                "status": row["processing_status"] or "unknown",
                "reference_validation": row["reference_status"] or "not_validated",
                "counts": {
                    "named_versions": row["versions"],
                    "binary_slots": row["slots"],
                    "maps": row["maps"],
                    "axes": row["axes"],
                },
                "parser_errors": errors,
                "warnings": warnings,
                "unresolved": {
                    "unlabeled_slots": metrics.get("unlabeled_binary_slots"),
                    "unsupported_elements": metrics.get("unsupported_element_count"),
                    "unsupported_count_status": metrics.get(
                        "unsupported_element_count_status", "not_reported"
                    ),
                },
            }
        )
    return {
        "project_count": database.connection.execute(
            "SELECT COUNT(*) FROM projects"
        ).fetchone()[0],
        "registered_sources": database.list_sources(),
        "formats": formats,
        "current_problem_groups": current_problems,
        "projects": projects,
        "projects_returned": len(projects),
        "truncated": len(projects) == limit,
        "interpretation": (
            "Parser coverage describes stored parser output. It does not imply "
            "independent WinOLS validation or trustworthy map values."
        ),
    }
