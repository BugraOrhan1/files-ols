from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Protocol


@dataclass(frozen=True)
class ReadIssue:
    code: str
    message: str
    severity: str = "warning"


@dataclass(frozen=True)
class ParsedAxis:
    role: str
    points: int
    address: int
    data_type: str
    unit: str
    factor: float
    offset: float
    identifier: str
    description: str
    data_source: int = 0


@dataclass(frozen=True)
class ParsedMap:
    name: str
    description: str
    map_type: str
    data_type: str
    columns: int
    rows: int
    data_offset: int
    unit: str
    factor: float
    conversion_offset: float
    folder_id: int = 0
    type_code: int = 0
    version_relation_status: str = "unknown_not_exposed_by_parser"
    axes: tuple[ParsedAxis, ...] = ()


@dataclass(frozen=True)
class ParsedVersion:
    name: str
    description: str
    slot_index: int
    declared_slot_count: int
    blob_offset: int
    blob_size: int
    epk: str
    epk_offset: int
    parent_id: int | None = None
    is_root: bool = False


@dataclass(frozen=True)
class ParsedVersionSlot:
    slot_index: int
    blob_offset: int
    blob_size: int
    name: str
    description: str
    marker_hex: str
    raw_sha256: str
    payload_sha256: str
    payload_size: int
    binary_status: str
    name_status: str


@dataclass(frozen=True)
class ParsedSkippedElement:
    ordinal: int
    element_kind: str
    description: str
    raw_metadata_json: str
    reason: str


@dataclass(frozen=True)
class ParsedFolder:
    index: int
    name: str
    description: str


@dataclass(frozen=True)
class ParsedCalBlock:
    offset: int
    cas_id: str
    epk: str
    size_bytes: int


@dataclass(frozen=True)
class ProjectSnapshot:
    parser_name: str
    parser_version: str
    format_version: int
    metadata: dict[str, str]
    versions: tuple[ParsedVersion, ...]
    maps: tuple[ParsedMap, ...]
    version_slots: tuple[ParsedVersionSlot, ...] = ()
    folders: tuple[ParsedFolder, ...] = ()
    cal_blocks: tuple[ParsedCalBlock, ...] = ()
    skipped_elements: tuple[ParsedSkippedElement, ...] = ()
    issues: tuple[ReadIssue, ...] = ()
    raw_counts: dict[str, int | str | bool | None] = field(default_factory=dict)
    data_structure: dict[str, object] = field(default_factory=dict)


class OlsProjectReader(Protocol):
    """Parser-independent interface consumed by the indexing pipeline."""

    parser_name: str
    parser_version: str

    def inspect(self, source: Path) -> ProjectSnapshot: ...


class PyOlsReader:
    """Adapter for the external py-ols implementation.

    The GPL-3.0 parser stays outside this application's source tree. Point the
    adapter at a separately obtained checkout with ``--py-ols-path`` or install
    its ``ols_reader`` module in the active Python environment.
    """

    _METADATA_FIELDS = (
        "make",
        "model",
        "engine",
        "year",
        "fuel_type",
        "displacement",
        "power",
        "gearbox",
        "memory_type",
        "ecu_manufacturer",
        "ecu_type",
        "sw_number",
        "hw_number",
        "hw_code",
        "user",
    )
    _ADAPTER_VERSION = "3"

    def __init__(self, parser_path: Path | None = None) -> None:
        self._module = self._load_parser(parser_path)
        self.parser_name = "py-ols"
        self.parser_version = self._parser_version()

    @staticmethod
    def _load_parser(parser_path: Path | None) -> ModuleType:
        if parser_path is None:
            try:
                from ols_reader import OLSReader  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "py-ols is not installed. Supply --py-ols-path pointing "
                    "to a py-ols checkout containing ols_reader.py."
                ) from exc
            module = sys.modules[OLSReader.__module__]
            if not hasattr(module, "read_ols"):
                raise RuntimeError("The installed py-ols module has no read_ols().")
            return module

        module_path = parser_path / "ols_reader.py" if parser_path.is_dir() else parser_path
        if not module_path.is_file():
            raise FileNotFoundError(f"py-ols parser not found: {module_path}")
        module_name = "_ols_platform_external_py_ols"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load py-ols module at {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        if not hasattr(module, "read_ols") or not hasattr(module, "OLSReader"):
            raise RuntimeError("py-ols must provide read_ols() and OLSReader.")
        return module

    def inspect(self, source: Path) -> ProjectSnapshot:
        try:
            parser = self._module.OLSReader(source)
        except Exception as exc:
            return self._failed_snapshot(exc)

        issues: list[ReadIssue] = []
        parsed_records: list[object] = []
        parse_failures: list[str] = []
        original_read_map = getattr(parser, "_seq_read_one_map", None)
        instrumentation_available = callable(original_read_map)
        if instrumentation_available:
            def observe_map_record() -> object:
                try:
                    item = original_read_map()
                except Exception as exc:
                    parse_failures.append(
                        f"0x{getattr(parser, 'pos', 0):x}: {type(exc).__name__}: {exc}"
                    )
                    raise
                parsed_records.append(item)
                return item

            parser._seq_read_one_map = observe_map_record

        try:
            project = parser.parse()
        except Exception as exc:
            return self._failed_snapshot(exc)

        if project.version <= 0:
            issues.append(
                ReadIssue(
                    "unrecognized_header",
                    "py-ols did not identify a valid OLS header.",
                    "error",
                )
            )

        metadata_field_status: dict[str, str] = {}
        metadata: dict[str, str] = {}
        for field in self._METADATA_FIELDS:
            if not hasattr(project, field):
                metadata[field] = ""
                metadata_field_status[field] = "missing_from_parser_object"
                continue
            raw_value = getattr(project, field)
            metadata[field] = str(raw_value or "")
            metadata_field_status[field] = (
                "empty_in_parser_object"
                if raw_value is None or not str(raw_value).strip()
                else "present_in_parser_object"
            )
        missing_metadata = [key for key, value in metadata.items() if not value]
        if missing_metadata:
            issues.append(
                ReadIssue(
                    "missing_metadata",
                    "Empty metadata fields: " + ", ".join(missing_metadata),
                )
            )

        versions: list[ParsedVersion] = []
        for item in project.binary_versions:
            slot_count = int(getattr(item, "version_count", 0) or 0)
            versions.append(
                ParsedVersion(
                    name=str(getattr(item, "name", "") or ""),
                    description=str(getattr(item, "description", "") or ""),
                    slot_index=int(getattr(item, "version_index", 0) or 0),
                    declared_slot_count=slot_count,
                    blob_offset=int(getattr(item, "blob_offset", 0) or 0),
                    blob_size=int(getattr(item, "blob_size", 0) or 0),
                    epk=str(getattr(item, "epk", "") or ""),
                    epk_offset=int(getattr(item, "epk_offset", 0) or 0),
                    parent_id=getattr(item, "parent_id", None),
                    is_root=bool(getattr(item, "is_root", False)),
                )
            )
        if not versions:
            issues.append(ReadIssue("no_versions_found", "No binary versions were returned."))

        data = getattr(parser, "data", b"") or b""
        source_size = source.stat().st_size
        if hasattr(parser, "data") and len(data) != source_size:
            issues.append(
                ReadIssue(
                    "parser_source_size_mismatch",
                    f"Parser data has {len(data)} bytes but the source file has "
                    f"{source_size} bytes.",
                    "error",
                )
            )
        version_slots, version_layout, slot_issues = self._enumerate_binary_slots(
            source, versions, source_size
        )
        issues.extend(slot_issues)

        map_header = self._inspect_map_header(data)
        hexdump_records = [
            item
            for item in parsed_records
            if getattr(item, "description", "") == "Hexdump"
            and not getattr(item, "name", "")
        ]
        skipped_elements = tuple(
            ParsedSkippedElement(
                ordinal=ordinal,
                element_kind="hexdump_container",
                description=str(getattr(item, "description", "") or ""),
                raw_metadata_json=json.dumps(
                    self._map_metadata(item), ensure_ascii=False, sort_keys=True
                ),
                reason="Filtered by py-ols as a hexdump entry, not a named map.",
            )
            for ordinal, item in enumerate(hexdump_records)
        )
        if parse_failures:
            issues.extend(
                ReadIssue("map_record_parse_error", message, "error")
                for message in parse_failures
            )
        if map_header["record_count"] is None:
            issues.append(
                ReadIssue(
                    "map_list_marker_not_found",
                    "py-ols did not expose a recognized map-list marker/count.",
                )
            )
        elif len(parsed_records) < map_header["record_count"]:
            issues.append(
                ReadIssue(
                    "map_records_not_returned",
                    f"Map-list header declares {map_header['record_count']} record(s), "
                    f"but py-ols returned {len(parsed_records)} before filtering.",
                    "error",
                )
            )

        maps: list[ParsedMap] = []
        for item in project.parameters:
            axes: list[ParsedAxis] = []
            for role in ("x", "y"):
                axis = getattr(item, f"{role}_axis", None)
                if axis is None:
                    continue
                axes.append(
                    ParsedAxis(
                        role=role,
                        points=int(getattr(axis, "points", 0) or 0),
                        address=int(getattr(axis, "address", 0) or 0),
                        data_type=str(getattr(axis, "data_type", "") or ""),
                        unit=str(getattr(axis, "unit", "") or ""),
                        factor=float(getattr(axis, "factor", 1.0)),
                        offset=float(getattr(axis, "offset", 0.0)),
                        identifier=str(getattr(axis, "id", "") or ""),
                        description=str(getattr(axis, "description", "") or ""),
                        data_source=int(getattr(axis, "data_source", 0) or 0),
                    )
                )
            maps.append(
                ParsedMap(
                    name=str(getattr(item, "name", "") or ""),
                    description=str(getattr(item, "description", "") or ""),
                    map_type=str(getattr(item, "param_type", "") or "UNKNOWN"),
                    data_type=str(getattr(item, "data_type", "") or "UNKNOWN"),
                    columns=int(getattr(item, "cols", 0) or 0),
                    rows=int(getattr(item, "rows", 0) or 0),
                    data_offset=int(getattr(item, "data_offset", 0) or 0),
                    unit=str(getattr(item, "unit", "") or ""),
                    factor=float(getattr(item, "factor", 1.0)),
                    conversion_offset=float(getattr(item, "offset", 0.0)),
                    folder_id=int(getattr(item, "folder_id", 0) or 0),
                    type_code=int(getattr(item, "type_code", 0) or 0),
                    axes=tuple(axes),
                )
            )
        if not maps:
            if not skipped_elements:
                issues.append(
                    ReadIssue(
                        "no_map_definitions",
                        "The parser returned no map definitions and no hexdump "
                        "container was observed.",
                    )
                )
        elif any(not item.name for item in maps):
            issues.append(
                ReadIssue(
                    "unnamed_maps",
                    f"{sum(not item.name for item in maps)} map definition(s) have no name.",
                )
            )

        for map_item in maps:
            issues.extend(self._map_structure_issues(map_item, source_size))
            for axis in map_item.axes:
                if not axis.unit or not axis.identifier:
                    issues.append(
                        ReadIssue(
                            "incomplete_axis_metadata",
                            f"Map at 0x{map_item.data_offset:x} has an axis with "
                            "missing unit or identifier.",
                        )
                    )
                    break

        folders = tuple(
            ParsedFolder(
                index=int(getattr(folder, "idx", 0) or 0),
                name=str(getattr(folder, "name", "") or ""),
                description=str(getattr(folder, "description", "") or ""),
            )
            for folder in (getattr(project, "folders", ()) or ())
        )
        cal_block = getattr(project, "cal_block", None)
        cal_blocks = (
            (
                ParsedCalBlock(
                    offset=int(getattr(cal_block, "offset", 0) or 0),
                    cas_id=str(getattr(cal_block, "cas_id", "") or ""),
                    epk=str(getattr(cal_block, "epk", "") or ""),
                    size_bytes=len(getattr(cal_block, "data", b"") or b""),
                ),
            )
            if cal_block is not None
            else ()
        )

        if getattr(project, "is_kp_file", False):
            issues.append(
                ReadIssue(
                    "kp_metadata_only",
                    "This KP import contains definitions only; no embedded binary "
                    "versions are provided by py-ols.",
                )
            )

        total_declared_slots = max(
            (version.declared_slot_count for version in versions), default=0
        )
        named_slot_indexes = {version.slot_index for version in versions}
        unlabeled_slots = sum(
            slot.slot_index not in named_slot_indexes for slot in version_slots
        )
        if unlabeled_slots:
            issues.append(
                ReadIssue(
                    "unlabeled_binary_slots",
                    f"{unlabeled_slots} of {len(version_slots)} physically enumerated "
                    "binary payload slots have no named version record. Their "
                    "version meaning is unresolved; they are not classified as "
                    "missing, empty, or reserved.",
                )
            )

        if not instrumentation_available:
            issues.append(
                ReadIssue(
                    "map_record_telemetry_unavailable",
                    "This py-ols build does not expose the internal map-record "
                    "reader; raw-to-returned map counts cannot be verified.",
                )
            )

        raw_counts: dict[str, int | str | bool | None] = {
            "folders": len(getattr(project, "folders", ()) or ()),
            "has_cal_block": int(getattr(project, "cal_block", None) is not None),
            "is_kp_file": int(bool(getattr(project, "is_kp_file", False))),
            "binary_data_big_endian": int(bool(getattr(project, "big_endian_data", False))),
            "declared_binary_slots": total_declared_slots,
            "named_version_records": len(versions),
            "enumerated_binary_slots": len(version_slots),
            "unlabeled_binary_slots": unlabeled_slots,
            "available_binary_slots": sum(
                slot.binary_status == "present_nonempty_unverified"
                for slot in version_slots
            ),
            "map_list_records_declared": map_header["record_count"],
            "map_records_returned_by_parser": len(parsed_records),
            "hexdump_records_skipped": len(skipped_elements),
            "map_record_parse_failures": len(parse_failures),
            "map_definitions_returned": len(maps),
            "map_record_telemetry_available": instrumentation_available,
        }

        return ProjectSnapshot(
            parser_name="py-ols",
            parser_version=self._parser_version(),
            format_version=int(project.version or 0),
            metadata=metadata,
            versions=tuple(versions),
            maps=tuple(maps),
            version_slots=tuple(version_slots),
            folders=folders,
            cal_blocks=cal_blocks,
            skipped_elements=skipped_elements,
            issues=tuple(issues),
            raw_counts=raw_counts,
            data_structure={
                "container": "KP" if getattr(project, "is_kp_file", False) else "OLS",
                "format_version": int(project.version or 0),
                "metadata_field_status": metadata_field_status,
                "map_list": map_header,
                "binary_layout": version_layout,
            },
        )

    def _failed_snapshot(self, exc: Exception) -> ProjectSnapshot:
        message = f"{type(exc).__name__}: {exc}"
        return ProjectSnapshot(
            parser_name="py-ols",
            parser_version=self.parser_version,
            format_version=0,
            metadata={field: "" for field in self._METADATA_FIELDS},
            versions=(),
            maps=(),
            issues=(ReadIssue("parser_failure", message, "error"),),
            raw_counts={"parser_failed": True},
            data_structure={"parser_error": message},
        )

    @staticmethod
    def _inspect_map_header(data: bytes) -> dict[str, object]:
        marker = b"\x77\x33\x88\x11"
        marker_offset = data.find(marker, 0x100, min(0x20000, len(data)))
        if marker_offset < 0 or marker_offset + 9 > len(data):
            return {
                "marker_found": False,
                "marker_offset": None,
                "compressed": None,
                "record_count": None,
            }
        return {
            "marker_found": True,
            "marker_offset": marker_offset,
            "compressed": bool(data[marker_offset + 4]),
            "record_count": int.from_bytes(
                data[marker_offset + 5:marker_offset + 9], "little"
            ),
        }

    @staticmethod
    def _map_metadata(item: object) -> dict[str, object]:
        metadata: dict[str, object] = {
            "name": str(getattr(item, "name", "") or ""),
            "description": str(getattr(item, "description", "") or ""),
            "map_type": str(getattr(item, "param_type", "") or "UNKNOWN"),
            "type_code": int(getattr(item, "type_code", 0) or 0),
            "data_type": str(getattr(item, "data_type", "") or "UNKNOWN"),
            "columns": int(getattr(item, "cols", 0) or 0),
            "rows": int(getattr(item, "rows", 0) or 0),
            "data_offset": int(getattr(item, "data_offset", 0) or 0),
            "unit": str(getattr(item, "unit", "") or ""),
            "factor": float(getattr(item, "factor", 1.0)),
            "conversion_offset": float(getattr(item, "offset", 0.0)),
            "folder_id": int(getattr(item, "folder_id", 0) or 0),
        }
        axes = {}
        for role in ("x", "y"):
            axis = getattr(item, f"{role}_axis", None)
            if axis is not None:
                axes[role] = {
                    "points": int(getattr(axis, "points", 0) or 0),
                    "address": int(getattr(axis, "address", 0) or 0),
                    "data_type": str(getattr(axis, "data_type", "") or ""),
                    "unit": str(getattr(axis, "unit", "") or ""),
                    "factor": float(getattr(axis, "factor", 1.0)),
                    "offset": float(getattr(axis, "offset", 0.0)),
                    "identifier": str(getattr(axis, "id", "") or ""),
                    "description": str(getattr(axis, "description", "") or ""),
                    "data_source": int(getattr(axis, "data_source", 0) or 0),
                }
        metadata["axes"] = axes
        return metadata

    @staticmethod
    def _map_structure_issues(
        item: ParsedMap, source_size: int
    ) -> list[ReadIssue]:
        issues: list[ReadIssue] = []
        if item.columns < 0 or item.rows < 0:
            issues.append(
                ReadIssue(
                    "invalid_map_dimensions",
                    f"Map at 0x{item.data_offset:x} reports negative dimensions "
                    f"{item.columns}x{item.rows}.",
                    "error",
                )
            )
        elif item.columns == 0 or item.rows == 0:
            issues.append(
                ReadIssue(
                    "unresolved_map_dimensions",
                    f"Map at 0x{item.data_offset:x} reports zero dimensions "
                    f"{item.columns}x{item.rows}; values cannot be decoded from "
                    "this shape.",
                )
            )
        elif item.columns * item.rows > source_size:
            issues.append(
                ReadIssue(
                    "map_dimensions_exceed_source_bounds",
                    f"Map at 0x{item.data_offset:x} reports "
                    f"{item.columns * item.rows} cells, more than the "
                    f"{source_size}-byte source can contain even at one byte "
                    "per cell.",
                    "error",
                )
            )
        if item.data_offset < 0:
            issues.append(
                ReadIssue(
                    "invalid_map_data_offset",
                    f"Map reports a negative data offset {item.data_offset}.",
                    "error",
                )
            )
        for axis in item.axes:
            if axis.points < 0 or axis.address < 0:
                issues.append(
                    ReadIssue(
                        "invalid_axis_bounds",
                        f"Map at 0x{item.data_offset:x} has a negative "
                        f"{axis.role}-axis point count or address.",
                        "error",
                    )
                )
            elif axis.points > source_size:
                issues.append(
                    ReadIssue(
                        "axis_points_exceed_source_bounds",
                        f"Map at 0x{item.data_offset:x} has {axis.points} "
                        f"{axis.role}-axis points, more than the "
                        f"{source_size}-byte source can contain.",
                        "error",
                    )
                )
        return issues

    @staticmethod
    def _enumerate_binary_slots(source: Path, versions: list[ParsedVersion], file_size: int):
        issues: list[ReadIssue] = []
        if not versions:
            return [], {"status": "not_enumerable_without_binary_record"}, issues

        layouts = {
            (
                version.blob_offset - version.slot_index * version.blob_size,
                version.blob_size,
                version.declared_slot_count,
            )
            for version in versions
            if version.blob_size > 0 and version.declared_slot_count > 0
        }
        if len(layouts) != 1:
            issues.append(
                ReadIssue(
                    "inconsistent_binary_layout",
                    f"Found {len(layouts)} incompatible binary-slot layouts; "
                    "the adapter did not synthesize slot records.",
                    "error",
                )
            )
            return [], {"status": "inconsistent", "layout_candidates": len(layouts)}, issues

        base_offset, blob_size, slot_count = next(iter(layouts))
        if slot_count > 20 or base_offset < 0 or blob_size <= 4:
            issues.append(
                ReadIssue(
                    "unsupported_binary_layout",
                    "Binary-slot count, base offset, or blob size is outside "
                    "the parser's observed supported bounds.",
                    "error",
                )
            )
            return [], {
                "status": "out_of_supported_bounds",
                "base_offset": base_offset,
                "blob_size": blob_size,
                "slot_count": slot_count,
            }, issues

        names_by_slot: dict[int, ParsedVersion] = {}
        for version in versions:
            if version.slot_index < 0 or version.slot_index >= slot_count:
                issues.append(
                    ReadIssue(
                        "version_slot_index_out_of_bounds",
                        f"Named version record points to slot {version.slot_index}, "
                        f"outside the declared range 0..{slot_count - 1}.",
                        "error",
                    )
                )
                continue
            previous = names_by_slot.get(version.slot_index)
            if previous is not None and (
                previous.name != version.name
                or previous.description != version.description
            ):
                issues.append(
                    ReadIssue(
                        "conflicting_version_names",
                        f"Multiple named records disagree for slot {version.slot_index}.",
                        "error",
                    )
                )
            else:
                names_by_slot[version.slot_index] = version

        slots: list[ParsedVersionSlot] = []
        with source.open("rb") as file:
            for slot_index in range(slot_count):
                blob_offset = base_offset + slot_index * blob_size
                end_offset = blob_offset + blob_size
                version = names_by_slot.get(slot_index)
                if blob_offset < 0 or end_offset > file_size:
                    slots.append(
                        ParsedVersionSlot(
                            slot_index, blob_offset, blob_size,
                            version.name if version else "",
                            version.description if version else "",
                            "", "", "", 0, "out_of_file_bounds",
                            "named_record" if version else "unresolved",
                        )
                    )
                    issues.append(
                        ReadIssue(
                            "binary_slot_out_of_bounds",
                            f"Slot {slot_index} range [0x{blob_offset:x}, "
                            f"0x{end_offset:x}) is outside the {file_size}-byte file.",
                            "error",
                        )
                    )
                    continue
                file.seek(blob_offset)
                remaining = blob_size
                raw_digest = hashlib.sha256()
                payload_digest = hashlib.sha256()
                marker = file.read(min(4, remaining))
                raw_digest.update(marker)
                remaining -= len(marker)
                payload_nonzero = False
                while remaining:
                    chunk = file.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    raw_digest.update(chunk)
                    payload_digest.update(chunk)
                    payload_nonzero = payload_nonzero or any(chunk)
                    remaining -= len(chunk)
                complete = remaining == 0
                if not complete:
                    issues.append(
                        ReadIssue(
                            "binary_slot_short_read",
                            f"Slot {slot_index} ended before its declared "
                            f"{blob_size}-byte range was fully read.",
                            "error",
                        )
                    )
                slots.append(
                    ParsedVersionSlot(
                        slot_index=slot_index,
                        blob_offset=blob_offset,
                        blob_size=blob_size,
                        name=version.name if version else "",
                        description=version.description if version else "",
                        marker_hex=marker.hex(),
                        raw_sha256=raw_digest.hexdigest(),
                        payload_sha256=payload_digest.hexdigest(),
                        payload_size=max(0, blob_size - len(marker) - remaining),
                        binary_status=(
                            "present_nonempty_unverified"
                            if complete and payload_nonzero
                            else "present_empty_or_unreadable"
                            if complete
                            else "short_read"
                        ),
                        name_status="named_record" if version else "unresolved",
                    )
                )
        trailer_offset = base_offset + slot_count * blob_size
        trailer = ""
        if 0 <= trailer_offset and trailer_offset + 4 <= file_size:
            with source.open("rb") as file:
                file.seek(trailer_offset)
                trailer = file.read(4).hex()
        return slots, {
            "status": "enumerated_from_parser_binary_record",
            "interpretation": (
                "version_count is treated as the fixed-stride binary slot count "
                "used by py-ols's blob_offset + index * blob_size layout; this "
                "storage interpretation is not independently validated against WinOLS."
            ),
            "base_offset": base_offset,
            "blob_size": blob_size,
            "slot_count": slot_count,
            "end_offset_before_trailer": trailer_offset,
            "trailing_four_bytes_hex": trailer,
        }, issues

    def _parser_version(self) -> str:
        parser_file = getattr(self._module, "__file__", None)
        if parser_file and Path(parser_file).is_file():
            digest = hashlib.sha256(Path(parser_file).read_bytes()).hexdigest()
            parser_identity = f"sha256:{digest}"
        else:
            parser_identity = str(
                getattr(self._module, "__version__", "")
                or getattr(self._module, "VERSION", "unknown")
            )
        return f"adapter-{self._ADAPTER_VERSION}+py-ols-{parser_identity}"
