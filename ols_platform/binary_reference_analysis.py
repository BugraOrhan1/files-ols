from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .winols_reference import _compare_to_slot, _sha256_file_region, sha256_file


ANALYSIS_VERSION = "1"
CONTEXT_BYTES = 16


def _read_at(path: Path, offset: int, length: int) -> bytes:
    if offset < 0 or length < 0:
        raise ValueError("File ranges must have non-negative offsets and lengths.")
    with path.open("rb") as file:
        file.seek(offset)
        data = file.read(length)
    if len(data) != length:
        raise OSError(f"Short read at offset {offset} in {path}.")
    return data


def _find_occurrences(
    path: Path,
    needle: bytes,
    start: int,
    length: int,
    *,
    limit: int = 1000,
    chunk_size: int = 1024 * 1024,
) -> dict[str, Any]:
    if not needle or limit <= 0 or chunk_size <= 0:
        raise ValueError("needle, limit, and chunk_size must be non-empty/positive.")
    positions: list[int] = []
    count = 0
    carry = b""
    consumed = 0
    with path.open("rb") as file:
        file.seek(start)
        while consumed < length and count < limit:
            block = file.read(min(chunk_size, length - consumed))
            if not block:
                raise OSError(f"Short read while searching {path}.")
            data = carry + block
            data_start = start + consumed - len(carry)
            cursor = 0
            while count < limit:
                found = data.find(needle, cursor)
                if found < 0:
                    break
                absolute = data_start + found
                if absolute + len(needle) > start + consumed:
                    count += 1
                    if len(positions) < 20:
                        positions.append(absolute)
                cursor = found + 1
            consumed += len(block)
            carry = data[-(len(needle) - 1):] if len(needle) > 1 else b""
    return {
        "count_at_least": count,
        "positions_first_20": positions,
        "truncated": count == limit,
        "limit": limit,
    }


def _context(
    path: Path,
    boundary: int,
    *,
    context_bytes: int = CONTEXT_BYTES,
) -> dict[str, Any]:
    file_size = path.stat().st_size
    before_start = max(0, boundary - context_bytes)
    after_end = min(file_size, boundary + context_bytes)
    return {
        "boundary_offset": boundary,
        "before_start": before_start,
        "before_hex": _read_at(path, before_start, boundary - before_start).hex(),
        "after_end": after_end,
        "after_hex": _read_at(path, boundary, after_end - boundary).hex(),
    }


def analyze_binary_artifact(
    project: dict[str, Any],
    artifact: dict[str, Any],
    *,
    chunk_size: int = 1024 * 1024,
    max_ranges: int = 100_000,
) -> dict[str, Any]:
    source_path = Path(project["path"])
    export_path = Path(artifact["path"])
    slots = project["version_slots"]
    source_hash = sha256_file(source_path)
    export_hash = sha256_file(export_path)
    source_hash_matches_index = source_hash == project["sha256"]
    export_hash_matches_import = export_hash == artifact["sha256"]
    source_size = source_path.stat().st_size
    export_size = export_path.stat().st_size
    slot_results: list[dict[str, Any]] = []

    for slot in slots:
        blob_offset = slot["blob_offset"]
        blob_size = slot["blob_size"]
        payload_offset = blob_offset + 4
        payload_size = max(0, blob_size - 4)
        payload_available = payload_offset + payload_size <= source_size
        common_length = min(export_size, payload_size)
        export_prefix_sha256 = _sha256_file_region(
            export_path, 0, common_length
        )
        source_prefix_sha256 = _sha256_file_region(
            source_path, payload_offset, common_length
        )
        prefix_hash_match = (
            payload_available
            and export_prefix_sha256 == source_prefix_sha256
        )
        payload_comparison = (
            _compare_to_slot(
                export_path,
                source_path,
                payload_offset,
                common_length,
                chunk_size=chunk_size,
                max_ranges=max_ranges,
            )
            if prefix_hash_match
            else {
                "reference_size": export_size,
                "source_range_size": common_length,
                "compared_bytes": common_length,
                "length_equal": export_size == payload_size,
                "differing_bytes_in_common_range": None,
                "changed_ranges": [],
                "changed_ranges_truncated": False,
                "reference_prefix_sha256": export_prefix_sha256,
                "source_prefix_sha256": source_prefix_sha256,
                "comparison_skipped_after_hash_mismatch": True,
            }
        )
        exact_payload_prefix = (
            payload_available
            and prefix_hash_match
            and export_size >= payload_size
        )
        extra_length = max(0, export_size - payload_size)
        export_extra = (
            _read_at(export_path, payload_size, extra_length)
            if extra_length
            else b""
        )
        continuation_available = (
            extra_length > 0
            and blob_offset + blob_size + extra_length <= source_size
        )
        continuation = (
            _read_at(source_path, blob_offset + blob_size, extra_length)
            if continuation_available
            else b""
        )
        continuation_matches = (
            continuation_available and export_extra == continuation
        )
        whole_export_fits = payload_offset + export_size <= source_size
        full_range_comparison: dict[str, Any] | None = None
        exact_source_range_match = False
        if whole_export_fits:
            export_range_sha256 = export_hash
            source_range_sha256 = _sha256_file_region(
                source_path, payload_offset, export_size
            )
            if export_range_sha256 == source_range_sha256:
                full_range_comparison = _compare_to_slot(
                    export_path,
                    source_path,
                    payload_offset,
                    export_size,
                    chunk_size=chunk_size,
                    max_ranges=max_ranges,
                )
                exact_source_range_match = (
                    full_range_comparison["length_equal"]
                    and full_range_comparison["differing_bytes_in_common_range"] == 0
                )
            else:
                full_range_comparison = {
                    "reference_size": export_size,
                    "source_range_size": export_size,
                    "compared_bytes": export_size,
                    "length_equal": True,
                    "differing_bytes_in_common_range": None,
                    "changed_ranges": [],
                    "changed_ranges_truncated": False,
                    "reference_sha256": export_range_sha256,
                    "source_range_sha256": source_range_sha256,
                    "comparison_skipped_after_hash_mismatch": True,
                }

        next_slots = [
            item["slot_index"]
            for item in slots
            if item["blob_offset"] == blob_offset + blob_size
        ]
        parser_version_name = next(
            (
                version["name"]
                for version in project["versions"]
                if version["id"] == slot["version_id"]
            ),
            None,
        )
        version_link_confirmed = (
            artifact["relation_status"] == "confirmed"
            and artifact["source_slot_index"] == slot["slot_index"]
            and bool(artifact["version_name"])
            and artifact["version_name"] == parser_version_name
        )
        if next_slots and continuation_matches:
            adjacency = "matches_bytes_at_next_slot_boundary"
        elif continuation_matches:
            adjacency = "matches_bytes_at_source_slot_boundary"
        elif extra_length and continuation_available:
            adjacency = "does_not_match_immediately_following_source_bytes"
        elif extra_length:
            adjacency = "following_source_bytes_unavailable"
        else:
            adjacency = "no_export_bytes_beyond_parser_payload"

        suffix_occurrences: dict[str, Any] | None = None
        if export_extra:
            suffix_occurrences = _find_occurrences(
                source_path,
                export_extra,
                0,
                source_size,
            )
        source_boundary = blob_offset + blob_size
        marker = _read_at(source_path, blob_offset, min(4, blob_size))
        slot_results.append(
            {
                "slot_index": slot["slot_index"],
                "version_id": slot["version_id"],
                "parser_version_name": parser_version_name,
                "slot_status": slot["binary_status"],
                "name_status": slot["name_status"],
                "offsets": {
                    "slot_start": blob_offset,
                    "slot_end_exclusive": blob_offset + blob_size,
                    "payload_start": payload_offset,
                    "payload_end_exclusive": payload_offset + payload_size,
                    "export_start_in_source_if_aligned": payload_offset,
                    "export_end_in_source_if_aligned": payload_offset + export_size,
                },
                "sizes": {
                    "raw_slot_bytes": blob_size,
                    "parser_payload_bytes": payload_size,
                    "export_bytes": export_size,
                    "common_prefix_bytes": common_length,
                    "export_bytes_beyond_payload": extra_length,
                },
                "hashes": {
                    "raw_slot_sha256": slot["raw_sha256"],
                    "parser_payload_sha256": slot["payload_sha256"],
                    "export_sha256": export_hash,
                    "source_range_sha256": (
                        full_range_comparison["source_range_sha256"]
                        if full_range_comparison
                        else None
                    ),
                },
                "marker": {
                    "stored_marker_hex": slot["marker_hex"],
                    "actual_slot_start_hex": marker.hex(),
                    "matches_stored_marker": marker.hex() == slot["marker_hex"],
                },
                "comparison": {
                    "payload_prefix_exact": exact_payload_prefix,
                    "payload_prefix": payload_comparison,
                    "complete_contiguous_source_range_exact": exact_source_range_match,
                    "complete_contiguous_source_range": full_range_comparison,
                    "exact_export_range": {
                        "export_start": 0,
                        "export_end_exclusive": export_size,
                        "source_start": payload_offset,
                        "source_end_exclusive": payload_offset + export_size,
                        "exact": exact_source_range_match,
                    },
                },
                "extra_bytes": {
                    "position_in_export": payload_size if extra_length else None,
                    "hex": export_extra.hex(),
                    "sha256": (
                        _sha256_file_region(export_path, payload_size, extra_length)
                        if extra_length
                        else None
                    ),
                    "immediately_following_source_bytes_hex": continuation.hex(),
                    "matches_immediately_following_source_bytes": continuation_matches,
                    "following_slot_indices": next_slots,
                    "adjacency_result": adjacency,
                    "export_context": (
                        _context(export_path, payload_size)
                        if extra_length
                        else None
                    ),
                    "source_context": (
                        _context(source_path, source_boundary)
                        if extra_length and source_boundary <= source_size
                        else None
                    ),
                    "occurrences_in_source": suffix_occurrences,
                    "semantic_interpretation": "unclassified",
                },
                "validation_rules": [
                    {
                        "rule": "source_hash_matches_index",
                        "passed": source_hash_matches_index,
                    },
                    {
                        "rule": "export_hash_matches_import",
                        "passed": export_hash_matches_import,
                    },
                    {
                        "rule": "stored_slot_marker_matches_source",
                        "passed": marker.hex() == slot["marker_hex"],
                    },
                    {
                        "rule": "payload_prefix_matches",
                        "passed": exact_payload_prefix,
                    },
                    {
                        "rule": "extra_bytes_match_adjacent_source_data",
                        "passed": continuation_matches if extra_length else None,
                    },
                    {
                        "rule": "complete_export_matches_contiguous_source_range",
                        "passed": exact_source_range_match,
                    },
                    {
                        "rule": "project_version_name_confirmed",
                        "passed": version_link_confirmed,
                        "reason": (
                            "An explicit artifact-to-version mapping is present."
                            if version_link_confirmed
                            else "No explicit artifact-to-version mapping has "
                            "been provided; a filename label is not sufficient."
                        ),
                    },
                ],
                "binary_match_status": (
                    "PASS"
                    if source_hash_matches_index
                    and export_hash_matches_import
                    and exact_source_range_match
                    else "INCONCLUSIVE"
                ),
                "version_relation_status": (
                    "PASS" if version_link_confirmed else "INCONCLUSIVE"
                ),
            }
        )

    exact_candidates = [
        item
        for item in slot_results
        if item["comparison"]["complete_contiguous_source_range_exact"]
    ]
    payload_candidates = [
        item for item in slot_results if item["comparison"]["payload_prefix_exact"]
    ]
    settings = artifact.get("metadata", {})
    return {
        "artifact_id": artifact["id"],
        "kind": artifact["kind"],
        "export_path": str(export_path),
        "export_size_bytes": export_size,
        "export_sha256": export_hash,
        "imported_export_sha256": artifact["sha256"],
        "source_project_id": project["id"],
        "source_path": str(source_path),
        "source_size_bytes": source_size,
        "source_sha256": source_hash,
        "indexed_source_sha256": project["sha256"],
        "project_relation_status": settings.get(
            "manifest_project_relation_status", "unknown"
        ),
        "version_relation_status": artifact["relation_status"],
        "known_export_settings": {
            "winols_version": settings.get("winols_version", "unknown"),
            "export_method": settings.get("export_method", "unknown"),
            "representation": artifact["representation"],
            "filename_label_hint": settings.get("filename_label"),
            "manifest_metadata": settings,
            "explicit_fields": settings.get("export_settings", {}),
        },
        "candidate_slots": exact_candidates,
        "payload_prefix_candidate_slots": payload_candidates,
        "slots_examined": len(slot_results),
        "slot_results": slot_results,
        "unmatched_reason": (
            None
            if exact_candidates
            else "No exact contiguous source range match at the parser payload start."
        ),
    }


def analyze_project(
    database: Any,
    project_id: int,
    *,
    chunk_size: int = 1024 * 1024,
    max_ranges: int = 100_000,
) -> dict[str, Any]:
    project = database.get_project(project_id)
    if project is None:
        raise ValueError(f"No indexed project found with id {project_id}.")
    artifacts = database.list_reference_artifacts(project_id)
    source_hash = sha256_file(Path(project["path"]))
    source_hash_matches_index = source_hash == project["sha256"]
    artifact_reports = [
        analyze_binary_artifact(
            project,
            artifact,
            chunk_size=chunk_size,
            max_ranges=max_ranges,
        )
        for artifact in artifacts
        if artifact["kind"] == "binary_version"
    ]
    matches = [
        (artifact, slot)
        for artifact in artifact_reports
        for slot in artifact["candidate_slots"]
    ]
    confirmed_version_links = sum(
        slot["version_relation_status"] == "PASS"
        for artifact in artifact_reports
        for slot in artifact["candidate_slots"]
    )
    diagnostic = database.get_diagnostic(project_id)
    map_definitions = diagnostic["element_counts"]["map_definitions_stored"]
    has_project_metadata = any(
        item["kind"] == "project_metadata" for item in artifacts
    )
    has_map_reference = any(
        item["kind"] in {"map_metadata", "map_values"} for item in artifacts
    )
    counts = {
        "binary_reference_exports": len(artifact_reports),
        "slots_examined": sum(item["slots_examined"] for item in artifact_reports),
        "unique_exact_source_range_matches": len(matches),
        "exports_with_unique_slot_match": sum(
            len(item["candidate_slots"]) == 1 for item in artifact_reports
        ),
        "exports_with_ambiguous_slot_matches": sum(
            len(item["candidate_slots"]) > 1 for item in artifact_reports
        ),
        "exports_without_exact_slot_match": sum(
            not item["candidate_slots"] for item in artifact_reports
        ),
        "extra_byte_sequences_matching_source_continuation": sum(
            bool(slot["extra_bytes"]["matches_immediately_following_source_bytes"])
            for _, slot in matches
        ),
        "confirmed_version_name_links": confirmed_version_links,
        "independently_validated_map_records": 0,
    }
    return {
        "analysis_version": ANALYSIS_VERSION,
        "options": {
            "chunk_size": chunk_size,
            "max_ranges": max_ranges,
            "context_bytes": CONTEXT_BYTES,
        },
        "project_id": project_id,
        "source": {
            "path": str(project["path"]),
            "sha256": source_hash,
            "indexed_sha256": project["sha256"],
            "hash_matches_index": source_hash_matches_index,
            "size_bytes": project["size_bytes"],
            "format_version": project["format_version"],
        },
        "parser": diagnostic["parser"],
        "application_version": diagnostic["application_version"],
        "counts": counts,
        "overall_status": (
            "INCONCLUSIVE"
            if counts["confirmed_version_name_links"] == 0
            else "PARTIAL"
        ),
        "interpretation": {
            "four_byte_observation": (
                "For a candidate match, the four exported bytes beyond the "
                "parser payload equal the immediately following bytes in the "
                "original OLS file. This establishes byte continuity at the "
                "observed boundary; it does not identify their semantic role."
            ),
            "version_name_mapping": (
                "Slot-level binary matches are not treated as confirmation of "
                "the export filename label or a WinOLS version name."
            ),
        },
        "artifact_reports": artifact_reports,
        "additional_exports_needed": [
            *(
                [
                    "Binary version exports with known version labels and "
                    "explicit project/version provenance."
                ]
                if not artifact_reports
                or confirmed_version_links < len(artifact_reports)
                else []
            ),
            *(
                ["Independent project metadata export."]
                if not has_project_metadata
                else []
            ),
            *(
                [
                    "Independent map metadata/value exports for the parser's "
                    f"{map_definitions} map definitions."
                ]
                if map_definitions and not has_map_reference
                else []
            ),
            "Record the producing WinOLS version, export format, selected "
            "version/element, address range, and checksum/syncblock options.",
        ],
    }


def render_text_report(report: dict[str, Any]) -> str:
    lines = [
        f"WinOLS binary reference analysis — project {report['project_id']}",
        f"Source: {report['source']['path']}",
        f"Source SHA-256: {report['source']['sha256']}",
        f"Overall status: {report['overall_status']}",
        "",
        "Counts:",
    ]
    lines.extend(
        f"- {name.replace('_', ' ')}: {value}"
        for name, value in report["counts"].items()
    )
    for artifact in report["artifact_reports"]:
        lines.extend(
            [
                "",
                f"Export: {artifact['export_path']}",
                f"  Size: {artifact['export_size_bytes']} bytes",
                f"  SHA-256: {artifact['export_sha256']}",
                f"  Filename label hint (unverified): "
                f"{artifact['known_export_settings']['filename_label_hint']}",
                f"  Export settings: "
                f"{json.dumps(artifact['known_export_settings'], ensure_ascii=False)}",
                f"  Exact candidate slots: "
                f"{[slot['slot_index'] for slot in artifact['candidate_slots']]}",
            ]
        )
        for slot in artifact["slot_results"]:
            if not slot["extra_bytes"]["hex"]:
                continue
            lines.extend(
                [
                    f"  Slot {slot['slot_index']}: "
                    f"{slot['binary_match_status']}; version "
                    f"{slot['version_relation_status']}",
                    f"    Export extra bytes at "
                    f"{slot['extra_bytes']['position_in_export']}: "
                    f"{slot['extra_bytes']['hex']}",
                    f"    Following OLS bytes: "
                    f"{slot['extra_bytes']['immediately_following_source_bytes_hex']}",
                    f"    Boundary result: "
                    f"{slot['extra_bytes']['adjacency_result']}",
                    "    Semantic interpretation: unclassified",
                ]
            )
    lines.extend(["", report["interpretation"]["four_byte_observation"]])
    lines.append(report["interpretation"]["version_name_mapping"])
    return "\n".join(lines) + "\n"


def store_analysis_report(database: Any, report: dict[str, Any]) -> int:
    run_id = database.store_binary_analysis_run(
        project_id=report["project_id"],
        source_sha256=report["source"]["sha256"],
        algorithm_version=report["analysis_version"],
        options=report["options"],
        report=report,
    )
    for artifact in report["artifact_reports"]:
        for slot in artifact["slot_results"]:
            database.store_binary_slot_candidate(
                run_id=run_id,
                artifact_id=artifact["artifact_id"],
                project_id=report["project_id"],
                slot_index=slot["slot_index"],
                version_id=slot["version_id"],
                export_sha256=artifact["export_sha256"],
                slot_sha256=slot["hashes"]["raw_slot_sha256"],
                binary_status=slot["binary_match_status"],
                version_relation_status=slot["version_relation_status"],
                evidence=slot,
            )
    return run_id


def write_analysis_outputs(
    report: dict[str, Any],
    *,
    json_path: Path | None = None,
    text_path: Path | None = None,
) -> None:
    for path, content in (
        (
            json_path,
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        ),
        (text_path, render_text_report(report)),
    ):
        if path is None:
            continue
        if path.exists():
            raise FileExistsError(
                f"Refusing to overwrite an existing analysis report: {path}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
