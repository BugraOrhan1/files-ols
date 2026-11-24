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

from .identity import _useful, classify_input, token_in
from .compose import join, offered, parts_of
from .exact import known_versions
from .ols_versions import profile_label
from .package import contract_for


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


def _select_variant(package, data: bytes) -> tuple[dict, dict, dict]:
    """Pick the software variant by size and identity tokens found in the bytes."""

    cache: dict[str, bool] = {}

    def has(token: str) -> bool:
        if token not in cache:
            cache[token] = token_in(token, data)
        return cache[token]

    def check(identity: dict[str, list[str]]) -> dict[str, dict[str, list[str]]]:
        result = {}
        for name, tokens in identity.items():
            tokens = [t for t in tokens if _useful(t)]  # drop junk such as '!!!!!!' from older libraries
            if tokens:
                result[name] = {"found": [t for t in tokens if has(t)], "missing": [t for t in tokens if not has(t)]}
        return result

    full = []
    recognized: dict[str, tuple[tuple[int, int], dict]] = {}
    partial = []
    for family, variant in package.variants():
        ecu_tokens = [t for t in family["family_identity"].get("ecu_type", []) if _useful(t)]
        ecu_found = bool(ecu_tokens) and all(has(t) for t in ecu_tokens)
        result = check(variant["identity"])
        missing = [t for v in result.values() for t in v["missing"]]
        found = sum(len(v["found"]) for v in result.values())
        if ecu_found or found:
            key = family["family_key"]
            score = (int(ecu_found), found)
            if key not in recognized or score > recognized[key][0]:
                recognized[key] = (score, family)
        if variant["size"] != len(data):
            continue
        if not result:
            continue  # no usable identification: only reachable through similarity transfer
        if not missing:
            full.append((-found, variant["variant_id"], family, variant, result))
        elif ecu_found or found:
            partial.append((variant["variant_id"], family, variant, result))
    if full:
        full.sort(key=lambda item: (item[0], item[1]))
        _, _, family, variant, result = full[0]
        return family, variant, result
    if not recognized:
        known = len({f["ecu_type"] or f["family_key"] for f in package["families"]})
        raise GenerationRejected(
            "ecu_not_recognized",
            f"De ECU van dit bestand is niet herkend ({known} ECU-typen in de bibliotheek). "
            "Voeg WinOLS-projecten van deze ECU met getunede versies toe "
            "en werk de bibliotheek bij.",
        )
    if not partial:
        # Name the best-matching family (ECU type found, most identity tokens), not the alphabetically first.
        top_score = max(score for score, _f in recognized.values())
        best = sorted((f for score, f in recognized.values() if score == top_score), key=lambda f: f["family_key"])
        family = best[0]
        sizes = ", ".join(sorted({str(v["size"]) for f in best for v in f["variants"]}))
        raise GenerationRejected(
            "unknown_segment_layout",
            f"De ECU-familie {family['ecu_type'] or family['family_key']} is herkend, maar de "
            f"bestandsgrootte ({len(data)} bytes) past bij geen bewezen leesvorm/segmentindeling "
            f"({sizes} bytes). Mogelijk is dit een andere uitleesvorm (bijv. alleen kalibratie of "
            "volledige flash) die nog niet gevalideerd is.",
        )
    partial.sort(key=lambda item: (
        -sum(len(v["found"]) for v in item[3].values()), item[0]))
    _, family, variant, result = partial[0]
    hw_missing = [t for f in ("hw_number", "hw_code") for t in result.get(f, {}).get("missing", [])]
    sw_missing = [t for f in ("sw_number", "sw_version") for t in result.get(f, {}).get("missing", [])]
    if hw_missing and not sw_missing:
        raise GenerationRejected(
            "other_hardware",
            "De ECU-familie is herkend, maar het hardwarenummer wijkt af "
            f"(niet gevonden: {', '.join(hw_missing)}). Voor deze hardware is het profiel niet bewezen.",
        )
    raise GenerationRejected(
        "software_variant_not_validated",
        "De ECU-familie en bestandsindeling zijn herkend, maar deze softwarevariant staat niet "
        f"in het pakket (niet gevonden: {', '.join(sw_missing or hw_missing)}). De kaartindeling "
        "voor deze variant is nog niet gevalideerd; werk de bibliotheek bij met projecten van "
        "deze software.",
    )


def _check_zones(blob: dict[str, Any], data: bytes, profile_name: str) -> None:
    zone_hits = sorted({
        zone["label"] for zone in blob.get("modification_zones", [])
        if zone.get("profile") != profile_name
        and len(zone["tuned"]) >= 16  # >= 8 bytes: shorter zones match by coincidence
        and len(set(bytes.fromhex(zone["tuned"]))) >= 3
        and data[zone["start"]:zone["end"]].hex() == zone["tuned"]
        and zone["tuned"] != zone["original"]
    })
    if zone_hits:
        raise GenerationRejected(
            "other_modification_present",
            "Het bestand bevat al een andere bewerking (" + ", ".join(zone_hits[:5])
            + "). Open het ongewijzigde origineel.",
        )


def _edits_match(data: bytes, edits: list, column: int) -> bool:
    for edit in edits:
        start, expected = edit[0], edit[column]
        if data[start:start + len(expected) // 2].hex() != expected:
            return False
    return True


def _known_pair(known: list[dict[str, Any]], data: bytes, input_sha: str,
                profile_name: str = "stage1") -> dict[str, Any] | None:
    """Return the training pair whose original matches this input in every
    byte that pair's Stage 1 changes (identical original -> identical tune)."""

    for pair in known:
        if pair["edits"] and _edits_match(data, pair["edits"], 2) and not _edits_match(data, pair["edits"], 1):
            raise GenerationRejected(
                "already_tuned",
                f"Dit bestand is al bewerkt: het bevat precies de {profile_label(profile_name)}-wijzigingen "
                f"van project {pair.get('project_name') or pair['pair_id']}. Open het originele bestand.",
            )
    matches = [p for p in known if p["edits"] and _edits_match(data, p["edits"], 1)]
    if not matches:
        return None
    exact = [p for p in matches if p.get("original_sha256") == input_sha]
    pool = exact or matches
    counts: dict[str, int] = {}
    for p in pool:
        counts[p["tuned_sha256"]] = counts.get(p["tuned_sha256"], 0) + 1
    pool.sort(key=lambda p: (-counts[p["tuned_sha256"]], -len(p["edits"]), p["pair_id"]))
    choice = dict(pool[0])
    choice["alternatives"] = len({p["tuned_sha256"] for p in pool}) - 1
    return choice


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


def _layout_confirmed(region: dict[str, Any], data: bytes) -> bool:
    start, end = region["start"], region["end"]
    return (_context_matches(data, max(0, start - len(region["context_before"]) // 2),
                             region["context_before"], region["context_before_mask"])
            and _context_matches(data, end, region["context_after"], region["context_after_mask"]))


def _check_mixed_input(blob: dict[str, Any], profile: dict[str, Any], data: bytes, profile_name: str) -> None:
    label = profile_label(profile_name)
    tuned_hits = 0
    original_hits = 0
    for region in profile["regions"]:
        current = data[region["start"]:region["end"]].hex()
        tuned_values = {t for o, t in region["exact"]}
        orig_values = {o for o, t in region["exact"]}
        if current in tuned_values and current not in orig_values:
            raw = bytes.fromhex(current)
            # repeated fill patterns (e.g. 0080 0080 ...) occur everywhere and prove nothing
            if len(set(raw)) >= 4 and (_layout_confirmed(region, data) or len(raw) >= 16):
                tuned_hits += 1
        elif current in orig_values:
            original_hits += 1
        elif _layout_confirmed(region, data) and _region_looks_tuned(region, data):
            # value-level judgement only where the map is at its proven place
            tuned_hits += 1
    total = len(profile["regions"]) or 1
    if tuned_hits and tuned_hits / total >= 0.5:
        raise GenerationRejected(
            "already_tuned",
            f"Dit bestand is al bewerkt: {tuned_hits} van {total} geleerde gebieden van {label} "
            "bevatten al de gewijzigde waarden. Open het originele bestand.",
        )
    _check_zones(blob, data, profile_name)
    if tuned_hits:
        raise GenerationRejected(
            "partially_tuned",
            f"{tuned_hits} van {total} gebieden van {label} zijn al gewijzigd en de rest niet. "
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


def available_profiles(data: bytes, package, hint: str = "", min_similarity: float | None = None,
                       family_floor: dict[str, float] | None = None) -> tuple[dict | None, dict | None, list[str]]:
    """Profiles for this file: those of its exact software variant plus those
    of similar originals in the library (best first)."""

    from .ols_versions import profile_sort_key
    from .transfer import DEFAULT_MIN_SIMILARITY, transfer_profiles

    classification = classify_input(data)
    if classification.kind != "raw_image":
        raise GenerationRejected(f"input_{classification.kind}", classification.message)
    family = variant = None
    profiles: list[str] = []
    error = None
    try:
        family, variant, _identity = _select_variant(package, data)
        profiles = list(variant["profiles"])
    except GenerationRejected as exc:
        error = exc
    same = known_versions(data, package)
    profiles = list(dict.fromkeys([v["profile"] for v in same] + profiles))
    similar, donors = transfer_profiles(package, data, hint, min_similarity or DEFAULT_MIN_SIMILARITY, family_floor)
    for profile in similar:
        if profile not in profiles:
            profiles.append(profile)
    if not profiles:
        if error is not None:
            raise GenerationRejected(error.code, error.message + " Er is ook geen vergelijkbaar origineel "
                                     "(voldoende gelijk, ook niet bij andere bestandsgrootte) in de bibliotheek.")
        raise GenerationRejected("profile_not_available", "Geen tunings beschikbaar voor dit bestand.")
    if family is None and donors:
        family = next((f for f in package["families"] for v in f["variants"]
                       if v["variant_id"] == donors[0]["variant_id"]), None)
    profiles.extend(offered(profiles))
    return family, variant, sorted(profiles, key=profile_sort_key)


def _integrity(family: dict[str, Any], contracts: dict | None) -> dict[str, Any]:
    contract = contract_for(contracts, family["family_key"])
    ready = contract.get("contract") == "FLASH_TOOL_CORRECTS_ON_WRITE"
    return {
        "reference_checksum_states": family["integrity"]["reference_checksum_states"],
        "contract": contract.get("contract", "UNKNOWN"),
        "contract_evidence": contract.get("evidence", ""),
        "ready_for_use": ready,
        "remaining_step": (
            "Checksumcorrectie bij het schrijven door het flashprogramma (AutoTuner), "
            "zoals door de gebruiker vastgelegd."
            if ready else "Integriteitsmethode onbekend: export geblokkeerd."
        ),
    }


_NO_NAME = {"", "ONBEKEND", "UNKNOWN", "UNDEFINED"}


def _named_family(data: bytes, package, family: dict[str, Any]) -> dict[str, Any]:
    """The ECU family to apply the checksum rule of.

    A project without an ECU type says nothing about the ECU. The file does:
    when all numbers of a named software variant occur in it (the same proof
    as for exact recognition), that variant's family is used."""

    if family["family_key"].rpartition("|")[0].upper() not in _NO_NAME:
        return family
    try:
        named, _variant, _identity = _select_variant(package, data)
    except GenerationRejected:
        return family
    return family if named["family_key"].rpartition("|")[0].upper() in _NO_NAME else named


def _generate_known(data: bytes, profile_name: str, package, contracts: dict | None,
                    pair_id: str | None = None) -> GenerationResult | None:
    """The tune of this exact original; ``pair_id`` picks one of its versions."""

    versions = [v for v in known_versions(data, package) if v["profile"] == profile_name]
    if pair_id is not None:
        versions = [v for v in versions if v["pair_id"] == pair_id]
    if not versions:
        return None
    chosen = versions[0]
    family, variant = _family_of(package, chosen["variant_id"])
    family = _named_family(data, package, family)
    blob = package.load_variant(chosen["variant_id"])
    _check_zones(blob, data, profile_name)
    output = bytearray(data)
    changes = []
    for edit in chosen["pair"]["edits"]:
        start, orig_hex, tuned_hex = edit[0], edit[1], edit[2]
        new = bytes.fromhex(tuned_hex)
        output[start:start + len(new)] = new
        changes.append({
            "start": start, "end": start + len(new), "kind": "known_pair", "method": "bekend paar",
            "changed_bytes": sum(1 for a, b in zip(new, bytes.fromhex(orig_hex)) if a != b),
            "changed_cells": None, "map": None, "label": f"Wijziging 0x{start:X}"})
    notes = [f"Exact hetzelfde origineel als in project {chosen['project']}; de versie '{chosen['name']}' daarvan "
             "wordt gebruikt."]
    if len(versions) > 1:
        notes.append(f"Er bestaan {len(versions) - 1} andere varianten van {profile_label(profile_name)} voor dit "
                     "origineel: " + "; ".join(
            f"'{v['name']}' ({v['project']})" for v in versions[1:6]) + ".")
    input_sha = hashlib.sha256(data).hexdigest()
    return GenerationResult(
        output=bytes(output), family_key=family["family_key"], variant_key=variant["variant_key"],
        profile=profile_name, package_id=package["package_id"], input_sha256=input_sha,
        output_sha256=hashlib.sha256(bytes(output)).hexdigest(), changes=changes,
        allowed_ranges=[(c["start"], c["end"]) for c in changes],
        identity={"family": family["ecu_type"], "variant": variant["variant_key"],
                  "known_pair": chosen["pair_id"], "original_sha256_equal": True},
        evidence_level="KNOWN_PAIR", integrity=_integrity(family, contracts), notes=notes)


def _generate_exact(data: bytes, profile_name: str, package, contracts: dict | None = None) -> GenerationResult:
    family, variant, identity = _select_variant(package, data)
    blob = package.load_variant(variant["variant_id"])
    profile = blob["profiles"].get(profile_name)
    known = blob["known_pairs"].get(profile_name, [])
    if profile is None and not known:
        available = ", ".join(profile_label(p) for p in blob["profiles"]) or "geen"
        raise GenerationRejected(
            "profile_not_available",
            f"Profiel {profile_label(profile_name)} is voor deze softwarevariant "
            f"niet getraind (beschikbaar: {available}).",
        )
    input_sha = hashlib.sha256(data).hexdigest()
    pair = _known_pair(known, data, input_sha, profile_name)  # raises already_tuned first
    _check_zones(blob, data, profile_name)
    if pair is not None:
        output = bytearray(data)
        changes = []
        for edit in pair["edits"]:
            start, orig_hex, tuned_hex = edit[0], edit[1], edit[2]
            new = bytes.fromhex(tuned_hex)
            output[start:start + len(new)] = new
            changes.append({
                "start": start, "end": start + len(new), "kind": "known_pair", "method": "bekend paar",
                "changed_bytes": sum(1 for a, b in zip(new, bytes.fromhex(orig_hex)) if a != b),
                "changed_cells": None, "map": None, "label": f"Wijziging 0x{start:X}",
            })
        out_bytes = bytes(output)
        version = pair.get("tuned_version_name") or profile_label(profile_name)
        notes = [
            f"Komt overeen met project {pair.get('project_name') or pair['pair_id']} "
            f"(alle {len(pair['edits'])} gewijzigde gebieden gelijk); de versie '{version}' uit dat project wordt gebruikt."
        ]
        if pair["alternatives"]:
            notes.append(f"Er bestaan {pair['alternatives']} andere varianten van {profile_label(profile_name)} voor dit origineel; "
                         "de meest voorkomende is gekozen.")
        return GenerationResult(
            output=out_bytes, family_key=family["family_key"], variant_key=variant["variant_key"],
            profile=profile_name, package_id=package["package_id"], input_sha256=input_sha,
            output_sha256=hashlib.sha256(out_bytes).hexdigest(), changes=changes,
            allowed_ranges=[(c["start"], c["end"]) for c in changes],
            identity={"family": family["ecu_type"], "variant": variant["variant_key"], "match": identity,
                      "known_pair": pair["pair_id"], "original_sha256_equal": pair.get("original_sha256") == input_sha},
            evidence_level="KNOWN_PAIR", integrity=_integrity(family, contracts), notes=notes,
        )
    if profile is None or not profile["regions"]:
        raise GenerationRejected("empty_profile", "Het profiel bevat geen geleerde wijzigingen.")
    _check_mixed_input(blob, profile, data, profile_name)
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
                "toepasbaar; een tuning wordt alleen als geheel toegepast.)"
                if len(failures) > 1 else ""
            ),
            {"failures": [{"code": f.code, "message": f.message} for f in failures[:50]]},
        )
    integrity = _integrity(family, contracts)
    out_bytes = bytes(output)
    return GenerationResult(
        output=out_bytes,
        family_key=family["family_key"],
        variant_key=variant["variant_key"],
        profile=profile_name,
        package_id=package["package_id"],
        input_sha256=input_sha,
        output_sha256=hashlib.sha256(out_bytes).hexdigest(),
        changes=changes,
        allowed_ranges=[(r["start"], r["end"]) for r in profile["regions"]],
        identity={"family": family["ecu_type"], "variant": variant["variant_key"], "match": identity},
        evidence_level=variant.get("evidence_level", "REPRODUCTION_ONLY"),
        integrity=integrity,
    )


def _family_of(package, variant_id: str) -> tuple[dict, dict]:
    for family, variant in package.variants():
        if variant["variant_id"] == variant_id:
            return family, variant
    raise KeyError(variant_id)


def _by_transfer(data: bytes, profile_name: str, package, contracts: dict | None, hint: str,
                 min_similarity: float | None, min_coverage: float | None,
                 earlier: GenerationRejected | None, family_floor: dict[str, float] | None = None) -> GenerationResult:
    from .transfer import DEFAULT_MIN_COVERAGE, DEFAULT_MIN_SIMILARITY, second_opinions, transfer

    outcome = transfer(data, profile_name, package, hint=hint, family_floor=family_floor,
                       min_similarity=min_similarity or DEFAULT_MIN_SIMILARITY,
                       min_coverage=DEFAULT_MIN_COVERAGE if min_coverage is None else min_coverage)
    if outcome["status"] != "ok":
        if outcome["code"] in ("already_tuned", "partially_tuned"):
            raise GenerationRejected(outcome["code"], outcome["reason"])
        prefix = (earlier.message + " ") if earlier is not None else ""
        code = outcome["code"]
        if earlier is not None and (code == "no_similar_original" or earlier.code in (
                "cell_out_of_range", "layout_mismatch", "patch_region_mismatch")):
            code = earlier.code  # the recognised variant's own finding stays the headline
        raise GenerationRejected(code, prefix + "Overzetten: " + outcome["reason"], outcome)
    donor = outcome["donor"]
    family, variant = _family_of(package, donor["variant_id"])
    family = _named_family(data, package, family)
    blob = package.load_variant(donor["variant_id"])
    _check_zones(blob, data, profile_name)
    how_counts: dict[str, int] = {}
    for item in outcome["placed"]:
        how_counts[item["how"]] = how_counts.get(item["how"], 0) + 1
    pair = outcome["pair"]
    notes = [
        f"Overgezet van {donor.get('vehicle') or 'project'} ({donor.get('project', '')}): "
        f"{donor['similarity']:.1%} gelijk aan uw bestand"
        + (f" (op dezelfde positie {donor['same_offset_similarity']:.0%}, rest verschoven)"
           if donor.get("same_offset_similarity", donor["similarity"]) + 0.01 < donor["similarity"] else "")
        + f"; versie '{pair.get('tuned_version_name') or profile_label(profile_name)}'.",
        f"{len(outcome['placed'])} van {len(pair['edits'])} wijzigingen geplaatst ("
        + ", ".join(f"{n} {how}" for how, n in sorted(how_counts.items())) + ").",
    ]
    if outcome.get("integrity_fields"):
        notes.append(f"{len(outcome['integrity_fields'])} checksum-/handtekeningveld(en) van het voorbeeld niet "
                     "overgenomen: die horen bij één bestand en moeten door het flashprogramma worden berekend.")
    opinion = second_opinions(data, profile_name, package, outcome, hint=hint, family_floor=family_floor,
                              min_similarity=min_similarity or DEFAULT_MIN_SIMILARITY)
    if opinion["contradicted"]:
        notes.insert(0, f"WAARSCHUWING: {opinion['opinions']} andere voorbeelden spreken deze tune deels tegen "
                        f"({opinion['agreement']:.0%} dezelfde richting). Controleer hem eerst in WinOLS.")
    elif opinion["opinions"] == 0:
        notes.append("Er is geen tweede voorbeeld om deze tune mee te vergelijken.")
    else:
        notes.append(f"Vergeleken met {opinion['opinions']} ander(e) voorbeeld(en): {opinion['agreement']:.0%} van de "
                     "gedeelde wijzigingen gaat dezelfde kant op.")
    if donor.get("identity_confirmed") == "full":
        notes.append("Zelfde software- en hardwarenummers als het voorbeeld (andere uitleesvorm); "
                     "alle wijzigingen op identieke originele bytes geplaatst.")
    elif donor.get("identity_confirmed") == "software":
        notes.append("Zelfde softwarenummer als het voorbeeld, ander hardwarenummer; "
                     "alle wijzigingen op identieke originele bytes geplaatst.")
    elif donor.get("exact_required"):
        notes.append(f"Grotere uitlezing als voorbeeld ({donor['size']} bytes); het gedeelte dat in uw "
                     "bestand kan staan komt overeen en alle wijzigingen staan op identieke originele bytes.")
    if outcome["missing"]:
        notes.append(f"Let op: {len(outcome['missing'])} wijzigingen niet geplaatst "
                     f"({outcome['coverage']:.1%} overgezet) – volgens uw ingestelde minimum toegestaan.")
    if how_counts.get("omgerekend"):
        notes.append("Omgerekende kaarten: waarden van uw bestand met dezelfde verhouding aangepast als in het voorbeeld.")
    if how_counts.get("kaart via assen"):
        notes.append(f"{how_counts['kaart via assen']} kaart(en) gevonden via hun assen (andere plek of andere "
                     "omgeving dan in het voorbeeld) en met dezelfde verhouding per cel aangepast.")
    changes = [{
        "start": item["start"], "end": item["end"], "kind": "transfer", "method": item["how"],
        "changed_bytes": item["changed_bytes"], "changed_cells": None, "map": item.get("map"),
        "label": f"Wijziging 0x{item['start']:X}" + (f" (voorbeeld 0x{item['donor_start']:X})"
                                                     if item["start"] != item["donor_start"] else ""),
    } for item in outcome["placed"]]
    output = outcome["output"]
    return GenerationResult(
        output=output, family_key=family["family_key"], variant_key=variant["variant_key"],
        profile=profile_name, package_id=package["package_id"],
        input_sha256=hashlib.sha256(data).hexdigest(), output_sha256=outcome["output_sha256"],
        changes=changes, allowed_ranges=[(c["start"], c["end"]) for c in changes],
        identity={"family": family["ecu_type"], "variant": variant["variant_key"],
                  "donor_project": donor.get("project"), "donor_vehicle": donor.get("vehicle"),
                  "similarity": round(donor["similarity"], 4), "coverage": round(outcome["coverage"], 4)},
        evidence_level="DONOR_TRANSFER", integrity=_integrity(family, contracts), notes=notes,
    )


def generate(data: bytes, profile_name: str, package, contracts: dict | None = None, *,
             hint: str = "", min_similarity: float | None = None,
             min_coverage: float | None = None, family_floor: dict[str, float] | None = None) -> GenerationResult:
    """The tune itself when the library has it; a combination without an
    example is joined from its separate tunes."""

    options = {"hint": hint, "min_similarity": min_similarity, "min_coverage": min_coverage,
               "family_floor": family_floor}
    profile_name, _at, pair_id = profile_name.partition("@")
    if pair_id:
        result = _generate_known(data, profile_name, package, contracts, pair_id)
        if result is None:
            raise GenerationRejected("version_not_found", "Deze versie hoort niet bij dit origineel.")
        return result
    try:
        return _generate_single(data, profile_name, package, contracts, **options)
    except GenerationRejected as exc:
        parts = parts_of(profile_name)
        if not parts or exc.code in ("already_tuned", "partially_tuned", "other_modification_present") \
                or exc.code.startswith("input_"):
            raise
        return _compose(data, profile_name, parts, package, contracts, exc, options)


def _compose(data: bytes, profile_name: str, parts: list[str], package, contracts: dict | None,
             earlier: GenerationRejected, options: dict[str, Any]) -> GenerationResult:
    results = []
    for part in parts:
        try:
            results.append(_generate_single(data, part, package, contracts, **options))
        except GenerationRejected as exc:
            raise GenerationRejected(earlier.code, f"{earlier.message} Samenstellen uit losse delen kan ook niet: "
                                     f"{profile_label(part)}: {exc.message}") from exc
    output, conflicts = join(data, [r.output for r in results])
    if conflicts:
        raise GenerationRejected(
            "combination_conflict",
            f"Samenstellen uit losse delen kan niet: {len(conflicts)} waarden worden door twee delen verschillend "
            f"gezet (bijv. 0x{min(conflicts):X}). Er is geen voorbeeld van deze combinatie zelf.",
            {"cells": conflicts[:20]})
    notes = [f"Samengesteld uit losse delen ({', '.join(profile_label(p) for p in parts)}): er is geen voorbeeld van "
             "deze combinatie. De delen spreken elkaar niet tegen; hun samenwerking is niet bewezen. "
             "Controleer het resultaat in WinOLS."]
    for part, result in zip(parts, results):
        notes.extend(f"{profile_label(part)}: {note}" for note in result.notes)
    changes = [dict(c, label=f"{profile_label(part)}: {c.get('label') or ''}".strip())
               for part, result in zip(parts, results) for c in result.changes]
    base = next((r for p, r in zip(parts, results) if p.startswith("stage")), results[0])
    ready = all(r.integrity["ready_for_use"] for r in results)
    integrity = next((r.integrity for r in results if not r.integrity["ready_for_use"]), base.integrity) \
        if not ready else base.integrity
    return GenerationResult(
        output=output, family_key=base.family_key, variant_key=base.variant_key, profile=profile_name,
        package_id=base.package_id, input_sha256=base.input_sha256,
        output_sha256=hashlib.sha256(output).hexdigest(), changes=changes,
        allowed_ranges=sorted({tuple(x) for r in results for x in r.allowed_ranges}),
        identity=dict(base.identity, parts=parts), evidence_level="COMBINED", integrity=integrity, notes=notes)


def _generate_single(data: bytes, profile_name: str, package, contracts: dict | None = None, *,
                     hint: str = "", min_similarity: float | None = None,
                     min_coverage: float | None = None,
                     family_floor: dict[str, float] | None = None) -> GenerationResult:
    """Exact software variant first; otherwise transfer from the most similar original."""

    classification = classify_input(data)
    if classification.kind != "raw_image":
        raise GenerationRejected(f"input_{classification.kind}", classification.message)
    known = _generate_known(data, profile_name, package, contracts)
    if known is not None:
        return known
    try:
        return _generate_exact(data, profile_name, package, contracts)
    except GenerationRejected as exc:
        # Byte-exact proof that the file is already tuned is final. Everything
        # else (other software, moved maps, value heuristics) gets a transfer try.
        # Byte-exact or value-range proof from the recognised software variant
        # that the file is (partly) tuned is final: never transfer onto it.
        if exc.code in ("already_tuned", "partially_tuned", "other_modification_present") \
                or exc.code.startswith("input_"):
            raise
        try:
            return _by_transfer(data, profile_name, package, contracts, hint, min_similarity, min_coverage, exc,
                                family_floor)
        except GenerationRejected:
            if exc.code in ("partially_tuned", "other_modification_present"):
                raise exc  # the exact-variant finding is the more specific reason
            raise
