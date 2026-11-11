from __future__ import annotations

from typing import Any, Protocol


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
        exact_identifiers = [
            field for field in metadata if field in _IDENTIFIER_FIELDS
        ]
        if exact_identifiers:
            score += len(exact_identifiers) * 10
            reasons.append(
                f"{len(exact_identifiers)} exact ECU/calibration identifier filter(s) matched."
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
        uncertainty = [
            "Search ranking is evidence-based and does not establish a tune relationship.",
            *(
                ["Some project/version/map information is missing."]
                if result["missing_information"] else []
            ),
        ]
        result["ranking_uncertainties"] = uncertainty
        return score, reasons


def search_projects(
    database: Any,
    *,
    text: str = "",
    metadata: dict[str, str] | None = None,
    map_name: str = "",
    columns: int | None = None,
    rows: int | None = None,
    version_name: str = "",
    binary_hash: str = "",
    format_version: int | None = None,
    validation_status: str = "",
    limit: int = 100,
    ranker: SearchRanker | None = None,
) -> dict[str, Any]:
    if not 1 <= limit <= 1000:
        raise ValueError("Search limit must be between 1 and 1000.")
    filters = metadata or {}
    unknown = set(filters) - set(_METADATA_FIELDS)
    if unknown:
        raise ValueError(f"Unsupported metadata filter(s): {sorted(unknown)}")
    if (columns is not None and columns < 0) or (rows is not None and rows < 0):
        raise ValueError("Map dimensions cannot be negative.")
    clauses: list[str] = []
    parameters: list[object] = []
    matched_features: list[tuple[str, str]] = []
    for field, value in filters.items():
        if not value.strip():
            continue
        clauses.append(
            "EXISTS (SELECT 1 FROM project_metadata pm "
            "WHERE pm.project_id=p.id AND pm.field=? AND pm.value=?)"
        )
        parameters.extend((field, value.strip()))
        matched_features.append((field, value.strip()))
    if text.strip():
        clauses.append(
            """
            (
              s.path LIKE ? OR EXISTS (
                SELECT 1 FROM project_metadata pm
                WHERE pm.project_id=p.id AND pm.value LIKE ?
              ) OR EXISTS (
                SELECT 1 FROM map_definitions m
                WHERE m.project_id=p.id AND (m.name LIKE ? OR m.description LIKE ?)
              ) OR EXISTS (
                SELECT 1 FROM project_versions v
                WHERE v.project_id=p.id AND (v.name LIKE ? OR v.description LIKE ?)
              )
            )
            """
        )
        pattern = f"%{text.strip()}%"
        parameters.extend((pattern,) * 6)
        matched_features.append(("text", text.strip()))
    if map_name.strip():
        clauses.append(
            "EXISTS (SELECT 1 FROM map_definitions m WHERE m.project_id=p.id "
            "AND m.name=?)"
        )
        parameters.append(map_name.strip())
        matched_features.append(("map_name", map_name.strip()))
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
            "AND (LOWER(vs.payload_sha256)=LOWER(?) OR LOWER(vs.raw_sha256)=LOWER(?)))"
        )
        parameters.extend((binary_hash.strip(), binary_hash.strip()))
        matched_features.append(("binary_hash", binary_hash.strip().lower()))
    if format_version is not None:
        clauses.append("p.format_version=?")
        parameters.append(format_version)
        matched_features.append(("format_version", str(format_version)))
    if validation_status:
        clauses.append("d.reference_status=?")
        parameters.append(validation_status)
        matched_features.append(("validation_status", validation_status))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.append(limit)
    rows_found = database.connection.execute(
        f"""
        SELECT p.id,p.format_version,p.parser_name,p.parser_version,
               s.path,s.sha256,s.size_bytes,d.processing_status,d.reference_status,
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
        ORDER BY p.id DESC LIMIT ?
        """,
        parameters,
    ).fetchall()
    results = []
    for row in rows_found:
        project_id = row["id"]
        versions = [
            dict(item)
            for item in database.connection.execute(
                """
                SELECT id,name,description,slot_index,status,parent_id,is_root
                FROM project_versions WHERE project_id=? ORDER BY slot_index
                """,
                (project_id,),
            )
        ]
        binary_slots = [
            dict(item)
            for item in database.connection.execute(
                """
                SELECT slot_index,version_id,payload_sha256,payload_size,
                       binary_status,name_status
                FROM project_version_slots WHERE project_id=? ORDER BY slot_index
                """,
                (project_id,),
            )
        ]
        missing: list[str] = []
        metadata_rows = database.connection.execute(
            "SELECT field,value,status FROM project_metadata WHERE project_id=?",
            (project_id,),
        ).fetchall()
        present = {item["field"]: item["value"].strip() for item in metadata_rows}
        missing.extend(field for field in _METADATA_FIELDS if not present.get(field))
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
            if field in _IDENTIFIER_FIELDS and value.strip()
        )
        confidence = "LIKELY" if exact_ecu_filters >= 2 else "INCONCLUSIVE"
        reasons = [
            {"field": key, "matched_value": value} for key, value in matched_features
        ]
        results.append(
            {
                "project_id": project_id,
                "source": {
                    "path": row["path"],
                    "sha256": row["sha256"],
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
                "missing_information": sorted(set(missing)),
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
        "format_version": format_version,
        "validation_status": validation_status,
    }
    for result in results:
        score, rank_reasons = active_ranker.rank(result, criteria)
        result["rank_score"] = score
        result["rank_reasons"] = rank_reasons
    results.sort(
        key=lambda item: (-item["rank_score"], item["project_id"])
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
            "format_version": format_version,
            "validation_status": validation_status,
        },
        "result_count": len(results),
        "results": results,
        "ranker": active_ranker.provider_name,
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
