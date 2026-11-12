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


def build_sample_report(
    source: Path,
    reader: ProjectInspector,
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
            "reason": (
                "Use the canonical WinOLS reference-package commands for "
                "metadata, version, and map comparisons."
            ),
            "comparisons": [],
        },
    }
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create read-only py-ols diagnostics for the eight OLS samples."
    )
    parser.add_argument("--samples-dir", required=True, type=Path)
    parser.add_argument("--py-ols-path", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
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
        report = build_sample_report(source, reader)
        samples.append(report)

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
