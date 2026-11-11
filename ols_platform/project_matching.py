from __future__ import annotations

from typing import Any


_IDENTITY_FIELDS = (
    "sw_number",
    "hw_number",
    "hw_code",
    "ecu_type",
    "ecu_manufacturer",
    "ecu_family",
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
_ECU_ID_FIELDS = (
    "sw_number",
    "hw_number",
    "hw_code",
    "ecu_type",
    "calibration_number",
    "calibration_id",
)


def _project_metadata(database: Any, project_id: int) -> dict[str, str]:
    return {
        row["field"]: row["value"].strip()
        for row in database.connection.execute(
            "SELECT field,value FROM project_metadata WHERE project_id=?",
            (project_id,),
        )
    }


def _slots(database: Any, project_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in database.connection.execute(
            """
            SELECT s.slot_index,COALESCE(v.name,'') AS name,s.name_status,
                   s.payload_sha256,s.payload_size,s.binary_status,
                   s.version_id,s.blob_offset,s.blob_size
            FROM project_version_slots s
            LEFT JOIN project_versions v ON v.id=s.version_id
            WHERE s.project_id=?
            ORDER BY s.slot_index
            """,
            (project_id,),
        )
    ]


def _project_summary(database: Any, project_id: int) -> dict[str, Any]:
    row = database.connection.execute(
        """
        SELECT p.id,s.path,s.sha256 AS parser_snapshot_sha256,
               (SELECT fl.sha256 FROM file_locations fl
                WHERE fl.path=s.path AND fl.file_kind='ols'
                  AND fl.is_present=1 AND fl.sha256 IS NOT NULL
                ORDER BY fl.id DESC LIMIT 1) AS live_sha256,
               COALESCE((
                   SELECT fl.sha256 FROM file_locations fl
                   WHERE fl.path=s.path AND fl.file_kind='ols'
                     AND fl.is_present=1 AND fl.sha256 IS NOT NULL
                   ORDER BY fl.id DESC LIMIT 1
               ),s.sha256) AS sha256,
               s.size_bytes,p.format_version,p.parser_name,p.parser_version,
               COALESCE(d.processing_status,'partially_processed') AS parser_status,
               COALESCE(d.reference_status,'not_validated') AS reference_status,
               COALESCE(d.error_count,0) AS parser_error_count,
               COALESCE(d.warning_count,0) AS parser_warning_count
        FROM projects p
        JOIN source_files s ON s.id=p.source_id
        LEFT JOIN project_diagnostics d ON d.project_id=p.id
        WHERE p.id=?
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        return {}
    summary = dict(row)
    summary["hash_provenance"] = (
        "live_file_location"
        if row["live_sha256"] else
        "parser_import"
        if row["parser_snapshot_sha256"] else "unavailable"
    )
    summary["parser_snapshot_matches_current_content"] = (
        row["live_sha256"] == row["parser_snapshot_sha256"]
        if row["live_sha256"] and row["parser_snapshot_sha256"] else None
    )
    summary["metadata"] = _project_metadata(database, project_id)
    summary["missing_metadata_fields"] = [
        field for field in _IDENTITY_FIELDS
        if not summary["metadata"].get(field)
    ]
    summary["versions"] = [
        dict(item)
        for item in database.connection.execute(
            """
            SELECT name,description,slot_index,status,parent_id,is_root
            FROM project_versions WHERE project_id=? ORDER BY slot_index
            """,
            (project_id,),
        )
    ]
    summary["binary_slots"] = _slots(database, project_id)
    summary["maps"] = [
        dict(item)
        for item in database.connection.execute(
            """
            SELECT m.name,m.description,m.map_type,m.data_type,m.columns_count,
                   m.rows_count,m.unit,m.version_relation_status,m.status,
                   COUNT(a.id) AS axes_count
            FROM map_definitions m LEFT JOIN map_axes a ON a.map_id=m.id
            WHERE m.project_id=?
            GROUP BY m.id ORDER BY m.ordinal LIMIT 51
            """,
            (project_id,),
        )
    ]
    summary["maps_truncated"] = len(summary["maps"]) > 50
    summary["maps"] = summary["maps"][:50]
    summary["values_reliably_decoded"] = False
    return summary


def _map_signature(item: dict[str, Any]) -> tuple[str, ...] | None:
    if (
        not item.get("name")
        or not item.get("map_type")
        or not item.get("data_type")
        or int(item.get("columns_count") or 0) <= 0
        or int(item.get("rows_count") or 0) <= 0
    ):
        return None
    return tuple(
        str(item.get(field)).strip().casefold()
        for field in (
            "name", "map_type", "data_type", "columns_count",
            "rows_count", "unit",
        )
    )


def _slot_intersections(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    right_by_hash: dict[str, list[dict[str, Any]]] = {}
    for slot in right:
        digest = slot["payload_sha256"]
        if digest:
            right_by_hash.setdefault(digest, []).append(slot)
    matches = []
    for left_slot in left:
        for right_slot in right_by_hash.get(left_slot["payload_sha256"], []):
            matches.append(
                {
                    "left_slot": left_slot["slot_index"],
                    "left_name": left_slot["name"],
                    "right_slot": right_slot["slot_index"],
                    "right_name": right_slot["name"],
                    "payload_sha256": left_slot["payload_sha256"],
                    "payload_size": left_slot["payload_size"],
                    "relation_status": "parser_slot_extent_unverified",
                }
            )
    return matches


def compare_projects(database: Any, left_id: int, right_id: int) -> dict[str, Any]:
    if left_id == right_id:
        raise ValueError("Compare two different project IDs.")
    rows = database.connection.execute(
        """
        SELECT p.id,s.path,
               (SELECT fl.sha256 FROM file_locations fl
                WHERE fl.path=s.path AND fl.file_kind='ols'
                  AND fl.is_present=1 AND fl.sha256 IS NOT NULL
                ORDER BY fl.id DESC LIMIT 1) AS live_sha256,
               COALESCE((
                   SELECT fl.sha256 FROM file_locations fl
                   WHERE fl.path=s.path AND fl.file_kind='ols'
                     AND fl.is_present=1 AND fl.sha256 IS NOT NULL
                   ORDER BY fl.id DESC LIMIT 1
               ),s.sha256) AS sha256,
               s.size_bytes,p.format_version
        FROM projects p JOIN source_files s ON s.id=p.source_id
        WHERE p.id IN (?,?)
        """,
        (left_id, right_id),
    ).fetchall()
    projects = {row["id"]: dict(row) for row in rows}
    if left_id not in projects or right_id not in projects:
        missing = [item for item in (left_id, right_id) if item not in projects]
        raise ValueError(f"No indexed project found for ID(s): {missing}.")

    left_metadata = _project_metadata(database, left_id)
    right_metadata = _project_metadata(database, right_id)
    equal_metadata = [
        field for field in _IDENTITY_FIELDS
        if left_metadata.get(field) and left_metadata.get(field) == right_metadata.get(field)
    ]
    conflicting_metadata = [
        field for field in _IDENTITY_FIELDS
        if left_metadata.get(field) and right_metadata.get(field)
        and left_metadata[field] != right_metadata[field]
    ]
    slot_matches = _slot_intersections(
        _slots(database, left_id),
        _slots(database, right_id),
    )
    left_detail = _project_summary(database, left_id)
    right_detail = _project_summary(database, right_id)
    right_maps = {
        signature: item
        for item in right_detail.get("maps", [])
        if (signature := _map_signature(item)) is not None
    }
    common_maps = [
        {
            "left_map": item,
            "right_map": right_maps[signature],
            "evidence_status": "INCONCLUSIVE",
        }
        for item in left_detail.get("maps", [])
        if (signature := _map_signature(item)) in right_maps
    ][:25]
    right_map_signatures = set(right_maps)
    left_map_signatures = {
        signature for item in left_detail.get("maps", [])
        if (signature := _map_signature(item)) is not None
    }
    left_only_maps = [
        item for item in left_detail.get("maps", [])
        if (signature := _map_signature(item)) is not None
        and signature not in right_map_signatures
    ][:25]
    right_only_maps = [
        item for item in right_detail.get("maps", [])
        if (signature := _map_signature(item)) is not None
        and signature not in left_map_signatures
    ][:25]
    source_exact = bool(
        projects[left_id]["live_sha256"]
        and projects[left_id]["live_sha256"] == projects[right_id]["live_sha256"]
    )
    hard_identity_conflicts = (
        "ecu_type" in conflicting_metadata
        or {
            "hw_number", "hw_code"
        }.issubset(conflicting_metadata)
    )
    if source_exact:
        status = "CONFIRMED"
        basis = "The complete source OLS files have identical SHA-256 hashes."
    elif hard_identity_conflicts:
        status = "REJECTED"
        basis = (
            "Explicit ECU type or hardware identifiers conflict; this candidate "
            "is rejected as the same ECU reference despite other metadata overlap."
        )
    elif (
        slot_matches
        and any(field in equal_metadata for field in _ECU_ID_FIELDS)
        and not any(field in conflicting_metadata for field in _ECU_ID_FIELDS)
        and left_detail["parser_snapshot_matches_current_content"] is True
        and right_detail["parser_snapshot_matches_current_content"] is True
    ):
        status = "LIKELY"
        basis = (
            "At least one parser-reported slot payload hash and ECU identity field "
            "match; slot boundaries and version relationships remain unverified."
        )
    elif slot_matches or equal_metadata:
        status = "INCONCLUSIVE"
        basis = (
            "Some parser-derived binary or metadata evidence agrees, but independent "
            "project/version identity has not been established."
        )
    else:
        status = "INCONCLUSIVE"
        basis = "No sufficient matching evidence was found; absence is not a rejection."
    if (
        not source_exact
        and slot_matches
        and any(field in equal_metadata for field in _ECU_ID_FIELDS)
        and (
            left_detail["parser_snapshot_matches_current_content"] is False
            or right_detail["parser_snapshot_matches_current_content"] is False
        )
    ):
        status = "INCONCLUSIVE"
        basis = (
            "Parser-derived metadata or slot hashes may refer to a different file "
            "revision; current source content differs from a parser snapshot."
        )

    matching_evidence: list[dict[str, Any]] = []
    if source_exact:
        matching_evidence.append({
            "kind": "complete_source_sha256",
            "status": "CONFIRMED",
            "message": "The complete OLS source files have identical content hashes.",
        })
    matching_evidence.extend(
        {
            "kind": "exact_metadata",
            "field": field,
            "value": left_metadata[field],
            "status": "INCONCLUSIVE",
        }
        for field in equal_metadata
    )
    matching_evidence.extend(
        {
            "kind": "conflicting_ecu_identity",
            "field": field,
            "left": left_metadata[field],
            "right": right_metadata[field],
            "status": "REJECTED" if hard_identity_conflicts else "INCONCLUSIVE",
        }
        for field in conflicting_metadata
    )
    matching_evidence.extend(
        {
            "kind": "parser_slot_payload_sha256",
            **item,
            "status": "INCONCLUSIVE",
        }
        for item in slot_matches
    )
    matching_evidence.extend(
        {
            "kind": "parser_map_structure",
            **item,
        }
        for item in common_maps
    )
    return {
        "left": left_detail,
        "right": right_detail,
        "status": status,
        "basis": basis,
        "source_file_exact_match": source_exact,
        "equal_metadata_fields": equal_metadata,
        "conflicting_metadata_fields": conflicting_metadata,
        "parser_slot_payload_matches": slot_matches,
        "common_map_structures": common_maps,
        "matching_evidence": matching_evidence,
        "known_differences": [
            {
                "field": field,
                "left": left_metadata[field],
                "right": right_metadata[field],
            }
            for field in conflicting_metadata
        ] + [
            {"kind": "map_structure_only_left", "map": item}
            for item in left_only_maps
        ] + [
            {"kind": "map_structure_only_right", "map": item}
            for item in right_only_maps
        ],
        "unresolved_uncertainties": [
            "A matching map name and dimensions do not prove equal map semantics or values.",
            "Parser-reported payload hashes do not independently verify binary slot boundaries.",
            "Version names and parser relations are not independent WinOLS validation.",
            *(
                ["One or both map lists were truncated to the first 50 definitions."]
                if left_detail.get("maps_truncated")
                or right_detail.get("maps_truncated") else []
            ),
            *(
                ["At least one project has missing ECU identity fields."]
                if any(
                    not left_metadata.get(field) or not right_metadata.get(field)
                    for field in _ECU_ID_FIELDS
                ) else []
            ),
            *(
                ["At least one live file-location hash differs from its parser-import hash; parser metadata may be stale."]
                if left_detail["parser_snapshot_matches_current_content"] is False
                or right_detail["parser_snapshot_matches_current_content"] is False
                else []
            ),
            *(
                ["One or both complete source files have not yet been re-hashed by the live file-location worker."]
                if left_detail["parser_snapshot_matches_current_content"] is None
                or right_detail["parser_snapshot_matches_current_content"] is None
                else []
            ),
        ],
        "limitations": [
            "Map definitions are not bound to versions by the current parser.",
            "Exact hashes cover parser-reported payload ranges, not independently confirmed WinOLS versions.",
            "CONFIRMED means identical complete OLS source content only; it does not prove a tune relationship.",
        ],
    }


def find_project_matches(
    database: Any,
    project_id: int,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    if not 1 <= limit <= 1000:
        raise ValueError("Project match limit must be between 1 and 1000.")
    source = database.connection.execute(
        """
        SELECT (
            SELECT fl.sha256 FROM file_locations fl
            WHERE fl.path=s.path AND fl.file_kind='ols'
              AND fl.is_present=1 AND fl.sha256 IS NOT NULL
            ORDER BY fl.id DESC LIMIT 1
        ) AS live_sha256
        FROM projects p JOIN source_files s ON s.id=p.source_id
        WHERE p.id=?
        """,
        (project_id,),
    ).fetchone()
    if source is None:
        raise ValueError(f"No indexed project found with id {project_id}.")
    candidate_ids = [
        row["id"]
        for row in database.connection.execute(
            """
            SELECT DISTINCT other.id
            FROM projects other
            JOIN source_files other_source ON other_source.id=other.source_id
            WHERE other.id!=? AND (
                (SELECT fl.sha256 FROM file_locations fl
                 WHERE fl.path=other_source.path AND fl.file_kind='ols'
                   AND fl.is_present=1 AND fl.sha256 IS NOT NULL
                 ORDER BY fl.id DESC LIMIT 1)=? OR EXISTS (
                    SELECT 1
                    FROM project_version_slots a
                    JOIN project_version_slots b
                      ON b.payload_sha256=a.payload_sha256
                    WHERE a.project_id=? AND b.project_id=other.id
                      AND a.payload_sha256!=''
                ) OR EXISTS (
                    SELECT 1 FROM project_metadata a
                    JOIN project_metadata b
                      ON b.field=a.field AND b.value=a.value
                    WHERE a.project_id=? AND b.project_id=other.id
                      AND a.field IN ('sw_number','hw_number','hw_code','ecu_type')
                      AND TRIM(a.value)!=''
                )
            )
            ORDER BY other.id
            LIMIT ?
            """,
            (
                project_id, source["live_sha256"], project_id, project_id,
                limit * 10,
            ),
        )
    ]
    reports = [
        compare_projects(database, project_id, candidate_id)
        for candidate_id in candidate_ids
    ]
    rank = {"CONFIRMED": 0, "LIKELY": 1, "INCONCLUSIVE": 2, "REJECTED": 3}
    reports.sort(
        key=lambda item: (
            rank[item["status"]],
            -len(item["parser_slot_payload_matches"]),
            -len(item["equal_metadata_fields"]),
            item["right"]["id"],
        )
    )
    return {
        "project_id": project_id,
        "candidate_count": len(reports),
        "matches": reports[:limit],
        "candidate_limit_applied": len(candidate_ids) >= limit * 10,
        "matching_is_read_only": True,
    }
