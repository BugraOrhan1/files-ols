from __future__ import annotations

import csv
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .winols_reference import sha256_file


PACKAGE_SCHEMA_VERSION = 1
PACKAGE_TYPE = "winols-reference-package"
STATUSES = {"PASS", "FAIL", "INCONCLUSIVE", "NOT_TESTED"}
RELATION_STATUSES = {"confirmed", "suggested", "unknown"}
REPRESENTATIONS = {"full_binary", "post_marker_payload", "unknown"}
MAX_METADATA_BYTES = 32 * 1024 * 1024


def _read_manifest(path: Path) -> tuple[dict[str, Any], str]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"Package manifest is not a file: {resolved}")
    raw = resolved.read_bytes()
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid package manifest JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Package manifest root must be a JSON object.")
    return manifest, hashlib.sha256(raw).hexdigest()


def _safe_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def make_package_template(database: Any, project_id: int) -> dict[str, Any]:
    diagnostic = database.get_diagnostic(project_id)
    if diagnostic is None:
        raise ValueError(f"No indexed project found with id {project_id}.")
    root = Path(__file__).resolve().parents[1] / "data" / "reference-packets" / str(project_id)
    return {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_type": PACKAGE_TYPE,
        "project_id": project_id,
        "source": {
            "path": diagnostic["source"]["path"],
            "filename": Path(diagnostic["source"]["path"]).name,
            "sha256": diagnostic["source"]["sha256"],
            "size_bytes": diagnostic["source"]["size_bytes"],
            "format_version": diagnostic["format_version"],
        },
        "winols": {"version": "unknown"},
        "export_method": "unknown",
        "export_settings": {
            "binary_format": "unknown",
            "checksum_or_syncblock_options": "unknown",
            "address_range": "unknown",
            "version_selection": "export each version separately",
            "map_address_representation": {
                "parser": "unknown",
                "reference": "unknown",
                "project_base": None,
            },
        },
        "project_relation": {"status": "unknown", "evidence": ""},
        "coverage_complete": False,
        "exported_versions": [],
        "parser_inventory": {
            "versions": diagnostic["version_inventory"]["versions"],
            "slots": diagnostic["version_inventory"]["slots"],
            "maps": diagnostic["map_definitions"],
            "map_count": len(diagnostic["map_definitions"]),
        },
        "artifacts": [],
        "required_files": [
            "One raw binary export for each exported version, stored separately "
            "under exports/versions/.",
            "WinOLS map-list CSV or JSON under exports/maps/.",
            "Any available map details or value exports, stored separately.",
            "This completed manifest with the producing WinOLS version, exact "
            "export settings, sizes, SHA-256 hashes, and version provenance.",
        ],
        "notes": (
            "The original OLS file is referenced by path and hash and must not "
            "be copied over or modified. Add only versions actually exported. "
            "Filenames are not version-name or slot-mapping evidence. Keep "
            "unknown fields unknown; do not strip boundary bytes."
        ),
        "_suggested_package_directory": str(root),
    }


def write_package_template(
    database: Any,
    project_id: int,
    output_directory: Path,
) -> dict[str, str]:
    manifest_path = output_directory / "manifest.json"
    checklist_path = output_directory / "CHECKLIST.txt"
    if manifest_path.exists() or checklist_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite an existing reference package in {output_directory}."
        )
    manifest = make_package_template(database, project_id)
    manifest.pop("_suggested_package_directory", None)
    output_directory.joinpath("exports", "versions").mkdir(parents=True, exist_ok=True)
    output_directory.joinpath("exports", "maps").mkdir(parents=True, exist_ok=True)
    output_directory.joinpath("exports", "additional").mkdir(
        parents=True, exist_ok=True
    )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    checklist_path.write_text(
        "\n".join(
            [
                "WinOLS reference packet checklist",
                "",
                f"Project ID: {project_id}",
                f"Source OLS: {manifest['source']['filename']}",
                f"Source SHA-256: {manifest['source']['sha256']}",
                "",
                "[ ] Confirm the OLS source path and SHA-256 in WinOLS.",
                "[ ] Record the exact WinOLS version and export settings.",
                "[ ] List each version actually exported in exported_versions.",
                "[ ] Export each version to exports/versions/ as an unmodified raw binary.",
                "[ ] Record each export's exact version name, byte size, SHA-256,",
                "    relation evidence, and slot only if independently established.",
                "[ ] Export the WinOLS map list as CSV or JSON to exports/maps/.",
                "[ ] Add available map detail/value exports under exports/additional/.",
                "[ ] Record the address representation; leave it unknown if unclear.",
                "[ ] Do not truncate, normalize, checksum-correct, or strip bytes.",
                "[ ] Run package-inspect, then package-import, version-match,",
                "    map-compare, and package-report.",
                "",
                "No WinOLS export has been generated by this template.",
                "The empty package is NOT_TESTED and is not validation evidence.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "manifest": str(manifest_path.resolve()),
        "checklist": str(checklist_path.resolve()),
    }


def _resolve_artifact(manifest_path: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("Artifact path must be a non-empty string.")
    path = Path(raw_path)
    if not path.is_absolute():
        path = manifest_path.resolve().parent / path
    return path.resolve(strict=True)


def inspect_package(database: Any, manifest_path: Path) -> dict[str, Any]:
    manifest, manifest_hash = _read_manifest(manifest_path)
    errors: list[str] = []
    warnings: list[str] = []
    project_id = manifest.get("project_id")
    if manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        errors.append("Unsupported or missing package schema_version.")
    if manifest.get("package_type") != PACKAGE_TYPE:
        errors.append("Manifest package_type is not a WinOLS reference package.")
    if not isinstance(project_id, int) or project_id <= 0:
        errors.append("project_id must be a positive integer.")
        diagnostic = None
    else:
        diagnostic = database.get_diagnostic(project_id)
        if diagnostic is None:
            errors.append(f"Project {project_id} is not present in the index.")

    source = manifest.get("source")
    source_report: dict[str, Any] = {
        "declared_path": source.get("path") if isinstance(source, dict) else None,
        "declared_sha256": source.get("sha256") if isinstance(source, dict) else None,
        "indexed_sha256": diagnostic["source"]["sha256"] if diagnostic else None,
        "current_sha256": None,
        "status": "NOT_TESTED",
    }
    if not isinstance(source, dict) or not _safe_sha256(source.get("sha256")):
        errors.append("source.sha256 must be a valid SHA-256 digest.")
        source_report["status"] = "FAIL"
    elif diagnostic is not None:
        if source["sha256"].lower() != diagnostic["source"]["sha256"].lower():
            errors.append("Package source hash differs from the indexed OLS project.")
            source_report["status"] = "FAIL"
        else:
            try:
                source_path = Path(diagnostic["source"]["path"])
                source_hash = sha256_file(source_path)
            except OSError as exc:
                errors.append(f"Indexed OLS source is unavailable: {exc}")
                source_report["status"] = "FAIL"
            else:
                source_report["current_sha256"] = source_hash
                if source_hash != diagnostic["source"]["sha256"]:
                    errors.append("OLS source changed since it was indexed.")
                    source_report["status"] = "FAIL"
                elif isinstance(source.get("path"), str) and source.get("path"):
                    declared_size = source.get("size_bytes")
                    if (
                        declared_size is not None
                        and declared_size != diagnostic["source"]["size_bytes"]
                    ):
                        errors.append("Manifest source size differs from the indexed OLS.")
                        source_report["status"] = "FAIL"
                    try:
                        declared_source = Path(source["path"])
                        if not declared_source.is_absolute():
                            declared_source = manifest_path.resolve().parent / declared_source
                        declared_source = declared_source.resolve(strict=True)
                        if declared_source != source_path.resolve(strict=True):
                            warnings.append(
                                "Manifest source path differs from the indexed path; "
                                "the source hash matches."
                            )
                    except OSError:
                        warnings.append(
                            "Manifest source path is unavailable; the indexed source "
                            "was verified by hash."
                        )
                    if source_report["status"] != "FAIL":
                        source_report["status"] = "PASS"

    relation = manifest.get("project_relation", {})
    if not isinstance(relation, dict):
        relation = {}
        errors.append("project_relation must be an object.")
    relation_status = relation.get("status", "unknown")
    if relation_status not in RELATION_STATUSES:
        errors.append("project_relation.status must be confirmed, suggested, or unknown.")
        relation_status = "unknown"
    if relation_status != "confirmed":
        warnings.append("Project identity is not independently confirmed in this packet.")
    relation_evidence = relation.get("evidence")
    if relation_status == "confirmed" and (
        not isinstance(relation_evidence, str) or not relation_evidence.strip()
    ):
        errors.append("Confirmed project_relation requires non-empty evidence.")

    versions = manifest.get("exported_versions", [])
    if not isinstance(versions, list):
        errors.append("exported_versions must be an array.")
        versions = []
    version_by_key: dict[str, dict[str, Any]] = {}
    version_reports = []
    for ordinal, version in enumerate(versions):
        if not isinstance(version, dict):
            errors.append(f"exported_versions[{ordinal}] must be an object.")
            continue
        key = version.get("version_key")
        if not isinstance(key, str) or not key.strip():
            errors.append(f"exported_versions[{ordinal}] requires version_key.")
            continue
        key = key.strip()
        if key in version_by_key:
            errors.append(f"Duplicate version_key: {key}.")
            continue
        version_by_key[key] = version
        name = version.get("version_name")
        version_reports.append(
            {
                "version_key": key,
                "version_name": name if isinstance(name, str) else "",
                "source_slot_index": version.get("source_slot_index"),
                "name_status": (
                    "available"
                    if isinstance(name, str) and name.strip()
                    else "missing"
                ),
                "artifact_count": 0,
            }
        )
        if not isinstance(name, str) or not name.strip():
            warnings.append(f"Version {key} has no exported version name.")
        slot = version.get("source_slot_index")
        if slot is not None and (not isinstance(slot, int) or slot < 0):
            errors.append(f"Version {key} has an invalid source_slot_index.")
        elif (
            slot is not None
            and diagnostic is not None
            and slot
            not in {
                item["slot_index"]
                for item in diagnostic["version_inventory"]["slots"]
            }
        ):
            errors.append(f"Version {key} refers to an unknown binary slot {slot}.")

    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        errors.append("artifacts must be an array.")
        artifacts = []
    artifact_reports: list[dict[str, Any]] = []
    linked_binary_counts: dict[str, int] = {}
    valid_kinds = {
        "binary_version",
        "project_metadata",
        "map_metadata",
        "map_values",
    }
    for ordinal, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            errors.append(f"artifacts[{ordinal}] must be an object.")
            continue
        kind = artifact.get("kind")
        artifact_status = "NOT_TESTED"
        artifact_errors: list[str] = []
        size = None
        actual_hash = None
        resolved_path = None
        if kind not in valid_kinds:
            artifact_errors.append(f"Unsupported package artifact kind: {kind!r}.")
            artifact_status = "FAIL"
        try:
            raw_artifact_path = artifact.get("path")
            if not isinstance(raw_artifact_path, str) or not raw_artifact_path.strip():
                raise ValueError("Artifact path must be a non-empty string.")
            artifact_path = Path(raw_artifact_path)
            if not artifact_path.is_absolute():
                artifact_path = manifest_path.resolve().parent / artifact_path
            resolved_path = artifact_path.resolve()
            if not resolved_path.is_file():
                artifact_errors.append("Artifact file is missing.")
                warnings.append(
                    f"Artifact {ordinal} is not present yet: {resolved_path}"
                )
                artifact_status = "NOT_TESTED"
            else:
                size = resolved_path.stat().st_size
                actual_hash = sha256_file(resolved_path)
                artifact_status = "PASS"
        except (OSError, ValueError) as exc:
            artifact_errors.append(f"Artifact unavailable: {exc}")
            artifact_status = "FAIL" if isinstance(exc, ValueError) else "NOT_TESTED"

        expected_size = artifact.get("size_bytes")
        expected_hash = artifact.get("sha256")
        if resolved_path is not None and artifact_status == "PASS":
            if expected_size is None or expected_hash is None:
                artifact_status = "INCONCLUSIVE"
                warnings.append(
                    f"Artifact {ordinal} has no declared size_bytes and/or sha256."
                )
            elif not isinstance(expected_size, int) or expected_size < 0:
                artifact_status = "FAIL"
                artifact_errors.append("Declared size_bytes is invalid.")
            elif not _safe_sha256(expected_hash):
                artifact_status = "FAIL"
                artifact_errors.append("Declared sha256 is invalid.")
            elif expected_size != size or expected_hash.lower() != actual_hash.lower():
                artifact_status = "FAIL"
                artifact_errors.append("Declared artifact size or SHA-256 does not match.")

        version_key = artifact.get("version_key")
        version_name = artifact.get("version_name")
        artifact_relation = artifact.get("relation_status", "unknown")
        if artifact_relation not in RELATION_STATUSES:
            artifact_status = "FAIL"
            artifact_errors.append("Invalid artifact relation_status.")
        if kind == "binary_version":
            if not isinstance(version_key, str) or version_key not in version_by_key:
                artifact_status = (
                    "FAIL" if artifact_status == "FAIL" else "INCONCLUSIVE"
                )
                artifact_errors.append(
                    "Binary version artifact is not linked to an exported_versions entry."
                )
            else:
                linked_binary_counts[version_key] = (
                    linked_binary_counts.get(version_key, 0) + 1
                )
                if not isinstance(version_name, str) or not version_name.strip():
                    version_name = version_by_key[version_key].get("version_name", "")
                version_report = next(
                    (item for item in version_reports if item["version_key"] == version_key),
                    None,
                )
                if version_report is not None:
                    version_report["artifact_count"] += 1
                if artifact.get("representation", "unknown") not in REPRESENTATIONS:
                    artifact_status = "FAIL"
                    artifact_errors.append("Invalid binary representation.")
                if not isinstance(version_name, str) or not version_name.strip():
                    artifact_status = (
                        "INCONCLUSIVE"
                        if artifact_status != "FAIL"
                        else artifact_status
                    )
        if artifact_status == "FAIL":
            errors.extend(f"Artifact {ordinal}: {item}" for item in artifact_errors)
        artifact_reports.append(
            {
                "artifact_index": ordinal,
                "kind": kind,
                "path": str(resolved_path) if resolved_path else artifact.get("path"),
                "version_key": version_key,
                "version_name": version_name if isinstance(version_name, str) else "",
                "source_slot_index": artifact.get("source_slot_index"),
                "relation_status": artifact.get("relation_status", "unknown"),
                "representation": artifact.get("representation", "unknown"),
                "declared_size_bytes": expected_size,
                "actual_size_bytes": size,
                "declared_sha256": expected_hash,
                "actual_sha256": actual_hash,
                "status": artifact_status,
                "errors": artifact_errors,
            }
        )

    for item in version_reports:
        if item["artifact_count"] != 1:
            warnings.append(
                f"Version {item['version_key']} has {item['artifact_count']} binary exports; "
                "exactly one is expected."
            )
        if item["artifact_count"] == 0:
            item["status"] = "NOT_TESTED"
        elif item["artifact_count"] > 1:
            item["status"] = "INCONCLUSIVE"
        else:
            item["status"] = "NOT_TESTED"

    missing_versions = [
        item["version_key"]
        for item in version_reports
        if item["artifact_count"] == 0
    ]
    if not version_reports:
        warnings.append("No actually exported project versions are listed.")
    winols = manifest.get("winols", {})
    winols_version = (
        winols.get("version", "unknown") if isinstance(winols, dict) else "unknown"
    )
    export_method = manifest.get("export_method", "unknown")
    export_settings = manifest.get("export_settings", {})
    settings_complete = (
        isinstance(winols_version, str)
        and bool(winols_version.strip())
        and winols_version.strip().casefold() != "unknown"
        and isinstance(export_method, str)
        and bool(export_method.strip())
        and export_method.strip().casefold() != "unknown"
        and isinstance(export_settings, dict)
        and all(
            isinstance(export_settings.get(key), str)
            and bool(export_settings[key].strip())
            and export_settings[key].strip().casefold() != "unknown"
            for key in (
                "binary_format",
                "checksum_or_syncblock_options",
                "address_range",
            )
        )
    )
    if not settings_complete:
        warnings.append(
            "WinOLS version, export method, or required export settings are unknown."
        )
    if errors:
        status = "FAIL"
    elif (
        source_report["status"] == "PASS"
        and relation_status == "confirmed"
        and settings_complete
        and version_reports
        and not missing_versions
        and all(item["artifact_count"] == 1 for item in version_reports)
        and all(
            item["status"] == "PASS"
            for item in artifact_reports
        )
        and all(item["name_status"] == "available" for item in version_reports)
    ):
        status = "PASS"
    elif not artifact_reports or all(
        item["status"] == "NOT_TESTED" for item in artifact_reports
    ):
        status = "NOT_TESTED"
    else:
        status = "INCONCLUSIVE"
    return {
        "package_type": PACKAGE_TYPE,
        "package_manifest": str(manifest_path.resolve()),
        "manifest_sha256": manifest_hash,
        "project_id": project_id,
        "status": status,
        "source": source_report,
        "project_relation_status": relation_status,
        "winols_version": winols_version,
        "export_method": export_method,
        "export_settings": export_settings,
        "coverage_complete_declared": manifest.get("coverage_complete", False),
        "counts": {
            "exported_versions_listed": len(version_reports),
            "binary_exports": sum(item["kind"] == "binary_version" for item in artifact_reports),
            "artifacts": len(artifact_reports),
            "parser_map_definitions": (
                len(diagnostic["map_definitions"]) if diagnostic is not None else 0
            ),
            "missing_version_exports": len(missing_versions),
        },
        "versions": version_reports,
        "artifacts": artifact_reports,
        "missing_version_keys": missing_versions,
        "errors": errors,
        "warnings": list(dict.fromkeys(warnings)),
    }


def import_reference_package(database: Any, manifest_path: Path) -> dict[str, Any]:
    from .winols_reference import import_manifest

    report = inspect_package(database, manifest_path)
    if report["status"] == "FAIL":
        raise ValueError(
            "Reference package has blocking validation errors: "
            + "; ".join(report["errors"])
        )
    if report["counts"]["binary_exports"] == 0:
        raise ValueError("Package import requires at least one version binary export.")
    if report["missing_version_keys"]:
        raise ValueError(
            "Package import requires a binary export for each listed version: "
            + ", ".join(report["missing_version_keys"])
        )
    if any(item["status"] == "NOT_TESTED" for item in report["artifacts"]):
        raise ValueError(
            "Package import requires every declared artifact to be present; "
            "inspect the packet and add the missing exports first."
        )
    if any(
        item["status"] != "PASS"
        for item in report["artifacts"]
    ):
        raise ValueError(
            "Package import requires declared sizes and hashes for every artifact."
        )
    imported = import_manifest(database, manifest_path)
    return {
        "status": report["status"],
        "inspection": report,
        "import": imported,
    }


def _compare_range(
    reference_path: Path,
    source_path: Path,
    source_offset: int,
    source_length: int,
    *,
    chunk_size: int,
    max_ranges: int,
) -> dict[str, Any]:
    reference_size = reference_path.stat().st_size
    compared = min(reference_size, source_length)
    differing = 0
    ranges: list[dict[str, int]] = []
    range_start: int | None = None
    range_end = 0
    truncated = False
    with reference_path.open("rb") as reference, source_path.open("rb") as source:
        source.seek(source_offset)
        offset = 0
        while offset < compared:
            size = min(chunk_size, compared - offset)
            left = reference.read(size)
            right = source.read(size)
            if len(left) != size or len(right) != size:
                raise OSError("Short read during reference-package binary comparison.")
            for index, (ref_byte, source_byte) in enumerate(zip(left, right)):
                absolute = offset + index
                if ref_byte != source_byte:
                    differing += 1
                    if range_start is None:
                        range_start = absolute
                    range_end = absolute + 1
                elif range_start is not None:
                    if len(ranges) < max_ranges:
                        ranges.append(
                            {"start": range_start, "end_exclusive": range_end}
                        )
                    else:
                        truncated = True
                    range_start = None
            offset += size
    if range_start is not None:
        if len(ranges) < max_ranges:
            ranges.append({"start": range_start, "end_exclusive": range_end})
        else:
            truncated = True
    same_prefix = compared > 0 and differing == 0
    length_equal = reference_size == source_length
    return {
        "reference_size_bytes": reference_size,
        "source_range_size_bytes": source_length,
        "compared_bytes": compared,
        "length_difference_bytes": reference_size - source_length,
        "exact_full_match": length_equal and same_prefix,
        "exact_partial_match": same_prefix and not length_equal,
        "differing_bytes": differing,
        "changed_ranges": ranges,
        "changed_ranges_truncated": truncated,
        "reference_sha256": sha256_file(reference_path),
        "source_range_sha256": _sha256_region(
            source_path, source_offset, source_length
        ),
    }


def _sha256_region(path: Path, offset: int, length: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        file.seek(offset)
        remaining = length
        while remaining:
            block = file.read(min(1024 * 1024, remaining))
            if not block:
                raise OSError(f"Short read hashing source range in {path}.")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def match_package_versions(
    database: Any,
    manifest_path: Path,
    *,
    chunk_size: int = 1024 * 1024,
    max_ranges: int = 100_000,
) -> dict[str, Any]:
    if chunk_size <= 0 or max_ranges <= 0:
        raise ValueError("chunk_size and max_ranges must be positive.")
    manifest, manifest_hash = _read_manifest(manifest_path)
    inspected = inspect_package(database, manifest_path)
    project_id = manifest.get("project_id")
    if not isinstance(project_id, int):
        raise ValueError("Package project_id must be a positive integer.")
    project = database.get_project(project_id)
    if project is None:
        raise ValueError(f"No indexed project found with id {project_id}.")
    source_path = Path(project["path"])
    source_hash_matches = sha256_file(source_path) == project["sha256"]
    version_by_key = {
        item["version_key"]: item
        for item in manifest.get("exported_versions", [])
        if isinstance(item, dict) and isinstance(item.get("version_key"), str)
    }
    raw_artifacts = manifest.get("artifacts", [])
    version_reports: list[dict[str, Any]] = []
    for ordinal, artifact in enumerate(raw_artifacts):
        if not isinstance(artifact, dict) or artifact.get("kind") != "binary_version":
            continue
        key = artifact.get("version_key")
        version = version_by_key.get(key) if isinstance(key, str) else None
        name = artifact.get("version_name")
        if not isinstance(name, str) or not name.strip():
            name = version.get("version_name", "") if version else ""
        report: dict[str, Any] = {
            "project_id": project_id,
            "version_key": key,
            "version_name": name if isinstance(name, str) else "",
            "source_file": str(source_path),
            "source_sha256": project["sha256"],
            "artifact_path": artifact.get("path"),
            "candidate_slots": [],
            "status": "NOT_TESTED",
            "reason": "",
            "export_settings": manifest.get("export_settings", {}),
            "explicit_source_slot_index": artifact.get(
                "source_slot_index",
                version.get("source_slot_index") if version else None,
            ),
            "explicit_relation_status": artifact.get("relation_status", "unknown"),
            "representation": artifact.get("representation", "unknown"),
        }
        try:
            reference_path = _resolve_artifact(manifest_path, artifact.get("path"))
        except (OSError, ValueError) as exc:
            report["status"] = "NOT_TESTED"
            report["reason"] = f"Binary export unavailable: {exc}"
            version_reports.append(report)
            continue
        export_hash = sha256_file(reference_path)
        export_size = reference_path.stat().st_size
        report["export_size_bytes"] = export_size
        report["export_sha256"] = export_hash
        expected_hash = artifact.get("sha256")
        expected_size = artifact.get("size_bytes")
        artifact_integrity_ok = (
            _safe_sha256(expected_hash)
            and expected_hash.lower() == export_hash.lower()
            and isinstance(expected_size, int)
            and expected_size == export_size
        )
        report["artifact_integrity_status"] = (
            "PASS" if artifact_integrity_ok else "FAIL"
        )
        if not artifact_integrity_ok:
            report["status"] = "FAIL"
            report["reason"] = "Export file differs from its manifest hash or size."
            version_reports.append(report)
            continue
        if not source_hash_matches:
            report["status"] = "FAIL"
            report["reason"] = "Indexed OLS source changed or is unavailable."
            version_reports.append(report)
            continue
        for slot in project["version_slots"]:
            offset = slot["blob_offset"]
            blob_size = slot["blob_size"]
            payload_offset = offset + 4
            payload_size = max(0, blob_size - 4)
            comparisons = {
                "full_slot": _compare_range(
                    reference_path,
                    source_path,
                    offset,
                    blob_size,
                    chunk_size=chunk_size,
                    max_ranges=max_ranges,
                ),
                "post_marker_payload": _compare_range(
                    reference_path,
                    source_path,
                    payload_offset,
                    payload_size,
                    chunk_size=chunk_size,
                    max_ranges=max_ranges,
                ),
            }
            boundary_length = min(export_size, source_path.stat().st_size - payload_offset)
            boundary = None
            if boundary_length > 0:
                boundary = _compare_range(
                    reference_path,
                    source_path,
                    payload_offset,
                    boundary_length,
                    chunk_size=chunk_size,
                    max_ranges=max_ranges,
                )
            report["candidate_slots"].append(
                {
                    "slot_index": slot["slot_index"],
                    "slot_status": slot["binary_status"],
                    "slot_name": slot["name_status"],
                    "parser_version_name": next(
                        (
                            item["name"]
                            for item in project["versions"]
                            if item["id"] == slot["version_id"]
                        ),
                        "",
                    ),
                    "offset": offset,
                    "blob_size_bytes": blob_size,
                    "payload_offset": payload_offset,
                    "payload_size_bytes": payload_size,
                    "comparisons": comparisons,
                    "contiguous_export_range_from_payload": boundary,
                    "boundary_crosses_slot": export_size > payload_size,
                    "trailing_export_bytes": (
                        export_size - payload_size if export_size > payload_size else 0
                    ),
                }
            )
        representation = report["representation"]
        matched = []
        for candidate in report["candidate_slots"]:
            allowed = (
                ("full_slot",)
                if representation == "full_binary"
                else ("post_marker_payload",)
                if representation == "post_marker_payload"
                else ("full_slot", "post_marker_payload")
            )
            if any(
                candidate["comparisons"][item]["exact_full_match"]
                for item in allowed
            ):
                matched.append(candidate["slot_index"])
        report["exact_full_candidate_slots"] = matched
        prefix_candidates = []
        for candidate in report["candidate_slots"]:
            if any(
                comparison["exact_partial_match"]
                for comparison in candidate["comparisons"].values()
            ):
                prefix_candidates.append(candidate["slot_index"])
        report["exact_partial_candidate_slots"] = prefix_candidates
        explicit_slot = report["explicit_source_slot_index"]
        relation_confirmed = (
            inspected["project_relation_status"] == "confirmed"
            and isinstance(
                manifest.get("project_relation", {}).get("evidence"), str
            )
            and bool(manifest.get("project_relation", {}).get("evidence", "").strip())
            and report["explicit_relation_status"] == "confirmed"
            and isinstance(artifact.get("relation_evidence"), str)
            and bool(artifact.get("relation_evidence", "").strip())
            and representation in REPRESENTATIONS - {"unknown"}
            and isinstance(name, str)
            and bool(name.strip())
            and inspected["status"] != "FAIL"
        )
        slot_indices = {item["slot_index"] for item in project["version_slots"]}
        if explicit_slot is not None and explicit_slot not in slot_indices:
            report["status"] = "FAIL"
            report["reason"] = "Explicit source_slot_index is not present in the project."
        elif explicit_slot is not None and explicit_slot not in matched:
            report["status"] = "FAIL"
            report["reason"] = (
                "Explicit slot mapping does not have a full exact match under "
                "the declared representation."
            )
        elif explicit_slot is None:
            report["status"] = "INCONCLUSIVE"
            report["reason"] = (
                "Slot candidates are evidence only; no explicit export-to-slot mapping."
            )
        elif not relation_confirmed:
            report["status"] = "INCONCLUSIVE"
            report["reason"] = (
                "Exact bytes do not confirm the project/version identity or label."
            )
        else:
            report["status"] = "PASS"
            report["reason"] = (
                "Explicit project/version/slot relationship and representation "
                "have an exact full-length binary match."
            )
        version_reports.append(report)

    slot_claimants: dict[int, list[dict[str, Any]]] = {}
    for item in version_reports:
        slot = item["explicit_source_slot_index"]
        if isinstance(slot, int):
            slot_claimants.setdefault(slot, []).append(item)
    for slot, claimants in slot_claimants.items():
        if len(claimants) > 1:
            for item in claimants:
                if item["status"] == "PASS":
                    item["status"] = "INCONCLUSIVE"
                    item["reason"] = (
                        f"Multiple exported versions claim slot {slot}; a shared-slot "
                        "relationship requires independent evidence."
                    )
    counts = {
        status: sum(item["status"] == status for item in version_reports)
        for status in STATUSES
    }
    overall_status = (
        "FAIL"
        if counts["FAIL"]
        else "PASS"
        if version_reports and counts["PASS"] == len(version_reports)
        else "INCONCLUSIVE"
        if version_reports
        else "NOT_TESTED"
    )
    return {
        "run_type": "version_match",
        "project_id": project_id,
        "package_manifest": str(manifest_path.resolve()),
        "manifest_sha256": manifest_hash,
        "source": {
            "path": str(source_path),
            "sha256": project["sha256"],
            "current_hash_matches_index": source_hash_matches,
        },
        "status": overall_status,
        "counts": counts,
        "versions": version_reports,
        "unresolved": [
            "Extra bytes are reported and retained; their semantic role is not inferred.",
            "A prefix match is not a full match and cannot confirm a version relationship.",
            "Distinct versions claiming the same slot are never auto-confirmed.",
        ],
    }


def _load_map_rows(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError(f"Map metadata export exceeds {MAX_METADATA_BYTES} bytes.")
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if not reader.fieldnames:
                raise ValueError(f"Map CSV has no header row: {path}")
            return [dict(row) for row in reader]
    if path.suffix.lower() != ".json":
        raise ValueError("Map metadata must be a CSV or JSON file.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict) and isinstance(value.get("maps"), list):
        rows = value["maps"]
    else:
        raise ValueError("Map JSON root must be an array or an object with a maps array.")
    if not all(isinstance(item, dict) for item in rows):
        raise ValueError("Every map metadata record must be a JSON object.")
    return rows


def _field(record: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _canonical_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _numeric_value(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if text.lower().startswith("0x"):
            return Decimal(int(text, 16))
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _compare_axes(
    parser_axes: list[dict[str, Any]],
    external_axes: Any,
    *,
    address_comparable: bool,
) -> dict[str, Any]:
    if isinstance(external_axes, dict):
        axes = []
        for role, value in external_axes.items():
            if isinstance(value, dict):
                axes.append({"role": role, **value})
    elif isinstance(external_axes, list) and all(
        isinstance(item, dict) for item in external_axes
    ):
        axes = external_axes
    else:
        return {
            "status": "INCONCLUSIVE",
            "parser": parser_axes,
            "reference": external_axes,
            "reason": "External axes do not use a recognized object/list shape.",
        }
    if not parser_axes and not axes:
        return {"status": "PASS", "parser": [], "reference": []}
    external_by_role = {
        str(_field(item, "role", "axis", "axis_role") or "").casefold(): item
        for item in axes
    }
    results = []
    for parser_axis in parser_axes:
        role = str(parser_axis.get("role", "")).casefold()
        external_axis = external_by_role.get(role)
        if external_axis is None:
            results.append(
                {
                    "role": role,
                    "status": "NOT_TESTED",
                    "reason": "No external axis with this role was supplied.",
                }
            )
            continue
        fields = {}
        for field_name, parser_key, external_keys, numeric in (
            ("points", "points", ("points", "count", "size"), True),
            ("data_type", "data_type", ("data_type", "datatype", "type"), False),
            ("unit", "unit", ("unit", "units"), False),
            ("factor", "factor", ("factor", "conversion_factor"), True),
            (
                "conversion_offset",
                "conversion_offset",
                ("conversion_offset", "offset"),
                True,
            ),
            ("identifier", "identifier", ("identifier", "id"), False),
            (
                "description",
                "description",
                ("description", "comment"),
                False,
            ),
        ):
            expected = parser_axis.get(parser_key)
            actual = _field(external_axis, *external_keys)
            if actual is None:
                status = "NOT_TESTED"
            elif numeric:
                expected_number = _numeric_value(expected)
                actual_number = _numeric_value(actual)
                status = (
                    "PASS"
                    if expected_number is not None
                    and expected_number == actual_number
                    else "FAIL"
                )
            else:
                expected_text = _canonical_text(expected)
                actual_text = _canonical_text(actual)
                status = (
                    "PASS"
                    if expected_text is not None
                    and actual_text is not None
                    and expected_text.casefold() == actual_text.casefold()
                    else "FAIL"
                )
            fields[field_name] = {
                "parser": expected,
                "reference": actual,
                "status": status,
            }
        address = _field(external_axis, "address", "start_address", "offset")
        if address is None:
            fields["address"] = {
                "parser": parser_axis.get("address"),
                "reference": None,
                "status": "NOT_TESTED",
            }
        elif not address_comparable:
            fields["address"] = {
                "parser": parser_axis.get("address"),
                "reference": address,
                "status": "INCONCLUSIVE",
                "reason": "Axis address representations are not established.",
            }
        else:
            fields["address"] = {
                "parser": parser_axis.get("address"),
                "reference": address,
                "status": (
                    "PASS"
                    if _numeric_value(parser_axis.get("address")) is not None
                    and _numeric_value(parser_axis.get("address"))
                    == _numeric_value(address)
                    else "FAIL"
                ),
            }
        axis_status = (
            "FAIL"
            if any(item["status"] == "FAIL" for item in fields.values())
            else "PASS"
            if all(item["status"] == "PASS" for item in fields.values())
            else "INCONCLUSIVE"
        )
        results.append(
            {"role": role, "status": axis_status, "fields": fields}
        )
    status = (
        "FAIL"
        if any(item["status"] == "FAIL" for item in results)
        else "PASS"
        if len(results) == len(axes)
        and all(item["status"] == "PASS" for item in results)
        else "INCONCLUSIVE"
    )
    return {
        "status": status,
        "parser": parser_axes,
        "reference": axes,
        "axes": results,
    }


def compare_map_metadata(
    database: Any,
    manifest_path: Path,
) -> dict[str, Any]:
    manifest, manifest_hash = _read_manifest(manifest_path)
    project_id = manifest.get("project_id")
    if not isinstance(project_id, int):
        raise ValueError("Package project_id must be a positive integer.")
    diagnostic = database.get_diagnostic(project_id)
    if diagnostic is None:
        raise ValueError(f"No indexed project found with id {project_id}.")
    parser_maps = diagnostic["map_definitions"]
    address_settings = manifest.get("export_settings", {}).get(
        "map_address_representation", {}
    )
    if not isinstance(address_settings, dict):
        address_settings = {}
    address_comparable = (
        address_settings.get("parser") in {"absolute", "project_relative"}
        and address_settings.get("reference") == address_settings.get("parser")
    )
    artifacts = [
        item
        for item in manifest.get("artifacts", [])
        if isinstance(item, dict) and item.get("kind") == "map_metadata"
    ]
    if not artifacts:
        return {
            "run_type": "map_metadata",
            "project_id": project_id,
            "package_manifest": str(manifest_path.resolve()),
            "manifest_sha256": manifest_hash,
            "status": "NOT_TESTED",
            "parser_map_count": len(parser_maps),
            "external_map_count": None,
            "matches": [],
            "unresolved": ["No external map_metadata artifact is declared."],
            "address_representation": address_settings,
        }

    external_rows: list[dict[str, Any]] = []
    artifact_hashes: list[dict[str, Any]] = []
    parse_errors = []
    for artifact in artifacts:
        try:
            path = _resolve_artifact(manifest_path, artifact.get("path"))
            actual_hash = sha256_file(path)
            if artifact.get("sha256") and actual_hash != artifact["sha256"]:
                raise ValueError("Map metadata hash differs from manifest.")
            if artifact.get("size_bytes") is not None and (
                path.stat().st_size != artifact["size_bytes"]
            ):
                raise ValueError("Map metadata size differs from manifest.")
            external_rows.extend(_load_map_rows(path))
            integrity_status = (
                "PASS"
                if _safe_sha256(artifact.get("sha256"))
                and artifact.get("sha256").lower() == actual_hash.lower()
                and isinstance(artifact.get("size_bytes"), int)
                and artifact.get("size_bytes") == path.stat().st_size
                else "INCONCLUSIVE"
            )
            artifact_hashes.append(
                {
                    "path": str(path),
                    "sha256": actual_hash,
                    "status": integrity_status,
                }
            )
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            parse_errors.append(str(exc))
            artifact_hashes.append(
                {"path": artifact.get("path"), "status": "FAIL", "error": str(exc)}
            )
    if parse_errors:
        report = {
            "run_type": "map_metadata",
            "project_id": project_id,
            "package_manifest": str(manifest_path.resolve()),
            "manifest_sha256": manifest_hash,
            "status": "FAIL",
            "parser_map_count": len(parser_maps),
            "external_map_count": len(external_rows),
            "matches": [],
            "artifact_checks": artifact_hashes,
            "errors": parse_errors,
            "address_representation": address_settings,
        }
        return report

    by_ordinal = {item["ordinal"]: item for item in parser_maps}
    external_by_ordinal: dict[int, dict[str, Any]] = {}
    for record in external_rows:
        ordinal = _field(record, "parser_ordinal", "parser ordinal")
        if ordinal is not None:
            try:
                ordinal_value = int(str(ordinal), 0)
            except ValueError:
                continue
            if ordinal_value in by_ordinal and ordinal_value not in external_by_ordinal:
                external_by_ordinal[ordinal_value] = record

    matches: list[dict[str, Any]] = []
    used_external: set[int] = set()
    for parser_map in parser_maps:
        ordinal = parser_map["ordinal"]
        record = external_by_ordinal.get(ordinal)
        match_basis = "explicit_parser_ordinal" if record else None
        if record is None and parser_map["name"]:
            named = [
                (index, item)
                for index, item in enumerate(external_rows)
                if _canonical_text(_field(item, "name", "map_name", "map name"))
                == parser_map["name"]
            ]
            parser_same_name = sum(
                item["name"] == parser_map["name"] for item in parser_maps
            )
            if len(named) == 1 and parser_same_name == 1:
                index, record = named[0]
                match_basis = "unique_name_candidate"
                used_external.add(index)
        field_comparisons: dict[str, dict[str, Any]] = {}
        if record is not None:
            fields = (
                ("name", parser_map["name"], ("name", "map_name", "map name")),
                (
                    "description",
                    parser_map["description"],
                    ("description", "comment", "map_description"),
                ),
                (
                    "columns",
                    parser_map["columns_count"],
                    ("columns", "column_count", "x_size"),
                ),
                ("rows", parser_map["rows_count"], ("rows", "row_count", "y_size")),
                (
                    "data_type",
                    parser_map["data_type"],
                    ("data_type", "datatype", "type"),
                ),
                (
                    "conversion_factor",
                    parser_map["factor"],
                    ("factor", "conversion_factor"),
                ),
                (
                    "conversion_offset",
                    parser_map["conversion_offset"],
                    ("conversion_offset", "offset_conversion"),
                ),
            )
            for field_name, parser_value, keys in fields:
                reference_value = _field(record, *keys)
                parser_text = _canonical_text(parser_value)
                reference_text = _canonical_text(reference_value)
                if field_name in {
                    "columns",
                    "rows",
                    "conversion_factor",
                    "conversion_offset",
                }:
                    parser_number = _numeric_value(parser_value)
                    reference_number = _numeric_value(reference_value)
                    same = (
                        parser_number is not None
                        and reference_number is not None
                        and parser_number == reference_number
                    )
                elif field_name == "data_type":
                    same = (
                        parser_text is not None
                        and reference_text is not None
                        and parser_text.casefold() == reference_text.casefold()
                    )
                else:
                    same = parser_text == reference_text
                field_comparisons[field_name] = {
                    "parser": parser_value,
                    "reference": reference_value,
                    "status": (
                        "NOT_TESTED"
                        if reference_text is None
                        else "PASS"
                        if same
                        else "FAIL"
                    ),
                }
            ext_address = _field(record, "address", "start_address", "offset")
            if ext_address is None:
                field_comparisons["address"] = {
                    "parser": parser_map["data_offset"],
                    "reference": None,
                    "status": "NOT_TESTED",
                    "reason": "External address was not supplied.",
                }
            elif not address_comparable:
                field_comparisons["address"] = {
                    "parser": parser_map["data_offset"],
                    "reference": ext_address,
                    "status": "INCONCLUSIVE",
                    "reason": (
                        "Address representations are not both explicitly declared "
                        "and equal; no direct address comparison was made."
                    ),
                }
            else:
                field_comparisons["address"] = {
                    "parser": parser_map["data_offset"],
                    "reference": ext_address,
                    "status": (
                        "PASS"
                        if _numeric_value(parser_map["data_offset"])
                        is not None
                        and _numeric_value(parser_map["data_offset"])
                        == _numeric_value(ext_address)
                        else "FAIL"
                    ),
                    "representation": address_settings.get("parser"),
                }
            external_axes = _field(record, "axes", "axis_data")
            field_comparisons["axes"] = _compare_axes(
                parser_map["axes"],
                external_axes,
                address_comparable=address_comparable,
            )
            field_comparisons["map_id"] = {
                "parser": None,
                "parser_ordinal": ordinal,
                "reference": _field(record, "map_id", "id", "element_id"),
                "status": "NOT_TESTED",
                "reason": (
                    "The parser does not expose a comparable WinOLS map ID; "
                    "parser_ordinal is the only explicit join key."
                ),
            }
        else:
            field_comparisons = {
                field_name: {
                    "parser": parser_map.get(parser_key),
                    "reference": None,
                    "status": "NOT_TESTED",
                }
                for field_name, parser_key in (
                    ("name", "name"),
                    ("description", "description"),
                    ("dimensions", "rows_count"),
                    ("address", "data_offset"),
                    ("axes", "axes"),
                    ("data_type", "data_type"),
                    ("conversion", "factor"),
                    ("map_id", "ordinal"),
                )
            }
        matches.append(
            {
                "parser_ordinal": ordinal,
                "parser_name": parser_map["name"],
                "match_basis": match_basis,
                "status": (
                    "NOT_TESTED"
                    if record is None
                    else "INCONCLUSIVE"
                    if match_basis != "explicit_parser_ordinal"
                    else "FAIL"
                    if any(item["status"] == "FAIL" for item in field_comparisons.values())
                    else "INCONCLUSIVE"
                ),
                "fields": field_comparisons,
            }
        )
    stable_matches = all(
        item["match_basis"] == "explicit_parser_ordinal"
        for item in matches
    ) and len(matches) == len(parser_maps)
    any_fail = any(
        item["match_basis"] == "explicit_parser_ordinal"
        and any(field["status"] == "FAIL" for field in item["fields"].values())
        for item in matches
    )
    status = (
        "FAIL"
        if any_fail
        else "PASS"
        if stable_matches
        and matches
        and all(item["status"] == "PASS" for item in artifact_hashes)
        and all(item["status"] == "PASS" for item in matches)
        else "INCONCLUSIVE"
        if matches and external_rows
        else "NOT_TESTED"
    )
    return {
        "run_type": "map_metadata",
        "project_id": project_id,
        "package_manifest": str(manifest_path.resolve()),
        "manifest_sha256": manifest_hash,
        "status": status,
        "parser_map_count": len(parser_maps),
        "external_map_count": len(external_rows),
        "matches": matches,
        "artifact_checks": artifact_hashes,
        "unmatched_external_records": len(external_rows) - len(used_external),
        "address_representation": address_settings,
        "unresolved": [
            "Map names are candidate identifiers unless an explicit parser_ordinal is exported.",
            "Address fields are not compared unless both address representations are declared equal.",
            "Map metadata does not validate numeric map values in a binary version.",
            "Axis structures without a shared verified schema remain inconclusive.",
        ],
    }


def build_package_report(
    database: Any,
    project_id: int,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    if manifest_path is None:
        manifest_path = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "reference-packets"
            / str(project_id)
            / "manifest.json"
        )
    result: dict[str, Any] = {
        "project_id": project_id,
        "parser": database.get_diagnostic(project_id),
        "package_manifest": str(manifest_path),
        "package": None,
        "latest_runs": database.list_reference_package_runs(project_id, limit=20),
        "missing_reference_data": [
            "A complete, hash-verified binary export for each actual project version.",
            "WinOLS map-list CSV or JSON with recorded export settings.",
            "Independent project/version relationship evidence.",
        ],
    }
    if manifest_path.exists() and result["parser"] is not None:
        result["package"] = inspect_package(database, manifest_path)
    else:
        result["package_status"] = "NOT_TESTED"
    return result
