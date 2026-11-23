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
from .maps import load_records, locate, store_root_of
from .package import ANCHOR_SIZE, FP_SAMPLES, fingerprint

DEFAULT_MIN_SIMILARITY = 0.70
CANDIDATE_FLOOR = 0.10      # below this same-offset score a donor is not even checked for shifts
MAX_SHIFT_CHECKS = 24
CROSS_SIZE_CHECKS = 200     # donors of another file size (full read vs calibration-only) checked by anchors
CROSS_SIZE_RATIO = 4.0      # ... only when the sizes differ at most this factor
CROSS_SIZE_ALIGN = 0x10000  # ... and both are whole 64 KB sectors (a cut-off read is never "another form")
CROSS_SIZE_HEADER = 0x400   # ... apart from a small tool header/trailer (e.g. 0x840010 = 8.25 MB + 16 bytes)
_RANK_CACHE: dict[tuple, list[dict[str, Any]]] = {}
DEFAULT_MIN_COVERAGE = 1.0
MAX_DONORS_TRIED = 8
# Donors at least this similar count as the same software for 'already partly tuned' proof.
SAME_SOFTWARE_SIMILARITY = 0.999
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


def sizes_compatible(a: int, b: int) -> bool:
    """Two reads of the same ECU in another form: at most CROSS_SIZE_RATIO
    apart and both whole 64 KB sectors (plus at most a small header). A
    cut-off read (e.g. 1 KB short) is never another read form."""

    if a == b:
        return True
    if not a or not b or not 1 / CROSS_SIZE_RATIO <= a / b <= CROSS_SIZE_RATIO:
        return False
    return all(n % CROSS_SIZE_ALIGN <= CROSS_SIZE_HEADER for n in (a, b))


def shifted_similarity(data: bytes, anchor_blob: bytes, early_stop: bool = True) -> float:
    """Fraction of the donor's anchor snippets found anywhere in ``data``.
    Stops early when the first quarter already shows the donor is unrelated."""

    snippets = [anchor_blob[i:i + ANCHOR_SIZE] for i in range(0, len(anchor_blob), ANCHOR_SIZE)]
    if not snippets:
        return 0.0
    found = 0
    quarter = max(1, len(snippets) // 4)
    for index, snippet in enumerate(snippets, 1):
        if snippet in data:
            found += 1
        if early_stop and index == quarter and found / quarter < 0.3:
            return found / len(snippets)
    return found / len(snippets)


def rank_donors(package, data: bytes, hint: str = "", profile: str | None = None,
                min_similarity: float = DEFAULT_MIN_SIMILARITY, exclude: set[str] | None = None,
                family_floor: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Cached front for :func:`_rank_donors` (the app asks twice per file)."""

    key = (package.package_id, hashlib.sha256(data).digest(), hint, profile, min_similarity,
           frozenset(exclude or ()), tuple(sorted((family_floor or {}).items())))
    cached = _RANK_CACHE.get(key)
    if cached is None:
        cached = _rank_donors(package, data, hint, profile, min_similarity, exclude, family_floor)
        if len(_RANK_CACHE) > 16:
            _RANK_CACHE.clear()
        _RANK_CACHE[key] = cached
    return [dict(d) for d in cached]


def _rank_donors(package, data: bytes, hint: str = "", profile: str | None = None,
                 min_similarity: float = DEFAULT_MIN_SIMILARITY, exclude: set[str] | None = None,
                 family_floor: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Training originals ordered by similarity to ``data``.

    Donors of the same size are compared at the same offsets first; donors of
    another size (other read form of the same ECU) by their anchor snippets.

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
    exclude = exclude or set()
    cross_size = []
    for entry in package.fingerprints():
        if entry["original_sha256"] in exclude:
            continue
        if profile and profile not in entry.get("profiles", []):
            continue
        if entry["size"] != len(data):
            # Same ECU read in another form (e.g. full flash vs calibration
            # only, or another dump size). Same-offset sampling says nothing
            # here; the donor's anchor snippets are searched anywhere instead.
            if entry.get("anchors") and sizes_compatible(entry["size"], len(data)):
                donor_tokens = hint_tokens(" ".join((entry.get("vehicle", ""), entry.get("ecu", ""),
                                                     entry.get("project", ""))))
                cross_size.append((len(tokens & donor_tokens), entry))
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
        floor = max(min_similarity, (family_floor or {}).get(entry.get("family_key", ""), 0.0))
        if best >= floor:
            ranked.append(dict(entry, similarity=best, same_offset_similarity=score, hint_overlap=overlap))
    cross_size.sort(key=lambda item: (-item[0], abs(item[1]["size"] - len(data)), item[1]["original_sha256"]))
    for overlap, entry in cross_size[:CROSS_SIZE_CHECKS]:
        larger = entry["size"] > len(data)
        anchors_blob = base64.b64decode(entry["anchors"])
        best = shifted_similarity(data, anchors_blob, early_stop=not larger)
        floor = max(min_similarity, (family_floor or {}).get(entry.get("family_key", ""), 0.0))
        if best >= floor:
            ranked.append(dict(entry, similarity=best, same_offset_similarity=0.0, hint_overlap=overlap,
                               cross_size=True))
        elif larger:
            # A larger read (e.g. 8 MB full flash vs a 6 MB read) can at most
            # hold len(data)/size of its anchors in this file. Measured on the
            # part that can be present, it may still be the same software;
            # such a donor is only used when every change lands on identical
            # original bytes (see transfer()).
            contained = min(1.0, best / (len(data) / entry["size"]))
            if contained >= floor:
                ranked.append(dict(entry, similarity=contained, same_offset_similarity=0.0,
                                   hint_overlap=overlap, cross_size=True, exact_required=True,
                                   raw_similarity=best))
    # Same software, other read form or other hardware number: the
    # reference's software number(s) occur literally in this file (and for
    # "full" also every hardware token). Whole-image similarity says little
    # here (a 4 MB OBD read vs a 5.5 MB bench read share few same-offset
    # bytes; 80A907559B and 8W0907559G carry the same software 031471), so
    # such a donor is tried regardless of its score; transfer() then only
    # accepts it when every change lands on identical original bytes.
    confirmed = _identity_confirmed_variants(package, data)
    if confirmed:
        listed = {e["original_sha256"]: e for e in ranked}
        for entry in package.fingerprints():
            if entry["variant_id"] not in confirmed or entry["original_sha256"] in exclude:
                continue
            if profile and profile not in entry.get("profiles", []):
                continue
            level = confirmed[entry["variant_id"]]
            if entry["original_sha256"] in listed:
                listed[entry["original_sha256"]]["identity_confirmed"] = level
                continue
            if not sizes_compatible(entry["size"], len(data)):
                continue  # a cut-off read is never "another read form"
            score = 0.0
            if entry.get("anchors"):
                score = shifted_similarity(data, base64.b64decode(entry["anchors"]))
            item = dict(entry, similarity=score, same_offset_similarity=0.0, hint_overlap=0,
                        identity_confirmed=level, exact_required=True, cross_size=entry["size"] != len(data))
            ranked.append(item)
            listed[entry["original_sha256"]] = item
    ranked.sort(key=lambda e: (_IDENTITY_ORDER.get(e.get("identity_confirmed"), 2),
                               -(e["similarity"] + 0.004 * min(e["hint_overlap"], 5)), e["original_sha256"]))
    return ranked


IDENTITY_SW_FIELDS = ("sw_number", "sw_version")
_IDENTITY_ORDER = {"full": 0, "software": 1}


def _identity_confirmed_variants(package, data: bytes) -> dict[str, str]:
    """Variants whose software is present in ``data`` (found as whole numbers,
    see :func:`token_in`):

    ``full``      every identification token (>= 2, one a software number)
    ``software``  every software-number token, but another hardware number
                  (same software on other hardware, e.g. 80A907559B vs
                  8W0907559G both with software 031471)
    """

    from .identity import _useful, token_in

    out: dict[str, str] = {}
    cache: dict[str, bool] = {}

    def present(token: str) -> bool:
        if token not in cache:
            cache[token] = token_in(token, data)
        return cache[token]

    for _family, variant in package.variants():
        identity = {k: [t for t in v if _useful(t)] for k, v in (variant.get("identity") or {}).items()}
        sw_tokens = {t for f in IDENTITY_SW_FIELDS for t in identity.get(f, [])}
        if not sw_tokens or not all(present(t) for t in sw_tokens):
            continue
        tokens = {t for v in identity.values() for t in v}
        out[variant["variant_id"]] = "full" if len(tokens) >= 2 and all(present(t) for t in tokens) else "software"
    return out


def transfer_profiles(package, data: bytes, hint: str = "", min_similarity: float = DEFAULT_MIN_SIMILARITY,
                      family_floor: dict[str, float] | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    donors = rank_donors(package, data, hint, None, min_similarity, None, family_floor)[:MAX_DONORS_TRIED * 2]
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


def _recalculate(current: bytes, orig: bytes, tuned: bytes, width: int = 0) -> bytes | None:
    """Apply the donor's per-cell change to this file's own values.
    ``width`` 1 forces byte cells (known from a map definition)."""

    if len(current) != len(orig):
        return None
    if width == 1 or len(orig) % 2:
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


def place_by_maps(data: bytes, output: bytearray, placed: list[dict[str, Any]], missing: list[dict[str, Any]],
                  records: list[dict[str, Any]]) -> tuple[bytearray, list[dict[str, Any]], list[dict[str, Any]]]:
    """Place donor changes that byte anchoring could not place, by finding
    the changed map through its axes (see ``maps``). Only maps covering a
    missing change are tried; present (already tuned) findings are kept."""

    open_items = [m for m in missing if not m.get("present")]
    kept = [m for m in missing if m.get("present")]
    if not open_items:
        return output, placed, missing
    shifts = sorted((p["donor_start"], p["start"] - p["donor_start"]) for p in placed)
    taken = [(p["start"], p["end"]) for p in placed]
    placed = list(placed)
    for record in records:
        size = len(record["orig"]) // 2
        r0, r1 = record["start"], record["start"] + size
        covered = [m for m in open_items if r0 <= m["start"] and m["start"] + m["length"] <= r1]
        if not covered:
            continue
        before = [d for s, d in shifts if s <= r0][-1:]
        near = r0 + (before[0] if before else 0)
        offset = locate(data, record, near)
        if offset is None or any(offset < e and s < offset + size for s, e in taken):
            continue
        current = data[offset:offset + size]
        orig, tuned = bytes.fromhex(record["orig"]), bytes.fromhex(record["tuned"])
        if current == tuned:
            kept.extend(dict(m, present=True) for m in covered)
            open_items = [m for m in open_items if m not in covered]
            continue
        new = _recalculate(current, orig, tuned, record["width"])
        if new is ALREADY_TUNED:
            kept.extend(dict(m, present=True) for m in covered)
            open_items = [m for m in open_items if m not in covered]
            continue
        if new is None:
            continue
        output[offset:offset + size] = new
        taken.append((offset, offset + size))
        placed.append({"start": offset, "end": offset + size, "donor_start": r0, "how": "kaart via assen",
                       "map": record.get("name", ""),
                       "changed_bytes": sum(1 for a, b in zip(new, current) if a != b)})
        open_items = [m for m in open_items if m not in covered]
    placed.sort(key=lambda p: p["start"])
    return output, placed, open_items + kept


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


def value_distance(data: bytes, edits: list[list[Any]]) -> float:
    """Mean relative difference between this file's values and the donor's
    original values, measured where each donor change is actually found
    (same place, moved, or anchored). Unplaceable changes count as 100 %."""

    _output, placed, missing = place_edits(data, edits)
    by_start = {e[0]: e for e in edits}
    total = count = 0.0
    for item in placed:
        cells = max(1, (item["end"] - item["start"]) // 2)
        if item["how"] != "omgerekend":
            count += cells  # identical original bytes: distance 0
            continue
        edit = by_start.get(item["donor_start"])
        if edit is None:
            continue
        orig = bytes.fromhex(edit[1])
        size = min(len(orig), item["end"] - item["start"]) // 2 * 2
        if size < 2:
            continue
        current = data[item["start"]:item["start"] + size]
        fmt = "<" if _endian(orig[:size]) == "little" else ">"
        for a, b in zip(struct.unpack(f"{fmt}{size // 2}H", current), struct.unpack(f"{fmt}{size // 2}H", orig[:size])):
            total += abs(a - b) / max(b, 1)
            count += 1
    for item in missing:
        cells = max(1, item["length"] // 2)
        total += cells
        count += cells
    return total / count if count else 1.0


def _prefer_closest_values(data: bytes, profile: str, package, donors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Among donors as similar as the best one (within 0.5 %), the donor whose
    map values are closest to this file comes first: it is the best example
    to transfer from and the fairest judge of 'already tuned'."""

    if len(donors) < 2:
        return donors
    top = donors[0]["similarity"]
    close = [d for d in donors[:MAX_DONORS_TRIED] if d["similarity"] >= top - 0.005]
    if len(close) < 2:
        return donors
    scored = []
    for donor in close:
        pair = choose_known_pair(package.load_variant(donor["variant_id"]), profile, donor["original_sha256"])
        distance = value_distance(data, pair["edits"]) if pair else 1.0
        scored.append((distance, -donor["similarity"], donor["original_sha256"], donor))
    scored.sort(key=lambda item: item[:3])
    chosen = [item[3] for item in scored]
    ids = {d["original_sha256"] for d in chosen}
    return chosen + [d for d in donors if d["original_sha256"] not in ids]


def choose_known_pair(blob: dict[str, Any], profile: str, original_sha: str) -> dict[str, Any] | None:
    pairs = [p for p in blob["known_pairs"].get(profile, []) if p.get("original_sha256") == original_sha]
    if not pairs:
        return None
    counts: dict[str, int] = {}
    for p in pairs:
        counts[p["tuned_sha256"]] = counts.get(p["tuned_sha256"], 0) + 1
    pairs.sort(key=lambda p: (-counts[p["tuned_sha256"]], p["pair_id"]))
    return pairs[0]


def transfer(data: bytes, profile: str, package, *, hint: str = "", exclude: set[str] | None = None,
             family_floor: dict[str, float] | None = None,
             min_similarity: float = DEFAULT_MIN_SIMILARITY,
             min_coverage: float = DEFAULT_MIN_COVERAGE) -> dict[str, Any]:
    """Best transfer over the most similar donors. Returns a dict with
    ``status`` 'ok' (output, placed, donor, coverage) or 'failed' (reason)."""

    donors = rank_donors(package, data, hint, profile, min_similarity, exclude, family_floor)
    if not donors:
        return {"status": "failed", "code": "no_similar_original",
                "reason": f"Geen vergelijkbaar origineel met '{profile}' in de bibliotheek "
                          f"(minimaal {min_similarity:.0%} gelijk)."}
    donors = _prefer_closest_values(data, profile, package, donors)
    maps_root = store_root_of(package)
    best = None
    suspect = None  # partial-presence finding from a donor with other software
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
        if missing and not donor.get("exact_required"):
            records = load_records(maps_root, donor["original_sha256"], pair.get("tuned_sha256", ""))
            if records:
                output, placed, missing = place_by_maps(data, output, placed, missing, records)
        if donor.get("exact_required"):
            # Accepted on identity or partial-read similarity alone: only
            # byte-identical placement of every change counts (no
            # recalculated maps, nothing missing).
            if missing or any(p["how"] == "omgerekend" for p in placed):
                continue
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
            finding = {"status": "failed", "code": "partially_tuned",
                       "reason": f"{len(present_large)} kaart(en) in dit bestand zijn al aangepast zoals bij "
                                 f"{pair.get('tuned_version_name') or profile}; de rest niet. Een half bewerkt "
                                 "bestand wordt niet nog eens getuned (dat zou dubbel verhogen). Open het origineel."}
            if donor["similarity"] >= SAME_SOFTWARE_SIMILARITY and not donor.get("cross_size"):
                return finding
            # Other software or another read form (anchor similarity only):
            # its tuned values may simply be this file's stock values. Skip
            # this donor; reported as no_reliable_donor if no other donor works.
            suspect = suspect or {
                "status": "failed", "code": "no_reliable_donor",
                "reason": f"Geen betrouwbaar voorbeeld: het beste voorbeeld ({pair.get('tuned_version_name') or profile}, "
                          f"project {pair.get('project_name', '')}) is andere software of een andere uitleesvorm en "
                          f"heeft op {len(present_large)} kaart(en) al de waarden die dit bestand ook heeft. Of dit "
                          "bestand is deels getuned, of die software heeft andere standaardwaarden; daarom is "
                          "niets overgezet. Meer projecten van deze software in de bibliotheek lossen dit op."}
            continue
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
        if suspect is not None:
            return suspect
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
