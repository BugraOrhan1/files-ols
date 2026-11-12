from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any


MANIFEST_VERSION = 1
ARTIFACT_KINDS = {
    "source_ols",
    "binary_version",
    "project_metadata",
    "map_metadata",
    "map_values",
}
RELATION_STATUSES = {"confirmed", "suggested", "unknown"}
REPRESENTATIONS = {
    "post_marker_payload",
    "full_binary",
    "map_export",
    "metadata_export",
    "unknown",
}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def make_manifest_template(database: Any, project_id: int) -> dict[str, Any]:
    report = database.get_diagnostic(project_id)
    if report is None:
        raise ValueError(f"No indexed project found with id {project_id}.")
    inventory = report["version_inventory"]
    return {
        "schema_version": MANIFEST_VERSION,
        "project_id": project_id,
        "source": {
            "path": report["source"]["path"],
            "filename": Path(report["source"]["path"]).name,
            "sha256": report["source"]["sha256"],
            "format_version": report["format_version"],
        },
        "winols": {"version": "unknown"},
        "export_method": "unknown",
        "export_settings": {
            "format": "unknown",
            "selected_project_version": "unknown",
            "all_elements_or_active_only": "unknown",
            "address_range": "unknown",
            "checksum_or_syncblock_options": "unknown",
            "custom_representation_or_offset": "unknown",
        },
        "project_relation": {
            "status": "unknown",
            "evidence": "",
        },
        "coverage_complete": False,
        "known_parser_versions": inventory["versions"],
        "known_binary_slots": inventory["slots"],
        "artifacts": [],
        "expected_reference_categories": [
            "project_metadata",
            "binary_version exports with an explicit version/slot mapping",
            "map_metadata and map_values when the project contains maps",
        ],
        "notes": (
            "Set project_relation.status to confirmed only after checking the "
            "source OLS hash and project identity in WinOLS. Filenames alone "
            "do not establish a project/version relationship. Each artifact "
            "must use a path relative to this manifest or an absolute local path."
            " Each artifact's relation_status must independently confirm its "
            "version mapping; confirming the project identity alone is insufficient."
        ),
    }


def write_templates(
    database: Any,
    output_directory: Path,
    project_id: int | None = None,
) -> list[Path]:
    if project_id is None:
        project_ids = [
            row["id"]
            for row in database.connection.execute("SELECT id FROM projects ORDER BY id")
        ]
    else:
        project_ids = [project_id]
    targets = [
        output_directory / str(current_id) / "manifest.json"
        for current_id in project_ids
    ]
    existing = [path for path in targets if path.exists()]
    if existing:
        raise FileExistsError(
            "Refusing to overwrite existing reference manifest(s): "
            + ", ".join(str(path) for path in existing)
        )
    output_paths: list[Path] = []
    for current_id, target in zip(project_ids, targets, strict=True):
        manifest = make_manifest_template(database, current_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        output_paths.append(target)
    return output_paths


def load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    manifest_path = path.resolve()
    raw = manifest_path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid reference manifest JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Reference manifest root must be a JSON object.")
    if payload.get("schema_version") != MANIFEST_VERSION:
        raise ValueError(
            f"Unsupported reference manifest schema: {payload.get('schema_version')!r}."
        )
    if not isinstance(payload.get("project_id"), int) or payload["project_id"] <= 0:
        raise ValueError("Manifest project_id must be a positive integer.")
    source = payload.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("sha256"), str):
        raise ValueError("Manifest source.sha256 is required.")
    if len(source["sha256"]) != 64:
        raise ValueError("Manifest source.sha256 must be a SHA-256 hex digest.")
    try:
        int(source["sha256"], 16)
    except ValueError as exc:
        raise ValueError("Manifest source.sha256 is not valid hexadecimal.") from exc
    artifacts = payload.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise ValueError("Manifest artifacts must be a JSON array.")
    return payload, hashlib.sha256(raw).hexdigest()


def _read_metadata_artifact(path: Path, kind: str) -> tuple[int, list[dict[str, Any]]]:
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError(f"Metadata export exceeds the 32 MiB limit: {path}")
    if path.suffix.lower() == ".csv":
        if kind != "map_metadata":
            raise ValueError("CSV is supported only for map_metadata artifacts.")
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if not reader.fieldnames:
                raise ValueError(f"CSV has no header row: {path}")
            return sum(1 for _ in reader), [{"columns": reader.fieldnames}]
    if path.suffix.lower() != ".json":
        raise ValueError(
            f"{kind} artifacts must be .json"
            + (" or .csv." if kind == "map_metadata" else ".")
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid metadata JSON in {path}: {exc}") from exc
    if isinstance(value, list):
        return len(value), value[:1000]
    if isinstance(value, dict):
        items = value.get("maps") if kind == "map_metadata" else None
        if isinstance(items, list):
            return len(items), items[:1000]
        return 1, [value]
    raise ValueError(f"Unsupported JSON root in {path}; expected object or array.")


def import_manifest(database: Any, manifest_path: Path) -> dict[str, Any]:
    manifest, manifest_sha256 = load_manifest(manifest_path)
    project_id = manifest["project_id"]
    diagnostic = database.get_diagnostic(project_id)
    if diagnostic is None:
        raise ValueError(f"Manifest refers to unknown project id {project_id}.")
    if diagnostic["source"]["sha256"] != manifest["source"]["sha256"]:
        raise ValueError(
            "Manifest source hash does not match the indexed OLS project; "
            "no reference data was imported."
        )
    relation = manifest.get("project_relation", {})
    relation_status = relation.get("status", "unknown")
    if relation_status not in RELATION_STATUSES:
        raise ValueError(f"Unsupported project_relation.status: {relation_status!r}.")
    artifacts = manifest.get("artifacts", [])
    prepared: list[dict[str, Any]] = []
    for ordinal, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise ValueError(f"Artifact {ordinal} must be a JSON object.")
        kind = artifact.get("kind")
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"Unsupported artifact kind in entry {ordinal}: {kind!r}.")
        relative_path = artifact.get("path")
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValueError(f"Artifact {ordinal} requires a path.")
        raw_path = Path(relative_path)
        resolved = raw_path if raw_path.is_absolute() else manifest_path.resolve().parent / raw_path
        resolved = resolved.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError(f"Reference artifact is not a file: {resolved}")
        file_size = resolved.stat().st_size
        file_hash = sha256_file(resolved)
        declared_size = artifact.get("size_bytes")
        declared_hash = artifact.get("sha256")
        if declared_size is not None and declared_size != file_size:
            raise ValueError(f"Artifact size mismatch for {resolved}.")
        if declared_hash is not None and declared_hash != file_hash:
            raise ValueError(f"Artifact SHA-256 mismatch for {resolved}.")
        version_name = artifact.get("version_name")
        slot_index = artifact.get("source_slot_index")
        if slot_index is not None and (
            not isinstance(slot_index, int) or slot_index < 0
        ):
            raise ValueError(f"Artifact {ordinal} has an invalid source_slot_index.")
        item_relation = artifact.get("relation_status", "unknown")
        if item_relation not in RELATION_STATUSES:
            raise ValueError(
                f"Artifact {ordinal} has unsupported relation_status {item_relation!r}."
            )
        representation = artifact.get("representation", "unknown")
        if representation not in REPRESENTATIONS:
            raise ValueError(
                f"Artifact {ordinal} has unsupported representation {representation!r}."
            )
        export_settings = artifact.get(
            "export_settings", manifest.get("export_settings", {})
        )
        if not isinstance(export_settings, dict):
            raise ValueError(f"Artifact {ordinal} export_settings must be an object.")
        user_metadata = artifact.get("metadata", {})
        if not isinstance(user_metadata, dict):
            raise ValueError(f"Artifact {ordinal} metadata must be an object.")
        metadata: dict[str, Any] = dict(user_metadata)
        version_key = artifact.get("version_key")
        versions = manifest.get("exported_versions", [])
        version_record = next(
            (
                item
                for item in versions
                if isinstance(item, dict)
                and item.get("version_key") == version_key
            ),
            {},
        ) if isinstance(versions, list) else {}
        metadata.update(
            {
                "element_id": artifact.get("element_id"),
                "version_key": artifact.get("version_key"),
                "filename_label": artifact.get(
                    "filename_label", user_metadata.get("filename_label")
                ),
                "memory_region": artifact.get("memory_region"),
                "coverage_complete_declared": manifest.get("coverage_complete", False),
                "manifest_project_relation_status": relation_status,
                "manifest_project_relation_evidence": relation.get("evidence", ""),
                "project_identity": manifest.get("project_identity", {}),
                "project_manual_review": manifest.get("manual_review", {}),
                "artifact_source": artifact.get("artifact_source", "unknown"),
                "manual_review": artifact.get("manual_review", {}),
                "export_settings": export_settings,
                "export_method": artifact.get(
                    "export_method", manifest.get("export_method", "unknown")
                ),
                "winols_version": artifact.get(
                    "winols_version",
                    manifest.get("winols", {}).get("version", "unknown"),
                ),
                "relation_evidence": artifact.get(
                    "relation_evidence",
                    relation.get("evidence", ""),
                ),
                "version_relation_status": version_record.get(
                    "relation_status", "unknown"
                ),
                "version_relation_evidence": version_record.get(
                    "relation_evidence", ""
                ),
            }
        )
        record_count = None
        records: list[dict[str, Any]] = []
        if kind in {"project_metadata", "map_metadata", "map_values"}:
            record_count, records = _read_metadata_artifact(resolved, kind)
            metadata["record_count"] = record_count
            metadata["records_preview"] = records
        prepared.append(
            {
                "ordinal": ordinal,
                "kind": kind,
                "path": str(resolved),
                "sha256": file_hash,
                "size_bytes": file_size,
                "version_name": str(version_name) if version_name is not None else "",
                "source_slot_index": slot_index,
                "relation_status": item_relation,
                "representation": representation,
                "metadata": metadata,
                "record_count": record_count,
            }
        )

    imported: list[dict[str, Any]] = []
    for artifact in prepared:
        artifact_id, created = database.store_reference_artifact(
            project_id=project_id,
            manifest_path=str(manifest_path.resolve()),
            manifest_sha256=manifest_sha256,
            ordinal=artifact["ordinal"],
            kind=artifact["kind"],
            path=artifact["path"],
            sha256=artifact["sha256"],
            size_bytes=artifact["size_bytes"],
            version_name=artifact["version_name"],
            source_slot_index=artifact["source_slot_index"],
            relation_status=artifact["relation_status"],
            representation=artifact["representation"],
            metadata=artifact["metadata"],
        )
        imported.append(
            {
                "artifact_id": artifact_id,
                "kind": artifact["kind"],
                "path": artifact["path"],
                "sha256": artifact["sha256"],
                "size_bytes": artifact["size_bytes"],
                "metadata_records": artifact["record_count"],
                "status": created,
            }
        )
    return {
        "project_id": project_id,
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": manifest_sha256,
        "artifacts": imported,
        "coverage_complete_declared": bool(manifest.get("coverage_complete", False)),
        "project_relation_status": relation_status,
    }


def _compare_to_slot(
    reference_path: Path,
    source_path: Path,
    source_offset: int,
    source_length: int,
    *,
    chunk_size: int = 1024 * 1024,
    max_ranges: int = 100_000,
) -> dict[str, Any]:
    if chunk_size <= 0 or max_ranges <= 0:
        raise ValueError("chunk_size and max_ranges must be positive.")
    reference_size = reference_path.stat().st_size
    compared_size = min(reference_size, source_length)
    differing_bytes = 0
    ranges: list[dict[str, int]] = []
    range_start: int | None = None
    range_end = 0
    with reference_path.open("rb") as reference, source_path.open("rb") as source:
        source.seek(source_offset)
        offset = 0
        while offset < compared_size:
            count = min(chunk_size, compared_size - offset)
            ref_chunk = reference.read(count)
            src_chunk = source.read(count)
            if len(ref_chunk) != count or len(src_chunk) != count:
                raise OSError("Short read while comparing reference and source ranges.")
            for local_index, (left, right) in enumerate(zip(ref_chunk, src_chunk)):
                absolute = offset + local_index
                if left != right:
                    differing_bytes += 1
                    if range_start is None:
                        range_start = absolute
                    range_end = absolute + 1
                elif range_start is not None:
                    if len(ranges) < max_ranges:
                        ranges.append(
                            {"start": range_start, "end_exclusive": range_end}
                        )
                    range_start = None
            offset += count
    if range_start is not None and len(ranges) < max_ranges:
        ranges.append({"start": range_start, "end_exclusive": range_end})
    return {
        "reference_size": reference_size,
        "source_range_size": source_length,
        "compared_bytes": compared_size,
        "length_equal": reference_size == source_length,
        "differing_bytes_in_common_range": differing_bytes,
        "changed_ranges": ranges,
        "changed_ranges_truncated": len(ranges) >= max_ranges,
        "reference_sha256": sha256_file(reference_path),
        "source_range_sha256": _sha256_file_region(
            source_path, source_offset, source_length
        ),
    }


def _sha256_file_region(path: Path, offset: int, length: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        file.seek(offset)
        remaining = length
        while remaining:
            chunk = file.read(min(1024 * 1024, remaining))
            if not chunk:
                raise OSError("Short read while hashing source binary range.")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def validate_project(database: Any, project_id: int) -> dict[str, Any]:
    project = database.get_project(project_id)
    if project is None:
        raise ValueError(f"No indexed project found with id {project_id}.")
    artifacts = database.list_reference_artifacts(project_id)
    try:
        current_source_sha256 = sha256_file(Path(project["path"]))
    except OSError as exc:
        source_error = f"{type(exc).__name__}: {exc}"
    else:
        source_error = (
            "Source OLS changed since indexing."
            if current_source_sha256 != project["sha256"]
            else ""
        )
    checks: list[dict[str, Any]] = []
    project_relation_confirmed = any(
        item["metadata"].get("manifest_project_relation_status") == "confirmed"
        for item in artifacts
    )
    for artifact in artifacts:
        status = "INCONCLUSIVE"
        message = "Artifact is imported, but its source/version linkage is unconfirmed."
        comparison: dict[str, Any] | None = None
        try:
            current_artifact_sha256 = sha256_file(Path(artifact["path"]))
        except OSError as exc:
            status = "FAIL"
            message = (
                f"Reference artifact is unavailable: {type(exc).__name__}: {exc}"
            )
        else:
            if source_error:
                status = "FAIL"
                message = f"Cannot validate against the indexed source: {source_error}"
            elif current_artifact_sha256 != artifact["sha256"]:
                status = "FAIL"
                message = "Reference artifact changed since it was imported."
        if status == "FAIL":
            pass
        elif artifact["kind"] == "binary_version":
            if artifact["source_slot_index"] is None:
                candidate_slots: list[dict[str, Any]] = []
                reference_path = Path(artifact["path"])
                if artifact["relation_status"] == "confirmed":
                    try:
                        reference_size = reference_path.stat().st_size
                        for slot in project["version_slots"]:
                            source_length = slot["blob_size"] - 4
                            if reference_size != source_length + 4:
                                continue
                            candidate_comparison = _compare_to_slot(
                                reference_path,
                                Path(project["path"]),
                                slot["blob_offset"] + 4,
                                source_length,
                            )
                            if candidate_comparison[
                                "differing_bytes_in_common_range"
                            ] == 0:
                                candidate_slots.append(
                                    {
                                        "slot_index": slot["slot_index"],
                                        "comparison": candidate_comparison,
                                        "unexplained_trailing_bytes": {
                                            "size_bytes": 4,
                                            "sha256": _sha256_file_region(
                                                reference_path,
                                                source_length,
                                                4,
                                            ),
                                        },
                                    }
                                )
                    except OSError as exc:
                        status = "FAIL"
                        message = (
                            f"Candidate binary comparison failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                    else:
                        comparison = {"candidate_slots": candidate_slots}
                        if len(candidate_slots) == 1:
                            candidate_index = candidate_slots[0]["slot_index"]
                            message = (
                                f"Slot {candidate_index} is a byte-exact candidate "
                                "when comparing the export prefix with the "
                                "post-marker payload; four trailing export bytes "
                                "remain unexplained. No normalization or slot "
                                "mapping was applied."
                            )
                        elif candidate_slots:
                            message = (
                                f"{len(candidate_slots)} slots are byte-exact "
                                "candidates after comparing the export prefix with "
                                "post-marker payloads; four trailing export bytes "
                                "remain unexplained. No normalization or slot "
                                "mapping was applied."
                            )
                        else:
                            message = (
                                "No candidate slot matched the export prefix with "
                                "exactly four trailing export bytes; no normalization "
                                "or slot mapping was applied."
                            )
                else:
                    message = (
                        "No explicit OLS binary slot mapping is present, and the "
                        "project/artifact relation is not confirmed."
                    )
            else:
                slot = next(
                    (
                        item
                        for item in project["version_slots"]
                        if item["slot_index"] == artifact["source_slot_index"]
                    ),
                    None,
                )
                if slot is None:
                    status = "FAIL"
                    message = "Manifest maps to a slot absent from the imported project."
                else:
                    try:
                        comparison = _compare_to_slot(
                            Path(artifact["path"]),
                            Path(project["path"]),
                            slot["blob_offset"] + 4,
                            slot["blob_size"] - 4,
                        )
                    except OSError as exc:
                        status = "FAIL"
                        message = f"Binary comparison failed: {type(exc).__name__}: {exc}"
                    else:
                        if not project_relation_confirmed or artifact["relation_status"] != "confirmed":
                            message = (
                                "Byte comparison recorded for investigation, but "
                                "project/version linkage is not explicitly confirmed."
                            )
                        elif artifact["representation"] != "post_marker_payload":
                            message = (
                                "Byte comparison recorded for investigation, but "
                                "reference representation is not declared as the "
                                "parser's post-marker payload; no normalization was applied."
                            )
                        elif not comparison["length_equal"]:
                            status = "FAIL"
                            message = (
                                "Binary length differs for the explicitly mapped "
                                "post-marker payload representation."
                            )
                        elif comparison["differing_bytes_in_common_range"]:
                            status = "FAIL"
                            message = (
                                f"{comparison['differing_bytes_in_common_range']} bytes "
                                "differ in the explicitly mapped representation."
                            )
                        else:
                            status = "PASS"
                            message = "Exact bytes and length match the mapped parser payload."
        elif artifact["kind"] in {"map_metadata", "map_values", "project_metadata"}:
            if project_relation_confirmed:
                message = (
                    "Structured reference was imported and hashed; semantic field "
                    "comparison is not implemented for this export schema."
                )
            else:
                message = "Structured reference imported; project linkage is unconfirmed."
        elif artifact["kind"] == "source_ols":
            message = (
                "Source OLS is retained as provenance, not an independent WinOLS "
                "reference export."
            )
        check = {
            "artifact_id": artifact["id"],
            "kind": artifact["kind"],
            "path": artifact["path"],
            "version_name": artifact["version_name"],
            "source_slot_index": artifact["source_slot_index"],
            "relation_status": artifact["relation_status"],
            "representation": artifact["representation"],
            "status": status,
            "message": message,
            "comparison": comparison,
        }
        database.store_reference_check(
            artifact_id=artifact["id"],
            source_sha256=project["sha256"],
            status=status,
            message=message,
            comparison=comparison,
        )
        checks.append(check)
    report = build_project_report(database, project_id, checks=checks)
    return report


def build_project_report(
    database: Any,
    project_id: int,
    *,
    checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    diagnostic = database.get_diagnostic(project_id)
    if diagnostic is None:
        raise ValueError(f"No indexed project found with id {project_id}.")
    artifacts = database.list_reference_artifacts(project_id)
    if checks is None:
        checks = database.list_reference_checks(project_id)
    counts = {status: sum(item["status"] == status for item in checks)
              for status in ("PASS", "FAIL", "INCONCLUSIVE", "NOT_TESTED")}
    source = diagnostic["source"]
    versions = diagnostic["version_inventory"]
    elements = diagnostic["element_counts"]
    exported_slots = {
        item["source_slot_index"]
        for item in artifacts
        if item["kind"] == "binary_version"
        and item["relation_status"] == "confirmed"
        and item["source_slot_index"] is not None
    }
    missing_references = []
    if not artifacts:
        missing_references.append("No reference manifest/artifacts have been imported.")
    coverage_complete_declared = bool(artifacts) and all(
        bool(item.get("metadata", {}).get("coverage_complete_declared", False))
        for item in artifacts
    )
    if not coverage_complete_declared:
        missing_references.append(
            "Reference coverage is not explicitly declared complete by all imported manifests."
        )
    named_slots = {
        item["slot_index"] for item in versions["versions"]
    }
    explicitly_mapped_named_slots = named_slots.intersection(exported_slots)
    if named_slots - explicitly_mapped_named_slots:
        missing_references.append(
            f"No confirmed binary export is explicitly mapped to "
            f"{len(named_slots - explicitly_mapped_named_slots)} of the parser's "
            "named version record(s). Unnamed slots are not presumed to be versions."
        )
    if elements["map_definitions_stored"] and not any(
        item["kind"] in {"map_metadata", "map_values"} for item in artifacts
    ):
        missing_references.append(
            "Independent map metadata/value exports are missing for parser map records."
        )
    if not any(item["kind"] == "project_metadata" for item in artifacts):
        missing_references.append("Independent project metadata export is missing.")
    return {
        "project_id": project_id,
        "source": source,
        "format_version": diagnostic["format_version"],
        "parser": diagnostic["parser"],
        "application_version": diagnostic["application_version"],
        "processing_status": diagnostic["processing_status"],
        "reference_status": diagnostic["reference_validation"]["status"],
        "coverage_complete_declared": coverage_complete_declared,
        "counts": {
            "declared_physical_slots": versions["declared_binary_slots"],
            "enumerated_slots": versions["enumerated_binary_slots"],
            "nonempty_unverified_slots": versions["available_nonempty_unverified_slots"],
            "parser_named_versions": versions["named_version_records"],
            "independently_confirmed_versions": sum(
                item["status"] == "PASS" and item["kind"] == "binary_version"
                for item in checks
            ),
            "parser_map_definitions": elements["map_definitions_stored"],
            "independently_confirmed_map_definitions": 0,
            "validated_axes": 0,
            "imported_map_metadata_records": sum(
                item.get("metadata", {}).get("record_count", 0)
                for item in artifacts
                if item["kind"] == "map_metadata"
            ),
            "comparable_binary_versions": sum(
                item["status"] in {"PASS", "FAIL"}
                for item in checks
                if item["kind"] == "binary_version"
            ),
            "candidate_binary_slot_matches": sum(
                len((item.get("comparison") or {}).get("candidate_slots", []))
                for item in checks
                if item["kind"] == "binary_version"
            ),
            "binary_artifacts_with_unique_candidate_slot": sum(
                len((item.get("comparison") or {}).get("candidate_slots", [])) == 1
                for item in checks
                if item["kind"] == "binary_version"
            ),
            "pass": counts["PASS"],
            "fail": counts["FAIL"],
            "inconclusive": counts["INCONCLUSIVE"],
            "not_tested": counts["NOT_TESTED"],
        },
        "missing_references": missing_references,
        "validation_status": (
            "NOT_TESTED"
            if not artifacts
            else "INCONCLUSIVE"
            if not checks or counts["INCONCLUSIVE"] or counts["NOT_TESTED"]
            else "FAIL"
            if counts["FAIL"]
            else "PARTIAL"
            if missing_references
            else "PASS"
            if counts["PASS"]
            else "INCONCLUSIVE"
        ),
        "unsupported_or_unvalidated": [
            "Map semantic comparison is not implemented for unknown export schemas.",
            "No version is counted as independently confirmed without a PASS binary check.",
            "A filename does not prove a project/version relationship.",
            "Candidate slot matches with unexplained trailing bytes are diagnostic only.",
        ],
        "validation_history": database.list_reference_check_history(
            project_id,
            limit=100,
        ),
        "parser_errors": diagnostic["errors"],
        "parser_warnings": diagnostic["warnings"],
        "artifacts": artifacts,
        "checks": checks,
    }


def build_reference_summary(database: Any) -> dict[str, Any]:
    projects = [
        row["id"]
        for row in database.connection.execute("SELECT id FROM projects ORDER BY id")
    ]
    reports = [build_project_report(database, project_id) for project_id in projects]
    totals: dict[str, int] = {}
    for report in reports:
        for key, value in report["counts"].items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    return {"project_count": len(reports), "totals": totals, "projects": reports}
