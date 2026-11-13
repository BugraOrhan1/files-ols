"""Transfer a known tune to a similar original ("95% match -> 100%").

When an original does not exactly match a software variant in the library,
the most similar training originals are found (same-offset sample
comparison, like WinOLS' match percentage; file-name hints such as vehicle
and ECU break ties). Every change of the donor's tune is then placed in the
new file:

1. same place, same original bytes            -> copy the donor's bytes
2. same original bytes found elsewhere        -> copy at the moved position
   (searched with the surrounding bytes as anchor)
3. same place/anchor, but different values    -> recalculate per cell with
                                                 the donor's ratio (16-bit)
4. not found                                   -> reported

Nothing is invented: every written byte is either a donor byte placed on
identical original bytes or a donor ratio applied to this file's value.
"""

from __future__ import annotations

import base64
import hashlib
import re
import struct
from array import array
from typing import Any

from .learn import _roughness, _words
from .package import ANCHOR_SIZE, FP_SAMPLES, fingerprint

DEFAULT_MIN_SIMILARITY = 0.70
CANDIDATE_FLOOR = 0.10      # below this same-offset score a donor is not even checked for shifts
MAX_SHIFT_CHECKS = 40
DEFAULT_MIN_COVERAGE = 1.0
MAX_DONORS_TRIED = 8
VALUE_TOLERANCE = 0.6
ALREADY_TUNED = b"__already_tuned__"  # a recalculated cell must lie within ±60 % of the donor's original


def _words32(fp: bytes) -> array:
    values = array("I")
    values.frombytes(fp[:FP_SAMPLES * 4])
    return values


def similarity(input_fp: array, donor_fp: array) -> float:
    match = informative = 0
    for a, b in zip(input_fp, donor_fp):
        if b in (0, 0xFFFFFFFF) or b == (b & 0xFF) * 0x01010101:
            continue  # padding / uniform bytes say nothing about similarity
        informative += 1
        if a == b:
            match += 1
    return match / informative if informative else 0.0


_HINT_SPLIT = re.compile(r"[^0-9A-Za-z]+")


def hint_tokens(text: str) -> set[str]:
    return {t.lower() for t in _HINT_SPLIT.split(text or "")
            if (len(t) >= 3 and not t.isdigit()) or (t.isdigit() and len(t) == 4)}


def shifted_similarity(data: bytes, anchor_blob: bytes) -> float:
    """Fraction of the donor's anchor snippets found anywhere in ``data``."""

    snippets = [anchor_blob[i:i + ANCHOR_SIZE] for i in range(0, len(anchor_blob), ANCHOR_SIZE)]
    if not snippets:
        return 0.0
    return sum(1 for s in snippets if s in data) / len(snippets)


def rank_donors(package, data: bytes, hint: str = "", profile: str | None = None,
                min_similarity: float = DEFAULT_MIN_SIMILARITY) -> list[dict[str, Any]]:
    """Training originals ordered by similarity to ``data`` (same size only).

    Score = max(same-offset sample match, share of anchor snippets found
    anywhere in the file); the second catches moved maps and inserted code."""

    input_fp = _words32(fingerprint(data))
    tokens = hint_tokens(hint)
    stage1 = []
    cache = getattr(package, "_fp_cache", None)
    if cache is None:
        cache = {}
        try:
            package._fp_cache = cache  # decoded once per loaded package
        except AttributeError:
            pass
    for entry in package.fingerprints():
        if entry["size"] != len(data):
            continue
        if profile and profile not in entry.get("profiles", []):
            continue
        key = (entry["variant_id"], entry["original_sha256"])
        donor_fp = cache.get(key)
        if donor_fp is None:
            donor_fp = cache[key] = _words32(base64.b64decode(entry["fp"]))
        score = similarity(input_fp, donor_fp)
        donor_tokens = hint_tokens(" ".join((entry.get("vehicle", ""), entry.get("ecu", ""), entry.get("project", ""))))
        stage1.append((score, len(tokens & donor_tokens), entry))
    stage1.sort(key=lambda item: (-(item[0] + 0.02 * min(item[1], 5)), item[2]["original_sha256"]))
    ranked = []
    checks = 0
    for score, overlap, entry in stage1:
        best = score
        if score < min_similarity and score >= CANDIDATE_FLOOR and checks < MAX_SHIFT_CHECKS and entry.get("anchors"):
            checks += 1
            best = max(score, shifted_similarity(data, base64.b64decode(entry["anchors"])))
        if best >= min_similarity:
            ranked.append(dict(entry, similarity=best, same_offset_similarity=score, hint_overlap=overlap))
    ranked.sort(key=lambda e: (-(e["similarity"] + 0.004 * min(e["hint_overlap"], 5)), e["original_sha256"]))
    return ranked


def transfer_profiles(package, data: bytes, hint: str = "",
                      min_similarity: float = DEFAULT_MIN_SIMILARITY) -> tuple[list[str], list[dict[str, Any]]]:
    donors = rank_donors(package, data, hint, None, min_similarity)[:MAX_DONORS_TRIED * 2]
    profiles: list[str] = []
    for donor in donors:
        for profile in donor.get("profiles", []):
            if profile not in profiles:
                profiles.append(profile)
    return profiles, donors


def _unique(data: bytes, pattern: bytes, near: int | None = None) -> int | None:
    if len(pattern) < 8:
        return None
    first = data.find(pattern)
    if first < 0:
        return None
    if data.find(pattern, first + 1) >= 0:
        if near is None:
            return None
        # several hits: accept the one closest to the expected place if it is clearly closest
        hits = []
        pos = first
        while pos >= 0 and len(hits) < 50:
            hits.append(pos)
            pos = data.find(pattern, pos + 1)
        hits.sort(key=lambda p: abs(p - near))
        if len(hits) > 1 and abs(hits[0] - near) * 4 >= abs(hits[1] - near):
            return None
        return hits[0]
    return first


def _endian(orig: bytes) -> str:
    rough = {e: _roughness(_words(orig, e)) for e in ("little", "big")}
    return min(rough, key=lambda e: (rough[e], e))


def _recalculate(current: bytes, orig: bytes, tuned: bytes) -> bytes | None:
    """Apply the donor's per-cell change to this file's own values."""

    if len(current) != len(orig):
        return None
    if len(orig) % 2:
        cells = [(c, o, t) for c, o, t in zip(current, orig, tuned)]
        out = bytearray()
        for c, o, t in cells:
            if o == t:
                out.append(c)
                continue
            if abs(c - o) > max(VALUE_TOLERANCE * o, 8):
                return None
            out.append(max(0, min(255, round(c * t / o) if o else c + (t - o))))
        return bytes(out)
    endian = _endian(orig)
    fmt = "<" if endian == "little" else ">"
    count = len(orig) // 2
    cur_v = struct.unpack(f"{fmt}{count}H", current)
    org_v = struct.unpack(f"{fmt}{count}H", orig)
    tun_v = struct.unpack(f"{fmt}{count}H", tuned)
    # Already tuned? Cells that the tune changes clearly lie closer to the
    # donor's tuned values than to its originals -> never tune twice.
    decisive = tuned_like = 0
    for c, o, t in zip(cur_v, org_v, tun_v):
        if o != t and abs(t - o) >= 0.05 * max(o, 1):
            decisive += 1
            if abs(c - t) * 2 < abs(c - o):  # clearly closer to the tuned value
                tuned_like += 1
    if decisive >= 3 and tuned_like / decisive > 0.5:
        return ALREADY_TUNED
    out = []
    for c, o, t in zip(cur_v, org_v, tun_v):
        if o == t:
            out.append(c)
            continue
        if abs(c - o) > max(VALUE_TOLERANCE * o, 16):
            return None
        new = round(c * t / o) if o else c + (t - o)
        out.append(max(0, min(0xFFFF, new)))
    return struct.pack(f"{fmt}{count}H", *out)


def _context_ok(data: bytes, offset: int, expected: bytes) -> bool:
    if not expected or offset < 0:
        return False
    actual = data[offset:offset + len(expected)]
    if len(actual) != len(expected):
        return False
    return sum(1 for a, b in zip(actual, expected) if a == b) >= 0.75 * len(expected)


def _is_table(window: bytes) -> bool:
    """True when the bytes around an edit look like a smooth 16-bit table."""

    if len(window) < 12:
        return False
    return min(_roughness(_words(window, e)) for e in ("little", "big")) <= 0.25


def place_edits(data: bytes, edits: list[list[Any]], exact_only: bool = False,
                ) -> tuple[bytearray, list[dict[str, Any]], list[dict[str, Any]]]:
    """Place every donor edit in ``data``. Returns output, placed, missing."""

    output = bytearray(data)
    placed: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    present: list[dict[str, Any]] = []
    shift = 0
    for edit in edits:
        start, orig_hex, tuned_hex = edit[0], edit[1], edit[2]
        before = bytes.fromhex(edit[3]) if len(edit) > 3 else b""
        after = bytes.fromhex(edit[4]) if len(edit) > 4 else b""
        orig, tuned = bytes.fromhex(orig_hex), bytes.fromhex(tuned_hex)
        # Align multi-byte edits to whole 16-bit cells (the last cell's high
        # byte often stays unchanged, which makes the edit odd-sized).
        if len(orig) >= 3 and len(orig) % 2:
            if start % 2 and before:
                start, orig, tuned, before = start - 1, before[-1:] + orig, before[-1:] + tuned, before[:-1]
            elif after:
                orig, tuned, after = orig + after[:1], tuned + after[:1], after[1:]
        size = len(orig)
        result = None
        # 1. same shift as the previous edit (or same place) with identical bytes;
        #    short edits also need matching surroundings, a single equal byte proves nothing
        for offset in dict.fromkeys((start + shift, start)):
            if 0 <= offset and data[offset:offset + size] == orig:
                if size < 8 and not (_context_ok(data, offset - len(before), before)
                                     or _context_ok(data, offset + size, after)):
                    continue
                result = ("zelfde plek" if offset == start else "verschoven", offset, tuned)
                break
        # 2. identical bytes found elsewhere, anchored by context
        if result is None:
            patterns = ((before + orig + after, len(before)),) if exact_only else (
                (before + orig + after, len(before)), (before + orig, len(before)), (orig + after, 0), (orig, 0))
            for pattern, lead in patterns:
                found = _unique(data, pattern, start + shift)
                if found is not None:
                    result = ("verschoven", found + lead, tuned)
                    break
        # 3. same layout, other values: recalculate
        if result is None and not exact_only:
            anchors = []
            for offset in dict.fromkeys((start + shift, start)):
                if (offset >= len(before) and data[offset - len(before):offset] == before
                        and data[offset + size:offset + size + len(after)] == after):
                    anchors.append(offset)
            if not anchors and len(before) >= 16:
                found = _unique(data, before, start + shift - len(before))
                if found is not None:
                    anchors.append(found + len(before))
            if not anchors and len(after) >= 16:
                found = _unique(data, after, start + shift + size)
                if found is not None:
                    anchors.append(found - size)
            table = size >= 4 or _is_table(before + orig + after)
            for offset in anchors:
                if data[offset:offset + size] == tuned and tuned != orig:
                    present.append({"start": offset, "length": size})
                    result = ("aanwezig", offset, tuned)
                    break
            for offset in (anchors if table and result is None else ()):  # switches/code are never scaled
                if 0 <= offset and offset + size <= len(data):
                    new = _recalculate(data[offset:offset + size], orig, tuned)
                    if new is ALREADY_TUNED:
                        present.append({"start": offset, "length": size})
                        result = ("aanwezig", offset, tuned)
                        break
                    if new is not None:
                        result = ("omgerekend", offset, new)
                        break
        if result is None:
            missing.append({"start": start, "length": size, "orig": orig, "tuned": tuned,
                            "before": before, "after": after})
            continue
        how, offset, new = result
        shift = offset - start
        if how == "aanwezig":
            continue  # already tuned here: reported, never tuned twice
        output[offset:offset + size] = new
        placed.append({"start": offset, "end": offset + size, "donor_start": start, "how": how,
                       "changed_bytes": sum(1 for a, b in zip(new, data[offset:offset + size]) if a != b)})
    # Second pass: a map whose surroundings differ (other calibration around it)
    # sits at the same shift as its placed neighbours; recalculate it there,
    # guarded by the per-cell plausibility check.
    if missing and placed and not exact_only:
        anchors_by_start = sorted((p["donor_start"], p["start"] - p["donor_start"]) for p in placed)
        still = []
        taken = [(p["start"], p["end"]) for p in placed]
        for item in missing:
            start, orig, tuned, size = item["start"], item["orig"], item["tuned"], item["length"]
            before = [d for s, d in anchors_by_start if s < start][-1:]
            after = [d for s, d in anchors_by_start if s > start][:1]
            shifts = list(dict.fromkeys(before + after))
            result = None
            for delta in shifts:
                offset = start + delta
                if offset < 0 or offset + size > len(data):
                    continue
                if any(offset < e and s < offset + size for s, e in taken):
                    continue
                if data[offset:offset + size] == tuned and tuned != orig:
                    present.append({"start": offset, "length": size})
                    result = ("present", None)
                    break
                table = size >= 4 or _is_table(item["before"] + orig + item["after"])
                new = _recalculate(data[offset:offset + size], orig, tuned) if table and size >= 2 else None
                if new is ALREADY_TUNED:
                    present.append({"start": offset, "length": size})
                    result = ("present", None)
                    break
                if new is not None:
                    result = (offset, new)
                    break
            if result is None:
                still.append({"start": start, "length": size})
                continue
            if result[0] == "present":
                continue
            offset, new = result
            output[offset:offset + size] = new
            taken.append((offset, offset + size))
            placed.append({"start": offset, "end": offset + size, "donor_start": start, "how": "omgerekend",
                           "changed_bytes": sum(1 for a, b in zip(new, data[offset:offset + size]) if a != b)})
        missing = still
    else:
        missing = [{"start": m["start"], "length": m["length"]} for m in missing]
    placed.sort(key=lambda p: p["start"])
    if present:
        missing = missing + [dict(p, present=True) for p in present]
    return output, placed, missing


def tuned_presence(data: bytes, edits: list[list[Any]]) -> tuple[float, int]:
    """(fraction of donor edits already present, number of present edits of
    at least 8 bytes). Found with the same anchoring as placement."""

    if not edits:
        return 0.0, 0
    swapped = [[e[0], e[2], e[1], *e[3:]] for e in edits if e[1] != e[2]]
    _out, placed, _missing = place_edits(data, swapped, exact_only=True)
    large = sum(1 for p in placed if p["end"] - p["start"] >= 8)
    return len(placed) / len(edits), large


def already_tuned_by(data: bytes, edits: list[list[Any]]) -> float:
    return tuned_presence(data, edits)[0]


def choose_known_pair(blob: dict[str, Any], profile: str, original_sha: str) -> dict[str, Any] | None:
    pairs = [p for p in blob["known_pairs"].get(profile, []) if p.get("original_sha256") == original_sha]
    if not pairs:
        return None
    counts: dict[str, int] = {}
    for p in pairs:
        counts[p["tuned_sha256"]] = counts.get(p["tuned_sha256"], 0) + 1
    pairs.sort(key=lambda p: (-counts[p["tuned_sha256"]], p["pair_id"]))
    return pairs[0]


def transfer(data: bytes, profile: str, package, *, hint: str = "",
             min_similarity: float = DEFAULT_MIN_SIMILARITY,
             min_coverage: float = DEFAULT_MIN_COVERAGE) -> dict[str, Any]:
    """Best transfer over the most similar donors. Returns a dict with
    ``status`` 'ok' (output, placed, donor, coverage) or 'failed' (reason)."""

    donors = rank_donors(package, data, hint, profile, min_similarity)
    if not donors:
        return {"status": "failed", "code": "no_similar_original",
                "reason": f"Geen vergelijkbaar origineel met '{profile}' in de bibliotheek "
                          f"(minimaal {min_similarity:.0%} gelijk, zelfde bestandsgrootte)."}
    best = None
    for rank, donor in enumerate(donors[:MAX_DONORS_TRIED]):
        blob = package.load_variant(donor["variant_id"])
        pair = choose_known_pair(blob, profile, donor["original_sha256"])
        if pair is None or not pair["edits"]:
            continue
        fraction, _large = tuned_presence(data, pair["edits"])
        decides = rank == 0 or best is None
        if decides and fraction >= 0.5:
            return {"status": "failed", "code": "already_tuned",
                    "reason": f"Dit bestand bevat al de wijzigingen van {pair.get('tuned_version_name') or profile} "
                              f"(zoals in project {pair.get('project_name', '')}). Open het originele bestand."}
        output, placed, missing = place_edits(data, pair["edits"])
        present = [m for m in missing if m.get("present")]
        if not decides:
            missing = [m for m in missing if not m.get("present")] + [
                {"start": m["start"], "length": m["length"]} for m in present]
            present = []
        if (len(present) + fraction * len(pair["edits"])) / len(pair["edits"]) >= 0.5:
            return {"status": "failed", "code": "already_tuned",
                    "reason": f"Dit bestand is al getuned: de wijzigingen van "
                              f"{pair.get('tuned_version_name') or profile} zitten er al in. Open het originele bestand."}
        present_large = [m for m in present if m["length"] >= 8]
        if present_large:
            return {"status": "failed", "code": "partially_tuned",
                    "reason": f"{len(present_large)} kaart(en) in dit bestand zijn al aangepast zoals bij "
                              f"{pair.get('tuned_version_name') or profile}; de rest niet. Een half bewerkt "
                              "bestand wordt niet nog eens getuned (dat zou dubbel verhogen). Open het origineel."}
        missing = [m for m in missing if not m.get("present")]
        total_bytes = sum(len(e[1]) // 2 for e in pair["edits"])
        missing_bytes = sum(m["length"] for m in missing)
        coverage = 1 - missing_bytes / total_bytes if total_bytes else 0.0
        candidate = {"donor": donor, "pair": pair, "output": bytes(output), "placed": placed,
                     "missing": missing, "coverage": coverage}
        if best is None or (coverage, donor["similarity"]) > (best["coverage"], best["donor"]["similarity"]):
            best = candidate
        if coverage >= 1.0:
            break
    if best is None:
        return {"status": "failed", "code": "no_similar_original",
                "reason": f"Vergelijkbare originelen gevonden, maar zonder '{profile}'-voorbeeld."}
    if best["coverage"] < min_coverage:
        donor = best["donor"]
        return {"status": "failed", "code": "transfer_incomplete",
                "reason": f"Beste voorbeeld: {donor.get('vehicle') or donor.get('project')} "
                          f"({donor['similarity']:.1%} gelijk). {len(best['missing'])} van "
                          f"{len(best['pair']['edits'])} wijzigingen konden niet veilig worden geplaatst "
                          f"({best['coverage']:.1%} overgezet; ingesteld minimum {min_coverage:.0%}).",
                "best": {k: best[k] for k in ("coverage",)} | {"missing": best["missing"][:20]}}
    best["status"] = "ok"
    best["output_sha256"] = hashlib.sha256(best["output"]).hexdigest()
    return best
