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


def _known_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold()
    return bool(normalized) and "unknown" not in normalized and normalized not in {
        "not known",
        "n/a",
    }


def _manual_review_gaps(
    review: object,
    required_fields: set[str] | None = None,
) -> list[str]:
    if not isinstance(review, dict):
        return ["manual_review record is missing."]
    gaps = []
    if review.get("status") != "confirmed":
        gaps.append("manual_review.status is not confirmed.")
    for field in ("reviewer", "reviewed_at", "method", "evidence"):
        if not _known_text(review.get(field)):
            gaps.append(f"manual_review.{field} is unknown.")
    confirmed_fields = review.get("confirmed_fields")
    if not isinstance(confirmed_fields, list) or not all(
        isinstance(item, str) and item.strip() for item in confirmed_fields
    ):
        confirmed_fields = []
    for field in sorted(required_fields or set()):
        if field not in confirmed_fields:
            gaps.append(f"manual_review does not confirm {field}.")
    missing_evidence = review.get("missing_evidence", [])
    if not isinstance(missing_evidence, list):
        gaps.append("manual_review.missing_evidence must be an array.")
    else:
        for item in missing_evidence:
            if not isinstance(item, str) or not item.strip():
                gaps.append(
                    "manual_review.missing_evidence entries must be non-empty strings."
                )
            else:
                gaps.append(f"Missing evidence: {item}")
    return gaps


def _project_review_gaps(manifest: dict[str, Any]) -> list[str]:
    identity = manifest.get("project_identity")
    gaps = []
    if not isinstance(identity, dict):
        gaps.append("project_identity record is missing.")
    else:
        if identity.get("status") != "confirmed":
            gaps.append("WinOLS project identity is not confirmed.")
        if not _known_text(identity.get("project_name")):
            gaps.append("WinOLS project name is unknown.")
        if not _known_text(identity.get("evidence")):
            gaps.append("WinOLS project identity evidence is missing.")

    relation = manifest.get("project_relation")
    if not isinstance(relation, dict) or relation.get("status") != "confirmed":
        gaps.append("OLS-to-WinOLS project relation is not confirmed.")
    elif not _known_text(relation.get("evidence")):
        gaps.append("OLS-to-WinOLS project relation evidence is missing.")

    gaps.extend(
        _manual_review_gaps(
            manifest.get("manual_review"),
            {"project_identity", "project_relation"},
        )
    )
    return gaps


def _artifact_provenance_gaps(
    artifact: dict[str, Any],
    manifest: dict[str, Any],
) -> list[str]:
    gaps = []
    if artifact.get("artifact_source") not in {
        "winols_export",
        "winols_manual_observation",
    }:
        gaps.append(
            "Artifact source must be winols_export or winols_manual_observation."
        )
    method = artifact.get("export_method", manifest.get("export_method"))
    winols = manifest.get("winols")
    default_version = winols.get("version") if isinstance(winols, dict) else None
    version = artifact.get("winols_version", default_version)
    if not _known_text(method):
        gaps.append("Artifact export method is unknown.")
    if not _known_text(version):
        gaps.append("Artifact-producing WinOLS version is unknown.")
    inherited_settings = manifest.get("export_settings")
    settings = dict(inherited_settings) if isinstance(inherited_settings, dict) else {}
    artifact_settings = artifact.get("export_settings")
    if isinstance(artifact_settings, dict):
        settings.update(artifact_settings)
    if not settings:
        gaps.append("Artifact-specific or inherited export settings are missing.")
    if artifact.get("kind") == "binary_version":
        for field in (
            "binary_format",
            "checksum_or_syncblock_options",
            "address_range",
        ):
            if not _known_text(settings.get(field)):
                gaps.append(f"Binary export setting {field} is unknown.")
    return gaps


def _reviewed_fields(review: object) -> set[str]:
    if not isinstance(review, dict):
        return set()
    fields = review.get("confirmed_fields")
    if not isinstance(fields, list):
        return set()
    return {item for item in fields if isinstance(item, str)}


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
        "project_identity": {
            "project_name": "unknown",
            "project_id": "unknown",
            "status": "unknown",
            "evidence": "",
        },
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
        "manual_review": {
            "status": "NOT_TESTED",
            "reviewer": "unknown",
            "reviewed_at": "unknown",
            "method": "unknown",
            "evidence": "",
            "confirmed_fields": [],
            "missing_evidence": [
                "Confirm the WinOLS project identity and its relation to this exact OLS hash.",
                "Review the complete version inventory and every exported artifact relation.",
            ],
        },
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
            "WinOLS map-list CSV or JSON under exports/maps/; include a parser_ordinal "
            "only when the map-to-parser relation was manually checked.",
            "Project metadata as JSON under exports/additional/ using "
            "{\"metadata\": {\"field\": \"value\"}}.",
            "Any available map details or value exports, stored separately.",
            "This completed manifest with the producing WinOLS version, exact "
            "export settings, sizes, SHA-256 hashes, and version provenance.",
            "Artifact source/origin (`winols_export` or "
            "`winols_manual_observation`), export method, WinOLS version, reviewer, "
            "date, evidence, confirmed field paths, and any remaining missing evidence.",
        ],
        "notes": (
            "The original OLS file is referenced by path and hash and must not "
            "be copied over or modified. Add only versions actually exported. "
            "Filenames are not version-name or slot-mapping evidence. Keep "
            "unknown fields unknown; do not strip boundary bytes. Hashes prove "
            "byte identity only, not project identity, export origin, or meaning."
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
                "[ ] Record the WinOLS project name/ID only as shown in WinOLS; "
                "leave unavailable identifiers unknown.",
                "[ ] Record the reviewer, date, method, and evidence for the "
                "project-to-source relation.",
                "[ ] Record the exact WinOLS version and export settings.",
                "[ ] Enumerate every project version in WinOLS; mark "
                "coverage_complete only after checking the whole inventory.",
                "[ ] List each exported version in exported_versions with its "
                "confirmed name, slot, relation status, and relation evidence.",
                "[ ] Export each version to exports/versions/ as an unmodified raw binary.",
                "[ ] Record each export's exact version name, byte size, SHA-256,",
                "    relation evidence, and slot only if independently established.",
                "[ ] For each artifact, record artifact_source. The manifest's "
                "export method, WinOLS version, and settings apply only when the",
                "    artifact has no explicit override.",
                "    Use winols_export or winols_manual_observation; parser output",
                "    cannot be an independent reference.",
                "[ ] Export the WinOLS map list as CSV or JSON to exports/maps/; "
                "record any reviewed map-to-parser ordinal relation.",
                "[ ] Review only fields actually checked in WinOLS and list their "
                "canonical paths in the artifact manual_review.confirmed_fields.",
                "[ ] Add available map detail/value exports under exports/additional/.",
                "[ ] For project metadata JSON, use {\"metadata\": {field: value}} "
                "and review each metadata.<field> path.",
                "[ ] Record the address representation; leave it unknown if unclear.",
                "[ ] Do not truncate, normalize, checksum-correct, or strip bytes.",
                "[ ] Run package-inspect, version-match, metadata-compare,",
                "    map-compare, and package-report. Import is optional and is",
                "    not an independent-validation step.",
                "",
                "No WinOLS export has been generated by this template.",
                "The empty package is NOT_TESTED and is not validation evidence.",
                "A hash verifies bytes only; it does not prove their origin or meaning.",
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
    project_identity = manifest.get("project_identity")
    if not isinstance(project_identity, dict):
        warnings.append("WinOLS project identity details are not recorded.")
    elif project_identity.get("status") != "confirmed":
        warnings.append("WinOLS project identity details are not confirmed.")
    elif not _known_text(project_identity.get("project_name")) or not _known_text(
        project_identity.get("evidence")
    ):
        warnings.append(
            "Confirmed WinOLS project identity requires a known name and evidence."
        )
    project_review_gaps = _project_review_gaps(manifest)

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
        version_relation = version.get("relation_status", "unknown")
        if version_relation not in RELATION_STATUSES:
            errors.append(f"Version {key} has an invalid relation_status.")
            version_relation = "unknown"
        relation_evidence_value = version.get("relation_evidence")
        if version_relation == "confirmed" and not _known_text(
            relation_evidence_value
        ):
            warnings.append(
                f"Version {key} has a confirmed relation without evidence."
            )
        version_reports.append(
            {
                "version_key": key,
                "version_name": name if isinstance(name, str) else "",
                "source_slot_index": version.get("source_slot_index"),
                "relation_status": version_relation,
                "relation_evidence_status": (
                    "available"
                    if _known_text(relation_evidence_value)
                    else "missing"
                ),
                "name_status": (
                    "available"
                    if _known_text(name)
                    else "missing"
                ),
                "artifact_count": 0,
            }
        )
        if not _known_text(name):
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
        artifact_slot = artifact.get("source_slot_index")
        if artifact_slot is not None and (
            not isinstance(artifact_slot, int) or artifact_slot < 0
        ):
            artifact_status = "FAIL"
            artifact_errors.append("Artifact source_slot_index is invalid.")
        if kind == "binary_version":
            if not isinstance(version_key, str) or version_key not in version_by_key:
                artifact_status = (
                    "FAIL" if artifact_status == "FAIL" else "INCONCLUSIVE"
                )
                artifact_errors.append(
                    "Binary version artifact is not linked to an exported_versions entry."
                )
            else:
                linked_version = version_by_key[version_key]
                linked_binary_counts[version_key] = (
                    linked_binary_counts.get(version_key, 0) + 1
                )
                if not _known_text(version_name):
                    version_name = linked_version.get("version_name", "")
                elif _known_text(linked_version.get("version_name")) and (
                    version_name.strip().casefold()
                    != linked_version["version_name"].strip().casefold()
                ):
                    artifact_status = "FAIL"
                    artifact_errors.append(
                        "Artifact version_name conflicts with exported_versions."
                    )
                linked_slot = linked_version.get("source_slot_index")
                if (
                    artifact_slot is not None
                    and linked_slot is not None
                    and artifact_slot != linked_slot
                ):
                    artifact_status = "FAIL"
                    artifact_errors.append(
                        "Artifact source_slot_index conflicts with exported_versions."
                    )
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
        artifact_review_fields = (
            {
                "project_relation",
                "version_relation",
                "version_name",
                "source_slot_index",
                "representation",
            }
            if kind == "binary_version"
            else set()
        )
        artifact_review_gaps = _manual_review_gaps(
            artifact.get("manual_review"),
            artifact_review_fields,
        )
        artifact_provenance_gaps = _artifact_provenance_gaps(artifact, manifest)
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
                "manual_review_status": (
                    "PASS" if not artifact_review_gaps else "INCONCLUSIVE"
                ),
                "manual_review_gaps": artifact_review_gaps,
                "provenance_gaps": artifact_provenance_gaps,
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
        _known_text(winols_version)
        and _known_text(export_method)
        and isinstance(export_settings, dict)
        and all(
            _known_text(export_settings.get(key))
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
    version_evidence_gaps = list(project_review_gaps)
    if manifest.get("coverage_complete") is not True:
        version_evidence_gaps.append(
            "Complete project-version coverage has not been confirmed."
        )
    top_review_gaps = _manual_review_gaps(
        manifest.get("manual_review"),
        {"version_inventory"},
    )
    version_evidence_gaps.extend(top_review_gaps)
    for version in version_reports:
        key = version["version_key"]
        if not _known_text(version["version_name"]):
            version_evidence_gaps.append(f"Version {key} has no confirmed name.")
        if version["source_slot_index"] is None:
            version_evidence_gaps.append(
                f"Version {key} has no confirmed source_slot_index."
            )
        if version["relation_status"] != "confirmed":
            version_evidence_gaps.append(
                f"Version {key} relation to the OLS project is not confirmed."
            )
        elif version["relation_evidence_status"] != "available":
            version_evidence_gaps.append(
                f"Version {key} relation evidence is missing."
            )
        linked_artifacts = [
            item
            for item in artifact_reports
            if item["kind"] == "binary_version"
            and item["version_key"] == key
        ]
        if len(linked_artifacts) != 1:
            version_evidence_gaps.append(
                f"Version {key} does not have exactly one binary export."
            )
        for item in linked_artifacts:
            if item["provenance_gaps"]:
                version_evidence_gaps.extend(item["provenance_gaps"])
            if item["relation_status"] != "confirmed":
                version_evidence_gaps.append(
                    f"Binary export for version {key} is not linked by a confirmed relation."
                )
            if item["manual_review_status"] != "PASS":
                version_evidence_gaps.extend(item["manual_review_gaps"])
            if item["status"] != "PASS":
                version_evidence_gaps.append(
                    f"Binary export for version {key} is not hash/size verified."
                )
    version_evidence_gaps = list(dict.fromkeys(version_evidence_gaps))
    if not version_reports:
        version_evidence_status = "NOT_TESTED"
    elif source_report["status"] == "FAIL" or any(
        item["status"] == "FAIL"
        for item in artifact_reports
        if item["kind"] == "binary_version"
    ):
        version_evidence_status = "FAIL"
    elif version_evidence_gaps:
        version_evidence_status = "INCONCLUSIVE"
    else:
        version_evidence_status = "PASS"
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
        "version_evidence_status": version_evidence_status,
        "version_evidence_gaps": version_evidence_gaps,
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


def audit_reference_package(database: Any, manifest_path: Path) -> dict[str, Any]:
    inspection = inspect_package(database, manifest_path)
    version_comparison = match_package_versions(database, manifest_path)
    metadata_comparison = compare_project_metadata(database, manifest_path)
    map_comparison = compare_map_metadata(database, manifest_path)
    statuses = [
        inspection["status"],
        version_comparison["status"],
        metadata_comparison["status"],
        map_comparison["status"],
    ]
    manifest, manifest_hash = _read_manifest(manifest_path)
    if "FAIL" in statuses:
        status = "FAIL"
    elif all(item == "NOT_TESTED" for item in statuses):
        status = "NOT_TESTED"
    elif (
        all(item == "PASS" for item in statuses)
        and inspection["coverage_complete_declared"] is True
    ):
        status = "PASS"
    else:
        status = "INCONCLUSIVE"
    return {
        "audit_type": "offline_reference_package_audit",
        "audit_version": 1,
        "package_manifest": str(manifest_path.resolve()),
        "manifest_sha256": manifest_hash,
        "project_id": manifest.get("project_id"),
        "status": status,
        "database_writes": False,
        "package_inspection": inspection,
        "version_comparison": version_comparison,
        "metadata_comparison": metadata_comparison,
        "map_comparison": map_comparison,
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
    for artifact in raw_artifacts:
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
                "source_slot_index"
            )
            if artifact.get("source_slot_index") is not None
            else (version.get("source_slot_index") if version else None),
            "explicit_relation_status": artifact.get("relation_status", "unknown"),
            "representation": artifact.get("representation", "unknown"),
            "version_evidence_status": inspected["version_evidence_status"],
            "version_evidence_gaps": inspected["version_evidence_gaps"],
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
        version_relation = (
            version.get("relation_status", "unknown") if version else "unknown"
        )
        relation_confirmed = (
            inspected["project_relation_status"] == "confirmed"
            and _known_text(
                manifest.get("project_relation", {}).get("evidence")
            )
            and version_relation == "confirmed"
            and _known_text(version.get("relation_evidence") if version else None)
            and report["explicit_relation_status"] == "confirmed"
            and _known_text(artifact.get("relation_evidence"))
            and representation in REPRESENTATIONS - {"unknown"}
            and _known_text(name)
            and inspected["version_evidence_status"] == "PASS"
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


def _load_project_metadata(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError(f"Project metadata export exceeds {MAX_METADATA_BYTES} bytes.")
    if path.suffix.lower() != ".json":
        raise ValueError("Project metadata must use the documented JSON format.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("metadata"), dict):
        raise ValueError(
            'Project metadata JSON must contain a "metadata" object.'
        )
    metadata = value["metadata"]
    if not all(isinstance(key, str) for key in metadata):
        raise ValueError("Project metadata keys must be strings.")
    return metadata


def _field(record: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


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


def _compare_values(
    parser_value: Any,
    reference_value: Any,
    *,
    numeric: bool = False,
    case_insensitive: bool = False,
) -> str:
    if reference_value is None:
        return "NOT_COMPARABLE"
    if parser_value is None:
        return "NOT_COMPARABLE"
    if numeric:
        parser_number = _numeric_value(parser_value)
        reference_number = _numeric_value(reference_value)
        if parser_number is None or reference_number is None:
            return "NOT_COMPARABLE"
        return "PASS" if parser_number == reference_number else "FAIL"
    parser_text = str(parser_value).strip()
    reference_text = str(reference_value).strip()
    if case_insensitive:
        return (
            "PASS"
            if parser_text.casefold() == reference_text.casefold()
            else "FAIL"
        )
    return "PASS" if parser_text == reference_text else "FAIL"


def _compare_axes(
    parser_axes: list[dict[str, Any]],
    external_axes: Any,
    *,
    address_comparable: bool,
    confirmed_fields: set[str],
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
            "status": "NOT_COMPARABLE",
            "parser": parser_axes,
            "reference": external_axes,
            "reason": "External axes do not use a recognized object/list shape.",
        }
    if not parser_axes and not axes:
        return {
            "status": (
                "PASS"
                if "maps[*].axes" in confirmed_fields
                else "NOT_COMPARABLE"
            ),
            "parser": [],
            "reference": [],
        }
    external_by_role = {
        str(_field(item, "role", "axis", "axis_role") or "").casefold(): item
        for item in axes
    }
    results = []
    for parser_axis in parser_axes:
        role = str(parser_axis.get("role", "")).casefold()
        external_axis = (
            external_by_role.get(role)
            if role and "maps[*].axes[*].role" in confirmed_fields
            else None
        )
        if external_axis is None:
            results.append(
                {
                    "role": role,
                    "status": "NOT_COMPARABLE",
                    "reason": (
                        "A manually confirmed role mapping to the external axis "
                        "was not supplied."
                    ),
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
            path = f"maps[*].axes[*].{field_name}"
            status = (
                _compare_values(
                    expected,
                    actual,
                    numeric=numeric,
                    case_insensitive=not numeric,
                )
                if path in confirmed_fields
                else "NOT_COMPARABLE"
            )
            fields[field_name] = {
                "parser": expected,
                "reference": actual,
                "status": status,
            }
        address = _field(external_axis, "address", "start_address", "offset")
        if "maps[*].axes[*].address" not in confirmed_fields:
            fields["address"] = {
                "parser": parser_axis.get("address"),
                "reference": address,
                "status": "NOT_COMPARABLE",
                "reason": "Axis address was not independently reviewed.",
            }
        elif address is None:
            fields["address"] = {
                "parser": parser_axis.get("address"),
                "reference": None,
                "status": "NOT_COMPARABLE",
            }
        elif not address_comparable:
            fields["address"] = {
                "parser": parser_axis.get("address"),
                "reference": address,
                "status": "NOT_COMPARABLE",
                "reason": "Axis address representations are not established.",
            }
        else:
            fields["address"] = {
                "parser": parser_axis.get("address"),
                "reference": address,
                "status": _compare_values(
                    parser_axis.get("address"), address, numeric=True
                ),
            }
        axis_status = (
            "FAIL"
            if any(item["status"] == "FAIL" for item in fields.values())
            else "PASS"
            if all(item["status"] == "PASS" for item in fields.values())
            else "NOT_COMPARABLE"
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
        else "NOT_COMPARABLE"
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
    inspection = inspect_package(database, manifest_path)
    project_review_gaps = _project_review_gaps(manifest)
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
            "field_statuses": ["PASS", "FAIL", "NOT_COMPARABLE"],
            "unresolved": ["No external map_metadata artifact is declared."],
            "address_representation": address_settings,
        }

    external_rows: list[dict[str, Any]] = []
    external_row_artifact_indices: dict[int, int] = {}
    external_row_indices: dict[int, int] = {}
    artifact_hashes: list[dict[str, Any]] = []
    artifact_evidence: dict[int, dict[str, Any]] = {}
    parse_errors = []
    for artifact_index, artifact in enumerate(artifacts):
        manual_review = artifact.get("manual_review")
        confirmed_fields = _reviewed_fields(manual_review)
        review_required_fields = {
            "project_relation",
            "map_inventory",
            "maps[*].parser_ordinal",
        }
        version_key = artifact.get("version_key")
        if version_key is not None:
            review_required_fields.add("version_relation")
        review_gaps = _manual_review_gaps(
            manual_review,
            review_required_fields,
        )
        review_gaps.extend(_artifact_provenance_gaps(artifact, manifest))
        if artifact.get("relation_status", "unknown") != "confirmed":
            review_gaps.append("Map artifact relation is not confirmed.")
        if not _known_text(artifact.get("relation_evidence")):
            review_gaps.append("Map artifact relation evidence is missing.")
        if artifact.get("coverage_complete") is not True:
            review_gaps.append("Map inventory coverage is not confirmed complete.")
        if version_key is not None:
            versions = manifest.get("exported_versions", [])
            version = next(
                (
                    item
                    for item in versions
                    if isinstance(item, dict)
                    and item.get("version_key") == version_key
                ),
                None,
            ) if isinstance(versions, list) else None
            if version is None:
                review_gaps.append("Map artifact refers to an unknown version_key.")
            elif (
                version.get("relation_status") != "confirmed"
                or not _known_text(version.get("relation_evidence"))
            ):
                review_gaps.append(
                    "Map artifact's project-version relation is not confirmed."
                )
        try:
            path = _resolve_artifact(manifest_path, artifact.get("path"))
            actual_hash = sha256_file(path)
            if artifact.get("sha256") and actual_hash != artifact["sha256"]:
                raise ValueError("Map metadata hash differs from manifest.")
            if artifact.get("size_bytes") is not None and (
                path.stat().st_size != artifact["size_bytes"]
            ):
                raise ValueError("Map metadata size differs from manifest.")
            rows = _load_map_rows(path)
            row_start = len(external_rows)
            external_rows.extend(rows)
            for row_offset, row in enumerate(rows):
                external_row_artifact_indices[id(row)] = artifact_index
                external_row_indices[id(row)] = row_start + row_offset
            manifest_artifact_index = next(
                index
                for index, item in enumerate(manifest.get("artifacts", []))
                if item is artifact
            )
            matching_inspection = next(
                (
                    item
                    for item in inspection["artifacts"]
                    if item["artifact_index"] == manifest_artifact_index
                ),
                None,
            )
            integrity_status = (
                "PASS"
                if _safe_sha256(artifact.get("sha256"))
                and artifact.get("sha256").lower() == actual_hash.lower()
                and isinstance(artifact.get("size_bytes"), int)
                and artifact.get("size_bytes") == path.stat().st_size
                and matching_inspection is not None
                and matching_inspection["status"] == "PASS"
                else "INCONCLUSIVE"
            )
            if (
                _safe_sha256(artifact.get("sha256"))
                and artifact.get("sha256").lower() != actual_hash.lower()
            ):
                integrity_status = "FAIL"
            if (
                isinstance(artifact.get("size_bytes"), int)
                and artifact.get("size_bytes") != path.stat().st_size
            ):
                integrity_status = "FAIL"
            artifact_hashes.append(
                {
                    "artifact_index": artifact_index,
                    "path": str(path),
                    "sha256": actual_hash,
                    "status": integrity_status,
                    "manual_review_gaps": review_gaps,
                }
            )
            artifact_evidence[artifact_index] = {
                "artifact": artifact,
                "confirmed_fields": confirmed_fields,
                "review_gaps": review_gaps,
                "integrity_status": integrity_status,
            }
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            parse_errors.append(str(exc))
            artifact_hashes.append(
                {
                    "artifact_index": artifact_index,
                    "path": artifact.get("path"),
                    "status": "FAIL",
                    "error": str(exc),
                    "manual_review_gaps": review_gaps,
                }
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
            "field_statuses": ["PASS", "FAIL", "NOT_COMPARABLE"],
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
        if record is not None:
            used_external.add(external_row_indices[id(record)])
        artifact_index = (
            external_row_artifact_indices.get(id(record))
            if record is not None
            else None
        )
        evidence = artifact_evidence.get(artifact_index, {})
        confirmed_fields = evidence.get("confirmed_fields", set())
        artifact_gaps = evidence.get("review_gaps", [])
        comparable_base = (
            record is not None
            and not project_review_gaps
            and not artifact_gaps
            and inspection["source"]["status"] == "PASS"
            and evidence.get("integrity_status") == "PASS"
            and "maps[*].parser_ordinal" in confirmed_fields
        )
        match_basis = (
            "confirmed_parser_ordinal"
            if comparable_base
            else "parser_ordinal_candidate"
            if record is not None
            else None
        )
        field_comparisons: dict[str, dict[str, Any]] = {}
        if record is not None:
            fields = (
                (
                    "name",
                    parser_map["name"],
                    ("name", "map_name", "map name"),
                    "maps[*].name",
                    False,
                ),
                (
                    "description",
                    parser_map["description"],
                    ("description", "comment", "map_description"),
                    "maps[*].description",
                    False,
                ),
                (
                    "columns",
                    parser_map["columns_count"],
                    ("columns", "column_count", "x_size"),
                    "maps[*].columns",
                    True,
                ),
                (
                    "rows",
                    parser_map["rows_count"],
                    ("rows", "row_count", "y_size"),
                    "maps[*].rows",
                    True,
                ),
                (
                    "data_type",
                    parser_map["data_type"],
                    ("data_type", "datatype", "type"),
                    "maps[*].data_type",
                    False,
                ),
                (
                    "conversion_factor",
                    parser_map["factor"],
                    ("factor", "conversion_factor"),
                    "maps[*].conversion_factor",
                    True,
                ),
                (
                    "conversion_offset",
                    parser_map["conversion_offset"],
                    ("conversion_offset", "offset_conversion"),
                    "maps[*].conversion_offset",
                    True,
                ),
            )
            for field_name, parser_value, keys, review_field, numeric in fields:
                reference_value = _field(record, *keys)
                status = (
                    _compare_values(
                        parser_value,
                        reference_value,
                        numeric=numeric,
                        case_insensitive=not numeric,
                    )
                    if comparable_base and review_field in confirmed_fields
                    else "NOT_COMPARABLE"
                )
                field_comparisons[field_name] = {
                    "parser": parser_value,
                    "reference": reference_value,
                    "status": status,
                }
            ext_address = _field(record, "address", "start_address", "offset")
            if not comparable_base or "maps[*].address" not in confirmed_fields:
                field_comparisons["address"] = {
                    "parser": parser_map["data_offset"],
                    "reference": ext_address,
                    "status": "NOT_COMPARABLE",
                    "reason": "Map address relation or field was not independently reviewed.",
                }
            elif ext_address is None:
                field_comparisons["address"] = {
                    "parser": parser_map["data_offset"],
                    "reference": None,
                    "status": "NOT_COMPARABLE",
                    "reason": "External address was not supplied.",
                }
            elif not address_comparable:
                field_comparisons["address"] = {
                    "parser": parser_map["data_offset"],
                    "reference": ext_address,
                    "status": "NOT_COMPARABLE",
                    "reason": (
                        "Address representations are not both explicitly declared "
                        "and equal; no direct address comparison was made."
                    ),
                }
            else:
                field_comparisons["address"] = {
                    "parser": parser_map["data_offset"],
                    "reference": ext_address,
                    "status": _compare_values(
                        parser_map["data_offset"], ext_address, numeric=True
                    ),
                    "representation": address_settings.get("parser"),
                }
            external_axes = _field(record, "axes", "axis_data")
            field_comparisons["axes"] = _compare_axes(
                parser_map["axes"],
                external_axes,
                address_comparable=address_comparable,
                confirmed_fields=confirmed_fields if comparable_base else set(),
            )
            field_comparisons["map_id"] = {
                "parser": None,
                "parser_ordinal": ordinal,
                "reference": _field(record, "map_id", "id", "element_id"),
                "status": "NOT_COMPARABLE",
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
                    "status": "NOT_COMPARABLE",
                }
                for field_name, parser_key in (
                    ("name", "name"),
                    ("description", "description"),
                    ("address", "data_offset"),
                    ("axes", "axes"),
                    ("data_type", "data_type"),
                    ("columns", "columns_count"),
                    ("rows", "rows_count"),
                    ("conversion_factor", "factor"),
                    ("conversion_offset", "conversion_offset"),
                    ("map_id", "ordinal"),
                )
            }
        field_statuses = [
            item.get("status")
            for item in field_comparisons.values()
        ]
        matches.append(
            {
                "parser_ordinal": ordinal,
                "parser_name": parser_map["name"],
                "match_basis": match_basis,
                "status": (
                    "FAIL"
                    if "FAIL" in field_statuses
                    else "PASS"
                    if field_statuses and all(item == "PASS" for item in field_statuses)
                    else "NOT_COMPARABLE"
                ),
                "evidence_gaps": list(
                    dict.fromkeys(
                        project_review_gaps
                        + artifact_gaps
                        + (
                            []
                            if "maps[*].parser_ordinal" in confirmed_fields
                            else ["Map-to-parser ordinal relation is not confirmed."]
                        )
                    )
                ),
                "fields": field_comparisons,
            }
        )
    any_fail = any(item["status"] == "FAIL" for item in matches)
    complete_coverage = all(
        artifact.get("coverage_complete") is True
        and "map_inventory" in _reviewed_fields(artifact.get("manual_review"))
        for artifact in artifacts
    )
    status = (
        "FAIL"
        if inspection["source"]["status"] == "FAIL"
        or any_fail
        or any(item["status"] == "FAIL" for item in artifact_hashes)
        else "PASS"
        if complete_coverage
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
        "field_statuses": ["PASS", "FAIL", "NOT_COMPARABLE"],
        "parser_map_count": len(parser_maps),
        "external_map_count": len(external_rows),
        "matches": matches,
        "artifact_checks": artifact_hashes,
        "unmatched_external_records": len(external_rows) - len(used_external),
        "address_representation": address_settings,
        "unresolved": [
            "A parser_ordinal must be explicitly present and manually reviewed; map names alone are not join keys.",
            "Address fields are not compared unless both address representations are declared equal.",
            "Map metadata does not validate numeric map values in a binary version.",
            "Only fields listed in an artifact's manual_review.confirmed_fields are compared.",
            "Incomplete axes or map coverage cannot yield an overall PASS.",
        ],
    }


def compare_project_metadata(
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
    inspection = inspect_package(database, manifest_path)
    parser_metadata = diagnostic["metadata"]
    project_review_gaps = _project_review_gaps(manifest)
    artifacts = [
        (index, item)
        for index, item in enumerate(manifest.get("artifacts", []))
        if isinstance(item, dict) and item.get("kind") == "project_metadata"
    ]
    if not artifacts:
        return {
            "run_type": "project_metadata",
            "project_id": project_id,
            "package_manifest": str(manifest_path.resolve()),
            "manifest_sha256": manifest_hash,
            "status": "NOT_TESTED",
            "field_statuses": ["PASS", "FAIL", "NOT_COMPARABLE"],
            "comparisons": [],
            "unresolved": ["No project_metadata artifact is declared."],
        }

    comparisons: list[dict[str, Any]] = []
    artifact_checks: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    incomplete_coverage = False
    for artifact_index, artifact in artifacts:
        review = artifact.get("manual_review")
        confirmed_fields = _reviewed_fields(review)
        review_required_fields = {"project_relation", "metadata_inventory"}
        version_key = artifact.get("version_key")
        if version_key is not None:
            review_required_fields.add("version_relation")
        review_gaps = _manual_review_gaps(review, review_required_fields)
        review_gaps.extend(_artifact_provenance_gaps(artifact, manifest))
        if artifact.get("relation_status", "unknown") != "confirmed":
            review_gaps.append("Metadata artifact relation is not confirmed.")
        if not _known_text(artifact.get("relation_evidence")):
            review_gaps.append("Metadata artifact relation evidence is missing.")
        if artifact.get("coverage_complete") is not True:
            incomplete_coverage = True
            review_gaps.append("Metadata artifact coverage is not confirmed complete.")
        if version_key is not None:
            versions = manifest.get("exported_versions", [])
            version = next(
                (
                    item
                    for item in versions
                    if isinstance(item, dict)
                    and item.get("version_key") == version_key
                ),
                None,
            ) if isinstance(versions, list) else None
            if version is None:
                review_gaps.append("Metadata artifact refers to an unknown version_key.")
            elif (
                version.get("relation_status") != "confirmed"
                or not _known_text(version.get("relation_evidence"))
            ):
                review_gaps.append(
                    "Metadata artifact's project-version relation is not confirmed."
                )
        try:
            path = _resolve_artifact(manifest_path, artifact.get("path"))
            actual_hash = sha256_file(path)
            actual_size = path.stat().st_size
            declared_hash = artifact.get("sha256")
            declared_size = artifact.get("size_bytes")
            matching_inspection = next(
                (
                    item
                    for item in inspection["artifacts"]
                    if item["artifact_index"] == artifact_index
                ),
                None,
            )
            integrity_status = (
                "PASS"
                if _safe_sha256(declared_hash)
                and declared_hash.lower() == actual_hash.lower()
                and isinstance(declared_size, int)
                and declared_size == actual_size
                and matching_inspection is not None
                and matching_inspection["status"] == "PASS"
                else "INCONCLUSIVE"
            )
            if (
                _safe_sha256(declared_hash)
                and declared_hash.lower() != actual_hash.lower()
            ) or (
                isinstance(declared_size, int) and declared_size != actual_size
            ):
                integrity_status = "FAIL"
            artifact_checks.append(
                {
                    "artifact_index": artifact_index,
                    "path": str(path),
                    "size_bytes": actual_size,
                    "sha256": actual_hash,
                    "status": integrity_status,
                    "manual_review_gaps": review_gaps,
                }
            )
            external_metadata = _load_project_metadata(path)
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            parse_errors.append(str(exc))
            artifact_checks.append(
                {
                    "artifact_index": artifact_index,
                    "path": artifact.get("path"),
                    "status": "FAIL",
                    "error": str(exc),
                    "manual_review_gaps": review_gaps,
                }
            )
            continue

        independently_reviewed = (
            not project_review_gaps
            and not review_gaps
            and inspection["source"]["status"] == "PASS"
            and integrity_status == "PASS"
        )
        for field, reference_value in sorted(external_metadata.items()):
            field_path = f"metadata.{field}"
            parser_record = parser_metadata.get(field)
            parser_value = (
                parser_record.get("value")
                if isinstance(parser_record, dict)
                else None
            )
            parser_status = (
                parser_record.get("status")
                if isinstance(parser_record, dict)
                else "missing_from_parser_object"
            )
            if (
                not independently_reviewed
                or field_path not in confirmed_fields
                or parser_status == "missing_from_parser_object"
            ):
                status = "NOT_COMPARABLE"
            else:
                status = _compare_values(
                    parser_value,
                    reference_value,
                    case_insensitive=True,
                )
            comparisons.append(
                {
                    "field": field_path,
                    "parser": parser_value,
                    "parser_status": parser_status,
                    "reference": reference_value,
                    "status": status,
                    "evidence_gaps": list(
                        dict.fromkeys(
                            project_review_gaps
                            + review_gaps
                            + (
                                []
                                if field_path in confirmed_fields
                                else [f"{field_path} was not independently reviewed."]
                            )
                        )
                    ),
                }
            )
        if set(external_metadata) != set(parser_metadata):
            incomplete_coverage = True

    statuses = {item["status"] for item in comparisons}
    status = (
        "FAIL"
        if inspection["source"]["status"] == "FAIL"
        or parse_errors
        or any(item["status"] == "FAIL" for item in artifact_checks)
        or "FAIL" in statuses
        else "PASS"
        if comparisons
        and not incomplete_coverage
        and all(item["status"] == "PASS" for item in artifact_checks)
        and statuses == {"PASS"}
        else "INCONCLUSIVE"
        if comparisons
        else "NOT_TESTED"
    )
    return {
        "run_type": "project_metadata",
        "project_id": project_id,
        "package_manifest": str(manifest_path.resolve()),
        "manifest_sha256": manifest_hash,
        "status": status,
        "field_statuses": ["PASS", "FAIL", "NOT_COMPARABLE"],
        "parser_metadata_fields": len(parser_metadata),
        "reference_metadata_fields": len(comparisons),
        "comparisons": comparisons,
        "artifact_checks": artifact_checks,
        "errors": parse_errors,
        "coverage_complete": not incomplete_coverage,
        "project_evidence_gaps": project_review_gaps,
        "unresolved": [
            "Only metadata fields explicitly reviewed in WinOLS are compared.",
            "An absent parser attribute is not treated as an empty value.",
            "A partial field set cannot produce an overall PASS.",
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
            "WinOLS project identity, exact export settings, and reviewer evidence tied to the indexed OLS hash.",
            "WinOLS map-list CSV or JSON with recorded export settings.",
            "Independently reviewed project metadata fields where available.",
            "Explicit project/version/slot and map-to-parser relationships with evidence.",
            "Axis definitions and conversion fields checked against the WinOLS project.",
        ],
    }
    if manifest_path.exists() and result["parser"] is not None:
        result["package"] = inspect_package(database, manifest_path)
    else:
        result["package_status"] = "NOT_TESTED"
    return result
