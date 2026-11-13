"""Compare a generated image with a held-out reference and simple baselines."""

from __future__ import annotations

import hashlib
import struct
from typing import Any

from .generate import GenerationRejected, generate
from .learn import diff_regions


def _changed_set(a: bytes, b: bytes) -> set[int]:
    return {i for s, e in diff_regions(a, b, 0) for i in range(s, e)}


def compare_to_reference(original: bytes, candidate: bytes, reference: bytes,
                         allowed: list[tuple[int, int]] | None = None) -> dict[str, Any]:
    ref_changed = _changed_set(original, reference)
    cand_changed = _changed_set(original, candidate)
    exact = sum(1 for i in ref_changed if candidate[i] == reference[i])
    missed = sorted(ref_changed - cand_changed)
    unexpected = sorted(cand_changed - ref_changed)
    outside = 0
    if allowed is not None:
        outside = sum(1 for i in cand_changed if not any(s <= i < e for s, e in allowed))
    # Word-level deviation (raw units, little and big endian reported).
    words = sorted({i - i % 2 for i in ref_changed | cand_changed})
    deviation = {}
    for endian, fmt in (("little", "<H"), ("big", ">H")):
        diffs = [
            abs(struct.unpack_from(fmt, candidate, w)[0] - struct.unpack_from(fmt, reference, w)[0])
            for w in words if w + 2 <= len(reference)
        ]
        deviation[endian] = {
            "mean_abs": round(sum(diffs) / len(diffs), 3) if diffs else 0.0,
            "max_abs": max(diffs) if diffs else 0,
        }
    return {
        "reference_changed_bytes": len(ref_changed),
        "candidate_changed_bytes": len(cand_changed),
        "reference_bytes_reproduced": exact,
        "reproduction_ratio": round(exact / len(ref_changed), 6) if ref_changed else 1.0,
        "missed_changes": len(missed),
        "unexpected_changes": len(unexpected),
        "changes_outside_allowed": outside,
        "word_deviation_raw": deviation,
        "byte_identical": candidate == reference,
        "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
        "reference_sha256": hashlib.sha256(reference).hexdigest(),
    }


def donor_diff_baseline(original: bytes, donor_original: bytes, donor_tuned: bytes) -> bytes:
    """Simple baseline: copy the donor's changed bytes to the same offsets."""

    out = bytearray(original)
    if len(donor_tuned) != len(original):
        return bytes(out)
    for s, e in diff_regions(donor_original, donor_tuned, 0):
        out[s:e] = donor_tuned[s:e]
    return bytes(out)


def evaluate_holdout(package: dict[str, Any], original: bytes, reference: bytes,
                     donor: tuple[bytes, bytes] | None, profile: str = "stage1") -> dict[str, Any]:
    result: dict[str, Any] = {"input_sha256": hashlib.sha256(original).hexdigest()}
    try:
        generated = generate(original, profile, package)
        result["generation"] = {"status": "GENERATED", "output_sha256": generated.output_sha256,
                                "regions": len(generated.changes)}
        result["learned_method"] = compare_to_reference(
            original, generated.output, reference, generated.allowed_ranges)
    except GenerationRejected as exc:
        result["generation"] = {"status": "REJECTED", "code": exc.code, "message": exc.message}
    result["baseline_unchanged_original"] = compare_to_reference(original, original, reference)
    if donor is not None:
        result["baseline_donor_diff"] = compare_to_reference(
            original, donor_diff_baseline(original, *donor), reference)
    return result
