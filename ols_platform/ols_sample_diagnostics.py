from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from .reader import ProjectSnapshot, PyOlsReader


SAMPLE_NAMES = (
    "100004.ols",
    "100007.ols",
    "100008.ols",
    "100009.ols",
    "100010.ols",
    "10001004.ols",
    "10001005.ols",
    "100010054.ols",
)


class ProjectInspector(Protocol):
    parser_name: str
    parser_version: str

    def inspect(self, source: Path) -> ProjectSnapshot: ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _value_at_path(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for component in path.split("."):
        if isinstance(current, dict) and component in current:
            current = current[component]
        elif isinstance(current, list) and component.isdecimal():
            index = int(component)
            if index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def compare_reference(
    report: dict[str, Any],
    manifest_path: Path | None,
) -> dict[str, Any]:
    if manifest_path is None or not manifest_path.is_file():
        return {
            "status": "NOT_TESTED",
            "reason": "No independent WinOLS reference manifest is available.",
            "comparisons": [],
        }

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "INCONCLUSIVE",
            "reason": f"Reference manifest cannot be read: {type(exc).__name__}: {exc}",
            "comparisons": [],
        }

    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        return {
            "status": "INCONCLUSIVE",
            "reason": "Reference manifest schema is missing or unsupported.",
            "comparisons": [],
        }

    sample = manifest.get("sample")
    reference = manifest.get("reference")
    observations = manifest.get("observations")
    source = report["source"]
    if not isinstance(sample, dict) or not isinstance(reference, dict):
        return {
            "status": "INCONCLUSIVE",
            "reason": "Reference sample identity or provenance is missing.",
            "comparisons": [],
        }
    if (
        sample.get("filename") != source["filename"]
        or sample.get("sha256") != source["sha256"]
    ):
        return {
            "status": "INCONCLUSIVE",
            "reason": "Reference package does not identify this exact OLS sample.",
            "comparisons": [],
        }

    artifact_value = reference.get("artifact_path")
    artifact_hash = reference.get("artifact_sha256")
    if not isinstance(artifact_value, str) or not artifact_value or not artifact_hash:
        return {
            "status": "NOT_TESTED",
            "reason": "No hashed independent export artifact is recorded.",
            "comparisons": [],
        }
    artifact_path = (manifest_path.parent / artifact_value).resolve()
    try:
        actual_artifact_hash = sha256_file(artifact_path)
    except OSError as exc:
        return {
            "status": "INCONCLUSIVE",
            "reason": f"Reference artifact is unavailable: {type(exc).__name__}: {exc}",
            "comparisons": [],
        }
    if actual_artifact_hash != artifact_hash:
        return {
            "status": "INCONCLUSIVE",
            "reason": "Reference artifact SHA-256 does not match its manifest.",
            "comparisons": [],
        }

    required_provenance = (
        reference.get("source") == "independent_winols_export"
        and reference.get("export_method") not in (None, "", "unknown")
        and reference.get("software_version") not in (None, "", "unknown")
        and reference.get("project_relation") == "confirmed"
        and reference.get("validation_status") == "independently_confirmed"
        and bool(reference.get("validated_by"))
        and bool(reference.get("validation_notes"))
    )
    if not required_provenance:
        return {
            "status": "INCONCLUSIVE",
            "reason": (
                "Reference provenance is not independently confirmed; parser "
                "output, unknown export settings, or unconfirmed project "
                "relationships cannot validate a parse."
            ),
            "comparisons": [],
        }
    if not isinstance(observations, dict) or not observations:
        return {
            "status": "NOT_TESTED",
            "reason": "No independently recorded field observations are available.",
            "comparisons": [],
        }

    comparisons: list[dict[str, Any]] = []
    for field_path, expected in observations.items():
        if not isinstance(field_path, str) or not field_path:
            return {
                "status": "INCONCLUSIVE",
                "reason": "Reference observations contain an invalid field path.",
                "comparisons": [],
            }
        exists, actual = _value_at_path(report, field_path)
        if not exists:
            status = "NOT_COMPARABLE"
            message = "The parser diagnostic does not expose this reference field."
        else:
            status = "PASS" if actual == expected else "FAIL"
            message = "Values match." if status == "PASS" else "Values differ."
        comparisons.append(
            {
                "field": field_path,
                "status": status,
                "expected": expected,
                "actual": actual,
                "message": message,
            }
        )

    statuses = {item["status"] for item in comparisons}
    overall_status = (
        "FAIL"
        if "FAIL" in statuses
        else "PARTIAL"
        if "NOT_COMPARABLE" in statuses
        or reference.get("coverage_complete") is not True
        else "PASS"
    )
    return {
        "status": overall_status,
        "reason": (
            "Compared the available parser fields with a hash-verified, "
            "independently confirmed WinOLS export."
        ),
        "artifact": {
            "path": str(artifact_path),
            "sha256": actual_artifact_hash,
            "export_method": reference["export_method"],
            "software_version": reference["software_version"],
        },
        "comparisons": comparisons,
    }


def build_sample_report(
    source: Path,
    reader: ProjectInspector,
    reference_manifest: Path | None = None,
) -> dict[str, Any]:
    snapshot = reader.inspect(source)
    metadata_status = snapshot.data_structure.get("metadata_field_status", {})
    if not isinstance(metadata_status, dict):
        metadata_status = {}
    metadata = {
        field: {
            "value": value,
            "status": metadata_status.get(
                field, "returned_value" if value else "empty_in_parser_output"
            ),
        }
        for field, value in snapshot.metadata.items()
    }
    map_counts = Counter(item.map_type for item in snapshot.maps)
    maps = []
    for item in snapshot.maps:
        map_value = asdict(item)
        map_value["name_status"] = "named" if item.name else "empty_name"
        map_value["axes"] = [
            {
                **asdict(axis),
                "status": (
                    "structurally_incomplete"
                    if not axis.unit or not axis.identifier
                    else "metadata_present"
                ),
                "missing_fields": [
                    field
                    for field in ("unit", "identifier")
                    if not getattr(axis, field)
                ],
            }
            for axis in item.axes
        ]
        maps.append(map_value)

    errors = [asdict(issue) for issue in snapshot.issues if issue.severity == "error"]
    warnings = [asdict(issue) for issue in snapshot.issues if issue.severity != "error"]
    if errors or snapshot.format_version <= 0:
        parse_status = "FAILED"
    elif warnings:
        parse_status = "PARSED_WITH_WARNINGS"
    else:
        parse_status = "PARSED"

    report: dict[str, Any] = {
        "report_schema_version": 1,
        "source": {
            "filename": source.name,
            "path": str(source.resolve()),
            "size_bytes": source.stat().st_size,
            "sha256": sha256_file(source),
        },
        "parser": {
            "name": snapshot.parser_name,
            "version": snapshot.parser_version,
            "format_version": snapshot.format_version,
            "status": parse_status,
        },
        "metadata": metadata,
        "counts": {
            "named_versions": sum(bool(item.name) for item in snapshot.versions),
            "versions_returned": len(snapshot.versions),
            "slots_enumerated": len(snapshot.version_slots),
            "maps_returned": len(snapshot.maps),
            "maps_by_type": dict(sorted(map_counts.items())),
            "axes_returned": sum(len(item.axes) for item in snapshot.maps),
            "incomplete_axes": sum(
                not axis.unit or not axis.identifier
                for item in snapshot.maps
                for axis in item.axes
            ),
        },
        "versions": [asdict(item) for item in snapshot.versions],
        "slots": [asdict(item) for item in snapshot.version_slots],
        "maps": maps,
        "folders": [asdict(item) for item in snapshot.folders],
        "cal_blocks": [asdict(item) for item in snapshot.cal_blocks],
        "skipped_elements": [asdict(item) for item in snapshot.skipped_elements],
        "raw_counts": snapshot.raw_counts,
        "data_structure": snapshot.data_structure,
        "errors": errors,
        "warnings": warnings,
        "reference_validation": {
            "status": "NOT_TESTED",
            "reason": "Reference comparison has not run.",
            "comparisons": [],
        },
    }
    report["reference_validation"] = compare_reference(report, reference_manifest)
    return report


def make_reference_template(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "sample": {
            "filename": report["source"]["filename"],
            "sha256": report["source"]["sha256"],
        },
        "reference": {
            "source": "unknown",
            "export_method": "unknown",
            "software_version": "unknown",
            "artifact_path": "",
            "artifact_sha256": "",
            "project_relation": "unknown",
            "validation_status": "NOT_TESTED",
            "validated_by": "",
            "validation_notes": "",
            "coverage_complete": False,
        },
        "observations": {},
        "notes": (
            "Populate only from an independently produced WinOLS export. "
            "Record the export artifact hash, exact export method, WinOLS "
            "version, and confirmed relation to this exact OLS source."
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create read-only py-ols diagnostics for the eight OLS samples."
    )
    parser.add_argument("--samples-dir", required=True, type=Path)
    parser.add_argument("--py-ols-path", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--reference-dir",
        type=Path,
        help="Optional directory containing <sample-stem>.json reference manifests.",
    )
    parser.add_argument(
        "--reference-template-dir",
        type=Path,
        help="Optional directory for NOT_TESTED reference-package templates.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    reader = PyOlsReader(args.py_ols_path)
    sample_paths = [args.samples_dir / name for name in SAMPLE_NAMES]
    output_path = args.output.resolve()
    if output_path in {path.resolve() for path in sample_paths}:
        raise ValueError("The diagnostic output path must not overwrite an OLS sample.")
    samples: list[dict[str, Any]] = []
    for name in SAMPLE_NAMES:
        source = args.samples_dir / name
        if not source.is_file():
            samples.append(
                {
                    "source": {"filename": name},
                    "parser": {"status": "NOT_TESTED"},
                    "reference_validation": {
                        "status": "NOT_TESTED",
                        "reason": "Sample file is unavailable.",
                        "comparisons": [],
                    },
                }
            )
            continue
        manifest = (
            args.reference_dir / f"{source.stem}.json"
            if args.reference_dir is not None
            else None
        )
        report = build_sample_report(source, reader, manifest)
        samples.append(report)
        if args.reference_template_dir is not None:
            template = args.reference_template_dir / f"{source.stem}.json"
            if template.exists():
                raise FileExistsError(
                    f"Refusing to overwrite reference template: {template}"
                )
            template.parent.mkdir(parents=True, exist_ok=True)
            template.write_text(
                json.dumps(make_reference_template(report), indent=2) + "\n",
                encoding="utf-8",
            )

    output = {
        "report_schema_version": 1,
        "parser": {
            "name": reader.parser_name,
            "version": reader.parser_version,
        },
        "samples": samples,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Diagnostics written to {args.output.resolve()}")
    return int(
        any(
            item.get("parser", {}).get("status") in {"NOT_TESTED", "FAILED"}
            for item in samples
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
