"""Version names and binary version payloads of WinOLS projects.

Two independent facts are combined:

* The project's *RevisionTag* string (length-prefixed, near the start of the
  file) lists the version names in slot order together with WinOLS' checksum
  note per version, e.g. ``1 (Original,\\t Stage 1:\\t NOCS)``.
* The parser adapter enumerates fixed-stride binary slots.

Slot order versus tag order was confirmed on four sample projects by matching
SHA-256 hashes of independent WinOLS exports to the slot payloads (see
``TUNING_STATUS.md``). The parser's own per-slot names are *not* used: they
were observed to be shifted.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TAG_PATTERN = re.compile(rb"(\d{1,2}) \(([^\x00-\x08,\t]{1,80})(?:,\t |\))")
_HEADER_SCAN_BYTES = 1024 * 1024


@dataclass(frozen=True)
class VersionTag:
    index: int
    name: str
    checksum_note: str


@dataclass(frozen=True)
class RevisionTag:
    declared_count: int
    versions: tuple[VersionTag, ...]
    raw: str


def read_revision_tag(path: Path) -> RevisionTag | None:
    with path.open("rb") as handle:
        head = handle.read(_HEADER_SCAN_BYTES)
    return parse_revision_tag(head)


def parse_revision_tag(head: bytes) -> RevisionTag | None:
    for match in _TAG_PATTERN.finditer(head):
        start = match.start()
        if start < 4:
            continue
        (length,) = struct.unpack_from("<I", head, start - 4)
        if length <= 0 or start + length > len(head) or length > 64 * 1024:
            continue
        raw = head[start:start + length].decode("latin-1")
        if not raw.endswith(")"):
            continue
        declared = int(match.group(1).decode())
        inner = raw[raw.index("(") + 1:-1]
        items = inner.split(",\t ")
        versions: list[VersionTag] = []
        for index, item in enumerate(items):
            if ":\t " in item:
                name, note = item.split(":\t ", 1)
            else:
                name, note = item, ""
            versions.append(VersionTag(index, name.strip(), note.strip()))
        if not versions or len(versions) != declared + 1:
            continue
        return RevisionTag(declared, tuple(versions), raw)
    return None


# Tuning components recognised in WinOLS version names. Order = display order.
COMPONENTS: list[tuple[str, str, re.Pattern]] = [
    (key, label, re.compile(pattern, re.IGNORECASE)) for key, label, pattern in (
        ("egr_off", "EGR OFF", r"\b(egr|agr)\b|egr[ _-]?off"),
        ("dpf_off", "DPF OFF", r"\b(dpf|fap|opf|gpf)\b|partikel"),
        ("adblue_off", "AdBlue OFF", r"adblue|\bscr\b|\bnox\b"),
        ("cat_off", "Lambda/KAT OFF", r"lambda|\bo2\b|\bcat\b|\bkat\b|decat"),
        ("dtc_off", "DTC OFF", r"\bdtc"),
        ("pop_bang", "Pop & Bang", r"pop|bang|burble|crackle|\bp ?& ?b\b|\bpnb\b"),
        ("vmax", "Vmax", r"v ?-?max|speed ?limit|limiter"),
        ("launch_control", "Launch control", r"launch|\blc\b"),
        ("hardcut", "Hardcut", r"hard ?cut"),
        ("immo_off", "IMMO OFF", r"immo"),
        ("swirl_off", "Swirl/flaps OFF", r"swirl|\bflaps?\b|drall"),
        ("start_stop_off", "Start/Stop OFF", r"start ?/? ?stop|\bs/s\b"),
        ("maf_off", "MAF OFF", r"\bmaf\b|\blmm\b"),
        ("torque_monitor_off", "Torque monitoring OFF", r"torq(ue)? ?monitor"),
        ("evap_off", "EVAP OFF", r"evap"),
        ("tva_off", "TVA OFF", r"\btva\b"),
        ("e85", "E85/Flex", r"\be85\b|flex ?fuel"),
    )
]
COMPONENT_LABELS = {key: label for key, label, _ in COMPONENTS}
_STAGE = re.compile(r"\b(stage|stufe|st|step|etappe)[ ._-]?([1-4])(?!\d)", re.IGNORECASE)
_ORIGINAL = {"original", "orginal", "ori", "org", "stock", "serie", "standard", "origineel"}


def classify_profile(name: str) -> str:
    """Canonical tuning profile of a WinOLS version name.

    Returns ``original``, a profile key such as ``stage1``, ``egr_off`` or
    ``stage1+egr_off+pop_bang`` (stage first, then components in a fixed
    order), or ``other`` when nothing recognisable is named.
    """

    normalized = re.sub(r"\s+", " ", name.strip().lower())
    if normalized in _ORIGINAL:
        return "original"
    core = re.sub(r"\b\d{2,4} ?(ps|hp|pk|cv|kw|nm|bhp)\b", " ", normalized)
    parts = []
    stage = _STAGE.search(core)
    if stage:
        parts.append(f"stage{stage.group(2)}")
        core = core[:stage.start()] + " " + core[stage.end():]
    for key, _label, pattern in COMPONENTS:
        if pattern.search(core):
            parts.append(key)
    return "+".join(parts) if parts else "other"


def profile_label(profile: str) -> str:
    if profile in ("original", "other"):
        return {"original": "Origineel", "other": "Overig"}[profile]
    labels = []
    for part in profile.split("+"):
        if part.startswith("stage"):
            labels.append(f"Stage {part[5:]}")
        else:
            labels.append(COMPONENT_LABELS.get(part, part))
    return " + ".join(labels)


def profile_sort_key(profile: str) -> tuple:
    parts = profile.split("+")
    stage = parts[0][5:] if parts[0].startswith("stage") else "9"
    return (len(parts), stage, profile)


def checksum_state(note: str) -> str:
    note = note.upper()
    if note.startswith("NOCS"):
        return "NOT_CORRECTED_IN_REFERENCE"
    if note.startswith("CS+"):
        return "CORRECTED_BY_WINOLS_MODULE"
    if note.startswith("CS-"):
        return "CHECKSUM_INVALID_IN_REFERENCE"
    return "UNKNOWN"


@dataclass
class ProjectVersions:
    project_path: str
    project_sha256: str
    project_size: int
    metadata: dict[str, str]
    tag: RevisionTag | None
    slots: list[dict[str, Any]]
    issues: list[str] = field(default_factory=list)
    maps: list[dict[str, Any]] = field(default_factory=list)


def _sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def inspect_project(path: Path, reader: Any, project_sha256: str | None = None) -> ProjectVersions:
    """Combine revision tag, parser slots and parser maps for one project."""

    snapshot = reader.inspect(path)
    tag = read_revision_tag(path)
    issues: list[str] = []
    if tag is None:
        issues.append("Geen RevisionTag met versienamen gevonden.")
    slots = [
        {
            "slot_index": slot.slot_index,
            "blob_offset": slot.blob_offset,
            "blob_size": slot.blob_size,
            "sha256": slot.raw_sha256,
            "status": slot.binary_status,
        }
        for slot in snapshot.version_slots
    ]
    if tag is not None and slots and len(slots) != tag.declared_count + 1:
        issues.append(
            f"Aantal binaire slots ({len(slots)}) past niet bij RevisionTag "
            f"({tag.declared_count + 1} versies)."
        )
    if tag is not None and slots and len(tag.versions) != len(slots):
        issues.append(
            f"RevisionTag noemt {len(tag.versions)} versies, parser vond {len(slots)} slots."
        )
    maps = [
        {
            "description": item.description,
            "data_type": item.data_type,
            "columns": item.columns,
            "rows": item.rows,
            "data_offset": item.data_offset,
            "unit": item.unit,
            "factor": item.factor,
            "conversion_offset": item.conversion_offset,
            "axes": [
                {
                    "role": axis.role, "points": axis.points, "address": axis.address,
                    "data_type": axis.data_type, "unit": axis.unit, "factor": axis.factor,
                }
                for axis in item.axes
            ],
        }
        for item in snapshot.maps
    ]
    return ProjectVersions(
        project_path=str(path),
        project_sha256=project_sha256 or _sha256_file(path),
        project_size=path.stat().st_size,
        metadata={k: v for k, v in snapshot.metadata.items() if v},
        tag=tag,
        slots=slots,
        issues=issues,
        maps=maps,
    )


# The version image starts four bytes after py-ols' ``blob_offset`` and is
# ``blob_size`` bytes long. Verified against 14 independent WinOLS exports in
# four projects (SHA-256 identical); the parser's own "marker + payload"
# reading is offset by these four bytes.
IMAGE_SHIFT = 4


def read_slot(path: Path, slot: dict[str, Any]) -> bytes:
    start = slot["blob_offset"] + IMAGE_SHIFT
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read(slot["blob_size"])
    if len(data) != slot["blob_size"]:
        raise OSError(f"Slot {slot['slot_index']} kon niet volledig gelezen worden.")
    return data
