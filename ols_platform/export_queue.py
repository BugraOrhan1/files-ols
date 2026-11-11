from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def plan_export_tasks(database: Any) -> dict[str, Any]:
    """Create only reference tasks that can address recorded project uncertainties."""
    projects = database.connection.execute(
        """
        SELECT p.id, s.path, s.sha256, d.processing_status, d.error_count,
               d.warning_count
        FROM projects p
        JOIN source_files s ON s.id=p.source_id
        LEFT JOIN project_diagnostics d ON d.project_id=p.id
        ORDER BY p.id
        """
    ).fetchall()
    created: list[int] = []
    planned: list[dict[str, Any]] = []

    for project in projects:
        project_id = project["id"]
        versions = database.connection.execute(
            """
            SELECT name FROM project_versions
            WHERE project_id=? AND TRIM(name)!=''
            ORDER BY slot_index
            """,
            (project_id,),
        ).fetchall()
        version_names = list(dict.fromkeys(row["name"] for row in versions))
        artifacts = database.connection.execute(
            """
            SELECT a.kind, a.version_name, a.relation_status, c.status
            FROM reference_artifacts a
            LEFT JOIN reference_checks c ON c.artifact_id=a.id
            WHERE a.project_id=?
            """,
            (project_id,),
        ).fetchall()
        binary_artifacts = [row for row in artifacts if row["kind"] == "binary_version"]
        map_metadata_exists = any(row["kind"] == "map_metadata" for row in artifacts)
        tasks: list[dict[str, Any]] = []

        if not binary_artifacts and version_names:
            tasks.append(
                {
                    "reason": "No raw WinOLS binary export is indexed for the named parser versions.",
                    "version_names": version_names,
                    "export_types": ["binary_version"],
                    "uncertainty": (
                        "The parser exposes named records but their binary payloads "
                        "are not independently related to those versions."
                    ),
                    "expected_outcome": (
                        "Compare each separately exported binary with OLS slot candidates; "
                        "confirm only with explicit version provenance and exact representation."
                    ),
                    "priority": (
                        "HIGH_PRIORITY"
                        if project["error_count"] or project["warning_count"]
                        else "NORMAL_PRIORITY"
                    ),
                }
            )
        elif binary_artifacts:
            matched_names = {
                row["version_name"].strip()
                for row in binary_artifacts
                if row["version_name"] and row["relation_status"] == "confirmed"
                and row["status"] == "PASS"
            }
            missing_names = [name for name in version_names if name not in matched_names]
            if missing_names:
                tasks.append(
                    {
                        "reason": (
                            "Existing binary references do not independently confirm "
                            "the named parser-version relationships."
                        ),
                        "version_names": missing_names,
                        "export_types": ["version_provenance"],
                        "uncertainty": (
                            "Existing export bytes or labels are inconclusive; a prefix "
                            "match is not sufficient evidence of a version relationship."
                        ),
                        "expected_outcome": (
                            "Supply the exact WinOLS version selection and export settings "
                            "for existing artifacts, or export only the unresolved named versions."
                        ),
                        "priority": "HIGH_PRIORITY",
                    }
                )

        if not map_metadata_exists:
            map_count = database.connection.execute(
                "SELECT COUNT(*) FROM map_definitions WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            tasks.append(
                {
                    "reason": (
                        "No independent WinOLS map-list export is indexed"
                        + (" despite parser map definitions." if map_count else
                           " and parser output contains no map definitions.")
                    ),
                    "version_names": [],
                    "export_types": ["map_metadata"],
                    "uncertainty": (
                        "The parser's map inventory and any map-to-version relationship "
                        "have not been independently checked."
                    ),
                    "expected_outcome": (
                        "Compare names, dimensions, axes and address representation; "
                        "do not infer numeric map values from metadata."
                    ),
                    "priority": (
                        "HIGH_PRIORITY" if map_count else "NORMAL_PRIORITY"
                    ),
                }
            )

        for task in tasks:
            priority = task["priority"]
            task_id = database.store_export_task(
                project_id=project_id,
                source_path=project["path"],
                source_sha256=project["sha256"],
                reason=task["reason"],
                version_names=task["version_names"],
                export_types=task["export_types"],
                uncertainty=task["uncertainty"],
                expected_outcome=task["expected_outcome"],
                priority=priority,
            )
            created.append(task_id)
            planned.append(
                {
                    "task_id": task_id,
                    "project_id": project_id,
                    "source_path": project["path"],
                    "source_sha256": project["sha256"],
                    "reason": task["reason"],
                    "version_names": task["version_names"],
                    "export_types": task["export_types"],
                    "uncertainty": task["uncertainty"],
                    "expected_outcome": task["expected_outcome"],
                    "priority": priority,
                    "status": "pending",
                }
            )

    return {
        "projects_considered": len(projects),
        "tasks_planned": len(planned),
        "task_ids": created,
        "tasks": planned,
        "queue": database.list_export_tasks(),
    }


def write_manual_export_batch(
    database: Any,
    *,
    output_directory: Path,
    status: str = "pending",
) -> dict[str, Any]:
    tasks = database.list_export_tasks(status=status, limit=5000)
    if not tasks:
        raise ValueError(f"No export tasks with status {status!r} are available.")
    manifest_path = output_directory / "export-batch.json"
    checklist_path = output_directory / "CHECKLIST.txt"
    if manifest_path.exists() or checklist_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite an existing export batch in {output_directory}."
        )
    output_directory.mkdir(parents=True, exist_ok=True)
    batch = {
        "schema_version": 1,
        "batch_type": "winols-manual-export-batch",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "winols_automation": "not_used",
        "tasks": [
            {
                "task_id": task["id"],
                "project_id": task["project_id"],
                "source_path": task["source_path"],
                "source_sha256": task["source_sha256"],
                "priority": task["priority"],
                "status": task["status"],
                "version_names": task["version_names"],
                "export_types": task["export_types"],
                "new_files_required": any(
                    kind in {"binary_version", "map_metadata", "map_values", "project_metadata"}
                    for kind in task["export_types"]
                ),
                "reason": task["reason"],
                "uncertainty": task["uncertainty"],
                "expected_outcome": task["expected_outcome"],
            }
            for task in tasks
        ],
        "instructions": [
            "Open the exact source OLS path and verify its recorded SHA-256.",
            "Select only the listed named version(s); do not infer names for unresolved slots.",
            "Record WinOLS version, selected version, export format and all export settings.",
            "Keep each version export separate and preserve all bytes.",
            "Record file size, SHA-256, and evidence for any version-to-slot relation.",
            "For version_provenance-only tasks, complete the existing manifest mapping; do not re-export unchanged binaries solely to duplicate data.",
            "Do not treat a filename, prefix match, or map list as version/value validation.",
        ],
    }
    manifest_path.write_text(
        json.dumps(batch, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    checklist = [
        "WinOLS manual export batch",
        "",
        f"Created: {batch['created_at']}",
        f"Tasks: {len(tasks)}",
        "Automation: none; use the installed WinOLS interface manually.",
        "",
    ]
    for task in batch["tasks"]:
        new_file_label = (
            "yes" if task["new_files_required"] else "no; complete existing provenance"
        )
        checklist.extend(
            [
                f"[ ] Task {task['task_id']} — project {task['project_id']} "
                f"({task['priority']})",
                f"    OLS: {task['source_path']}",
                f"    SHA-256: {task['source_sha256']}",
                f"    Export types: {', '.join(task['export_types'])}",
                f"    New export file required: {new_file_label}",
                f"    Versions: {', '.join(task['version_names']) or 'not specified'}",
                f"    Reason: {task['reason']}",
                f"    Uncertainty: {task['uncertainty']}",
                "    Record WinOLS version/settings and exported file hashes.",
                "",
            ]
        )
    checklist.extend(batch["instructions"])
    checklist_path.write_text("\n".join(checklist) + "\n", encoding="utf-8")
    return {
        "manifest": str(manifest_path.resolve()),
        "checklist": str(checklist_path.resolve()),
        "task_count": len(tasks),
        "automation": "not_used",
    }
