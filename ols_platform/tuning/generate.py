"""Generate a tuned image from a new original and a frozen package.

The generator never invents bytes: every write comes from a learned region
rule. Anything it cannot justify for this specific input becomes a rejection
with a concrete reason.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Any

from .identity import classify_input, match_identity
from .package import PROFILE_LABELS


class GenerationRejected(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class GenerationResult:
    output: bytes
    family_key: str
    variant_key: str
    profile: str
    package_id: str
    input_sha256: str
    output_sha256: str
    changes: list[dict[str, Any]]
    allowed_ranges: list[tuple[int, int]]
    identity: dict[str, Any]
    evidence_level: str
    integrity: dict[str, Any]
    notes: list[str] = field(default_factory=list)


def _context_matches(data: bytes, start: int, hex_ctx: str, mask: str) -> bool:
    expected = bytes.fromhex(hex_ctx)
    actual = data[start:start + len(expected)]
    if len(actual) != len(expected):
        return False
    return all(m == "0" or a == e for a, e, m in zip(actual, expected, mask))


def _pack(value: int, endian: str) -> bytes:
    return struct.pack("<H" if endian == "little" else ">H", value)


def _unpack(data: bytes, endian: str) -> int:
    return struct.unpack("<H" if endian == "little" else ">H", data)[0]


def _select_family(package: dict[str, Any], data: bytes) -> tuple[dict, dict, dict]:
    ecu_hits = []
    for family in package["families"]:
        ecu_tokens = family["family_identity"].get("ecu_type", [])
        ecu_found = bool(ecu_tokens) and all(t.encode("latin-1") in data for t in ecu_tokens)
        if ecu_found:
            ecu_hits.append(family)
    if not ecu_hits:
        # Fall back on full identity in case the ECU type string is absent.
        for family in package["families"]:
            for variant in family["variants"]:
                check = match_identity(variant["identity"], data)
                if check and all(not v["missing"] for v in check.values()):
                    ecu_hits.append(family)
                    break
    if not ecu_hits:
        known = ", ".join(sorted({f["ecu_type"] or f["family_key"] for f in package["families"]}))
        raise GenerationRejected(
            "ecu_not_recognized",
            "De ECU van dit bestand is niet herkend door het actieve modelpakket "
            f"(bekende families: {known}).",
        )
    size_hits = [f for f in ecu_hits if f["size"] == len(data)]
    if not size_hits:
        sizes = ", ".join(str(f["size"]) for f in ecu_hits)
        raise GenerationRejected(
            "unknown_segment_layout",
            f"De ECU-familie {ecu_hits[0]['ecu_type']} is herkend, maar de bestandsgrootte "
            f"({len(data)} bytes) past bij geen bewezen leesvorm/segmentindeling "
            f"({sizes} bytes). Mogelijk is dit een andere uitleesvorm (bijv. alleen "
            "kalibratie of volledige flash) die nog niet gevalideerd is.",
        )
    reasons = []
    for family in size_hits:
        for variant in family["variants"]:
            check = match_identity(variant["identity"], data)
            hw_missing = [
                t for f in ("hw_number", "hw_code") for t in check.get(f, {}).get("missing", [])
            ]
            sw_missing = [
                t for f in ("sw_number", "sw_version") for t in check.get(f, {}).get("missing", [])
            ]
            if not hw_missing and not sw_missing:
                return family, variant, check
            reasons.append((hw_missing, sw_missing))
    hw_missing, sw_missing = reasons[0]
    if hw_missing and not sw_missing:
        raise GenerationRejected(
            "other_hardware",
            "De ECU-familie is herkend, maar het hardwarenummer wijkt af "
            f"(niet gevonden: {', '.join(hw_missing)}). Voor deze hardware is het profiel niet bewezen.",
        )
    raise GenerationRejected(
        "software_variant_not_validated",
        "De ECU-familie en bestandsindeling zijn herkend, maar de softwarevariant "
        "staat niet in het pakket (niet gevonden: "
        f"{', '.join(sw_missing or hw_missing)}). De kaartindeling voor deze variant "
        "is nog niet gevalideerd.",
    )


def _region_looks_tuned(region: dict[str, Any], data: bytes) -> bool:
    """Value-level check for inputs that are not byte-identical to training.

    A cell votes "tuned" when its value lies inside the learned tuned range
    but outside the learned original range (both ranges must be disjoint).
    """

    if region["kind"] != "calibration":
        return False
    votes = decisive = 0
    for index, rule in enumerate(region["cells"]):
        if rule is None:
            continue
        if rule["tuned_min"] <= rule["orig_max"] and rule["orig_min"] <= rule["tuned_max"]:
            continue  # overlapping ranges are not decisive
        decisive += 1
        value = _unpack(data[region["start"] + index * 2:region["start"] + index * 2 + 2], region["endian"])
        in_tuned = rule["tuned_min"] <= value <= rule["tuned_max"]
        in_orig = rule["orig_min"] <= value <= rule["orig_max"]
        if in_tuned and not in_orig:
            votes += 1
    return decisive >= 3 and votes / decisive > 0.5


def _check_mixed_input(family: dict[str, Any], profile: dict[str, Any], data: bytes) -> None:
    tuned_hits = 0
    original_hits = 0
    for region in profile["regions"]:
        current = data[region["start"]:region["end"]].hex()
        tuned_values = {t for o, t in region["exact"]}
        orig_values = {o for o, t in region["exact"]}
        if current in tuned_values and current not in orig_values:
            tuned_hits += 1
        elif current in orig_values:
            original_hits += 1
        elif _region_looks_tuned(region, data):
            tuned_hits += 1
    total = len(profile["regions"]) or 1
    if tuned_hits and tuned_hits / total >= 0.5:
        raise GenerationRejected(
            "already_tuned",
            f"Dit bestand is al getuned: {tuned_hits} van {total} geleerde "
            "Stage 1-gebieden bevatten al de getunede waarden. Open het originele bestand.",
        )
    zone_hits = sorted({
        zone["label"] for zone in family.get("modification_zones", [])
        if data[zone["start"]:zone["end"]].hex() == zone["tuned"]
        and zone["tuned"] != zone["original"]
    })
    if zone_hits:
        raise GenerationRejected(
            "other_modification_present",
            "Het bestand bevat al een andere bewerking ("
            + ", ".join(zone_hits)
            + "). Open het ongewijzigde origineel.",
        )
    if tuned_hits:
        raise GenerationRejected(
            "partially_tuned",
            f"{tuned_hits} van {total} Stage 1-gebieden zijn al gewijzigd en de rest niet. "
            "Gemengde of half bewerkte invoer wordt niet verwerkt.",
        )


def _apply_region(region: dict[str, Any], data: bytes, settings: dict[str, Any]) -> tuple[bytes, str, list[str]]:
    start, end = region["start"], region["end"]
    current = data[start:end]
    for orig_hex, tuned_hex in region["exact"]:
        if current.hex() == orig_hex:
            return bytes.fromhex(tuned_hex), "exact", []
    if region["kind"] != "calibration":
        raise GenerationRejected(
            "patch_region_mismatch",
            f"Gebied 0x{start:X}-0x{end:X} is geen herkenbare kalibratietabel en de "
            "bytes in dit bestand wijken af van alle trainingsoriginelen. Een wijziging "
            "die niet als kaart verklaard is, wordt niet op afwijkende inhoud toegepast.",
            {"start": start, "end": end},
        )
    tolerance = settings["applicability_tolerance"]
    margin = settings["limit_margin"]
    out = bytearray(current)
    issues: list[str] = []
    methods = set()
    for index, rule in enumerate(region["cells"]):
        if rule is None:
            continue
        offset = index * 2
        value = _unpack(current[offset:offset + 2], region["endian"])
        lo = rule["orig_min"] * (1 - tolerance)
        hi = rule["orig_max"] * (1 + tolerance)
        if not (lo <= value <= hi or value == rule["orig_min"]):
            issues.append(f"cel {index}: {value} buiten geleerd bereik {rule['orig_min']}..{rule['orig_max']}")
            continue
        if rule["method"] == "ratio" and rule["ratio"] is not None:
            new = round(value * rule["ratio"])
        else:
            new = round(value + rule["delta"])
        t_lo = rule["tuned_min"] * (1 - margin)
        t_hi = rule["tuned_max"] * (1 + margin)
        new = int(min(max(new, t_lo), t_hi, 0xFFFF))
        new = max(new, 0)
        out[offset:offset + 2] = _pack(new, region["endian"])
        methods.add(rule["method"])
    if issues:
        raise GenerationRejected(
            "cell_out_of_range",
            f"Kaart 0x{start:X}-0x{end:X}: de waarden in dit bestand vallen buiten het "
            f"geleerde toepassingsbereik ({len(issues)} cellen, bijv. {issues[0]}).",
            {"start": start, "end": end, "issues": issues[:20]},
        )
    return bytes(out), "+".join(sorted(methods)) or "exact", []


def generate(data: bytes, profile_name: str, package: dict[str, Any], contracts: dict | None = None) -> GenerationResult:
    classification = classify_input(data)
    if classification.kind != "raw_image":
        raise GenerationRejected(f"input_{classification.kind}", classification.message)
    family, variant, identity = _select_family(package, data)
    profile = family["profiles"].get(profile_name)
    if profile is None:
        available = ", ".join(PROFILE_LABELS.get(p, p) for p in family["profiles"]) or "geen"
        raise GenerationRejected(
            "profile_not_available",
            f"Profiel {PROFILE_LABELS.get(profile_name, profile_name)} is voor deze familie "
            f"niet getraind (beschikbaar: {available}).",
        )
    if not profile["regions"]:
        raise GenerationRejected("empty_profile", "Het profiel bevat geen geleerde wijzigingen.")
    _check_mixed_input(family, profile, data)
    settings = package["settings"]
    output = bytearray(data)
    changes = []
    failures = []
    for region in profile["regions"]:
        start, end = region["start"], region["end"]
        if not (
            _context_matches(data, max(0, start - len(region["context_before"]) // 2),
                             region["context_before"], region["context_before_mask"])
            and _context_matches(data, end, region["context_after"], region["context_after_mask"])
        ):
            failures.append(GenerationRejected(
                "layout_mismatch",
                f"Rond gebied 0x{start:X}-0x{end:X} wijkt de kaartindeling af van de "
                "trainingsdata; de doelkaart is in dit bestand niet op de bewezen plek gevonden.",
                {"start": start, "end": end},
            ))
            continue
        try:
            new_bytes, method, _ = _apply_region(region, data, settings)
        except GenerationRejected as exc:
            failures.append(exc)
            continue
        output[start:end] = new_bytes
        changed_cells = sum(
            1 for i in range(0, end - start, 2) if new_bytes[i:i + 2] != data[start + i:start + i + 2]
        )
        changes.append({
            "start": start,
            "end": end,
            "kind": region["kind"],
            "method": method,
            "changed_bytes": sum(1 for a, b in zip(new_bytes, data[start:end]) if a != b),
            "changed_cells": changed_cells,
            "map": region.get("map"),
            "label": (
                region["map"]["description"] if region.get("map") else
                f"{'Kaart' if region['kind'] == 'calibration' else 'Patch'} 0x{start:X}"
            ),
        })
    if failures:
        first = failures[0]
        raise GenerationRejected(
            first.code,
            first.message + (
                f" ({len(failures)} van {len(profile['regions'])} samenhangende gebieden niet "
                "toepasbaar; Stage 1 wordt alleen als geheel toegepast.)"
                if len(failures) > 1 else ""
            ),
            {"failures": [{"code": f.code, "message": f.message} for f in failures[:50]]},
        )
    contract = (contracts or {}).get(family["family_key"], {"contract": "UNKNOWN"})
    integrity = {
        "reference_checksum_states": family["integrity"]["reference_checksum_states"],
        "contract": contract.get("contract", "UNKNOWN"),
        "contract_evidence": contract.get("evidence", ""),
        "ready_for_use": contract.get("contract") == "FLASH_TOOL_CORRECTS_ON_WRITE",
        "remaining_step": (
            "Checksumcorrectie bij het schrijven door het flashprogramma (AutoTuner), "
            "zoals door de gebruiker vastgelegd."
            if contract.get("contract") == "FLASH_TOOL_CORRECTS_ON_WRITE"
            else "Integriteitsmethode onbekend: export geblokkeerd."
        ),
    }
    out_bytes = bytes(output)
    return GenerationResult(
        output=out_bytes,
        family_key=family["family_key"],
        variant_key=variant["variant_key"],
        profile=profile_name,
        package_id=package["package_id"],
        input_sha256=hashlib.sha256(data).hexdigest(),
        output_sha256=hashlib.sha256(out_bytes).hexdigest(),
        changes=changes,
        allowed_ranges=[(r["start"], r["end"]) for r in profile["regions"]],
        identity={"family": family["ecu_type"], "variant": variant["variant_key"], "match": identity},
        evidence_level=variant.get("evidence_level", "REPRODUCTION_ONLY"),
        integrity=integrity,
    )
