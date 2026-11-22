"""Learn coherent Stage 1 edits from confirmed Original -> tuned pairs.

Method (deterministic, explainable, derived only from training data):

1. Every pair is diffed into *edit regions* (changed bytes merged when they
   are closer than ``merge_gap``).
2. Regions of all pairs in one family are united. A united region becomes part
   of the profile when it changed in at least ``min_support`` of the pairs:
   that is the learned "which maps change together" set.
3. Each region is classified. *Calibration* regions hold smooth 16-bit cell
   data (checked in both byte orders on the original's neighbourhood); other
   regions are *patch* regions (switches, code, unexplained bytes).
4. Per calibration cell the observed originals/tuned values give an exact
   mapping plus a transfer rule (``ratio`` or ``delta``) chosen by
   leave-one-pair-out error when at least two pairs carry different originals;
   otherwise the configured default. Limits come from the observed tuned
   values; applicability from the observed originals plus a tolerance.
5. Patch regions are only ever applied verbatim to identical input bytes.
"""

from __future__ import annotations

import statistics
import struct
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_SETTINGS: dict[str, Any] = {
    "merge_gap": 16,
    "context_bytes": 32,
    "min_support": 0.5,
    "single_pair_transfer": "ratio",
    "applicability_tolerance": 0.15,
    "limit_margin": 0.05,
    "smoothness_threshold": 0.2,
}


@dataclass
class TrainingPair:
    pair_id: str
    family_key: str
    variant_key: str
    identity: dict[str, list[str]]
    original: bytes
    tuned: bytes
    profile: str
    maps: list[dict[str, Any]] = field(default_factory=list)
    record: dict[str, Any] = field(default_factory=dict)


def diff_regions(original: bytes, tuned: bytes, merge_gap: int) -> list[tuple[int, int]]:
    if len(original) != len(tuned):
        raise ValueError("Origineel en getunede versie hebben een verschillende lengte.")
    regions: list[tuple[int, int]] = []
    start = end = -1
    # Compare in large blocks first; only scan bytes inside differing blocks.
    block = 4096
    for base in range(0, len(original), block):
        a = original[base:base + block]
        b = tuned[base:base + block]
        if a == b:
            continue
        for offset in range(len(a)):
            if a[offset] != b[offset]:
                position = base + offset
                if start < 0:
                    start = end = position
                elif position - end <= merge_gap:
                    end = position
                else:
                    regions.append((start, end + 1))
                    start = end = position
    if start >= 0:
        regions.append((start, end + 1))
    return regions


def merge_intervals(intervals: Iterable[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for s, e in sorted(intervals):
        if merged and s - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _align(start: int, end: int, size: int) -> tuple[int, int]:
    start -= start % 2
    end += end % 2
    return max(0, start), min(size, end)


def _words(data: bytes, endian: str) -> list[int]:
    fmt = "<" if endian == "little" else ">"
    count = len(data) // 2
    return list(struct.unpack(f"{fmt}{count}H", data[: count * 2]))


def _roughness(values: list[int]) -> float:
    if len(values) < 3:
        return 1.0
    span = max(values) - min(values)
    if span == 0:
        return 0.0
    steps = [abs(values[i + 1] - values[i]) for i in range(len(values) - 1)]
    return (sum(steps) / len(steps)) / span


def classify_region(original: bytes, start: int, end: int, settings: dict[str, Any]) -> dict[str, Any]:
    # Judge the region's own original cells; neighbouring code or axis data
    # would otherwise dominate short tables.
    window = original[start:end]
    rough = {e: _roughness(_words(window, e)) for e in ("little", "big")}
    endian = min(rough, key=lambda e: (rough[e], e))
    smooth = rough[endian] <= settings["smoothness_threshold"]
    kind = "calibration" if smooth and end - start >= 4 else "patch"
    return {"kind": kind, "endian": endian, "roughness": round(rough[endian], 4)}


def _map_for(region: tuple[int, int], maps: list[dict[str, Any]]) -> dict[str, Any] | None:
    s, e = region
    for item in maps:
        width = 2 if item.get("data_type", "").upper().endswith("WORD") else 1
        m_start = item["data_offset"]
        m_end = m_start + item["columns"] * item["rows"] * width
        if s < m_end and m_start < e:
            return item
    return None


def _median(values: list[float]) -> float:
    return float(statistics.median(values))


def _cell_rule(origs: list[int], tuneds: list[int], settings: dict[str, Any]) -> dict[str, Any]:
    ratios = [t / o for o, t in zip(origs, tuneds) if o]
    deltas = [t - o for o, t in zip(origs, tuneds)]
    rule: dict[str, Any] = {
        "orig_min": min(origs), "orig_max": max(origs),
        "tuned_min": min(tuneds), "tuned_max": max(tuneds),
        "ratio": round(_median(ratios), 6) if ratios else None,
        "delta": round(_median(deltas), 3),
    }
    method = settings["single_pair_transfer"]
    if len(set(origs)) >= 2 and len(origs) >= 2:
        errors = {}
        for candidate in ("ratio", "delta"):
            total = 0.0
            for k in range(len(origs)):
                rest_o = origs[:k] + origs[k + 1:]
                rest_t = tuneds[:k] + tuneds[k + 1:]
                if candidate == "ratio":
                    rr = [t / o for o, t in zip(rest_o, rest_t) if o]
                    predicted = origs[k] * _median(rr) if rr else origs[k]
                else:
                    predicted = origs[k] + _median([t - o for o, t in zip(rest_o, rest_t)])
                total += abs(predicted - tuneds[k])
            errors[candidate] = total
        method = min(errors, key=lambda c: (errors[c], c))
        rule["loo_error"] = {k: round(v, 3) for k, v in errors.items()}
    if method == "ratio" and rule["ratio"] is None:
        method = "delta"
    rule["method"] = method
    return rule


def learn_profile(pairs: list[TrainingPair], settings: dict[str, Any]) -> dict[str, Any]:
    """Learn one profile for one family from compatible pairs."""

    if not pairs:
        raise ValueError("Geen trainingsparen.")
    size = len(pairs[0].original)
    if any(len(p.original) != size or len(p.tuned) != size for p in pairs):
        raise ValueError("Paren binnen één familie moeten dezelfde lengte hebben.")
    gap = settings["merge_gap"]
    per_pair = {p.pair_id: diff_regions(p.original, p.tuned, gap) for p in pairs}
    united = merge_intervals(
        [r for regions in per_pair.values() for r in regions], gap
    )
    ctx = settings["context_bytes"]
    regions_out: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for raw_start, raw_end in united:
        start, end = _align(raw_start, raw_end, size)
        supporting = [
            p for p in pairs
            if any(s < end and start < e for s, e in per_pair[p.pair_id])
        ]
        support = len(supporting) / len(pairs)
        if support < settings["min_support"]:
            excluded.append({
                "start": start, "end": end, "support": round(support, 3),
                "reason": "Wijziging komt in te weinig trainingsparen voor.",
            })
            continue
        first = supporting[0].original
        classification = classify_region(first, start, end, settings)
        before = [p.original[max(0, start - ctx):start] for p in supporting]
        after = [p.original[end:end + ctx] for p in supporting]
        before_mask = "".join(
            "1" if len({b[i] for b in before}) == 1 else "0" for i in range(len(before[0]))
        )
        after_mask = "".join(
            "1" if len({a[i] for a in after}) == 1 else "0" for i in range(len(after[0]))
        )
        region: dict[str, Any] = {
            "start": start,
            "end": end,
            "support": round(support, 3),
            "supporting_pairs": sorted(p.pair_id for p in supporting),
            "kind": classification["kind"],
            "endian": classification["endian"],
            "roughness": classification["roughness"],
            "context_before": before[0].hex(),
            "context_before_mask": before_mask,
            "context_after": after[0].hex(),
            "context_after_mask": after_mask,
            "exact": sorted(
                {(p.original[start:end].hex(), p.tuned[start:end].hex()) for p in supporting}
            ),
        }
        map_ref = _map_for((start, end), supporting[0].maps)
        if map_ref is not None:
            region["map"] = {
                k: map_ref[k] for k in (
                    "description", "data_type", "columns", "rows", "data_offset",
                    "unit", "factor", "conversion_offset",
                ) if k in map_ref
            }
            region["map"]["axes"] = map_ref.get("axes", [])
        if region["kind"] == "calibration":
            cells = []
            for cell in range((end - start) // 2):
                offset = start + cell * 2
                origs = [_words(p.original[offset:offset + 2], region["endian"])[0] for p in supporting]
                tuneds = [_words(p.tuned[offset:offset + 2], region["endian"])[0] for p in supporting]
                if origs == tuneds:
                    cells.append(None)
                    continue
                cells.append(_cell_rule(origs, tuneds, settings))
            region["cells"] = cells
        regions_out.append(region)
    changed = sum(e - s for regions in per_pair.values() for s, e in regions)
    return {
        "regions": regions_out,
        "excluded_regions": excluded,
        "training_pairs": sorted(p.pair_id for p in pairs),
        "statistics": {
            "pairs": len(pairs),
            "united_regions": len(united),
            "included_regions": len(regions_out),
            "calibration_regions": sum(1 for r in regions_out if r["kind"] == "calibration"),
            "patch_regions": sum(1 for r in regions_out if r["kind"] == "patch"),
            "changed_span_bytes_total": changed,
            "regions_with_map_definition": sum(1 for r in regions_out if "map" in r),
        },
    }


def learn_modification_zones(pairs: list[TrainingPair], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Regions that other (non-target) versions change, used to spot mixed input."""

    zones = []
    for pair in pairs:
        for s, e in diff_regions(pair.original, pair.tuned, settings["merge_gap"]):
            zones.append({
                "start": s, "end": e, "label": pair.record.get("tuned_version_name", pair.profile),
                "tuned": pair.tuned[s:e].hex(), "original": pair.original[s:e].hex(),
            })
    zones.sort(key=lambda z: (z["start"], z["end"], z["label"]))
    return zones
