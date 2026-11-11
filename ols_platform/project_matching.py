from __future__ import annotations

from typing import Any


_IDENTITY_FIELDS = (
    "sw_number",
    "hw_number",
    "hw_code",
    "ecu_type",
    "ecu_manufacturer",
    "engine",
    "make",
    "model",
)
_ECU_ID_FIELDS = ("sw_number", "hw_number", "hw_code", "ecu_type")


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
                   s.payload_sha256,s.payload_size,s.binary_status
            FROM project_version_slots s
            LEFT JOIN project_versions v ON v.id=s.version_id
            WHERE s.project_id=?
            ORDER BY s.slot_index
            """,
            (project_id,),
        )
    ]


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
        SELECT p.id,s.path,s.sha256,s.size_bytes,p.format_version
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
    source_exact = projects[left_id]["sha256"] == projects[right_id]["sha256"]
    if source_exact:
        status = "CONFIRMED"
        basis = "The complete source OLS files have identical SHA-256 hashes."
    elif (
        slot_matches
        and any(field in equal_metadata for field in _ECU_ID_FIELDS)
        and not any(field in conflicting_metadata for field in _ECU_ID_FIELDS)
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

    return {
        "left": projects[left_id],
        "right": projects[right_id],
        "status": status,
        "basis": basis,
        "source_file_exact_match": source_exact,
        "equal_metadata_fields": equal_metadata,
        "conflicting_metadata_fields": conflicting_metadata,
        "parser_slot_payload_matches": slot_matches,
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
        "SELECT sha256 FROM projects p JOIN source_files s ON s.id=p.source_id WHERE p.id=?",
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
                other_source.sha256=? OR EXISTS (
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
            (project_id, source["sha256"], project_id, project_id, limit * 10),
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
