"""The same original, byte for byte, in the library.

Identical content is the strongest evidence there is, also when the project
has no ECU type or numbers filled in. It comes before every recognition by
number or similarity, and all versions of that project can be chosen.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .consensus import eligible_profile


def _index(package: Any) -> dict[str, list[dict[str, Any]]]:
    index = getattr(package, "_by_original", None)
    if index is None:
        index = {}
        for entry in package.fingerprints():
            index.setdefault(entry["original_sha256"], []).append(entry)
        try:
            package._by_original = index
        except AttributeError:
            pass
    return index


def known_versions(data: bytes, package: Any) -> list[dict[str, Any]]:
    """Every performance version of this exact original, one per different
    tune: profile, pair_id, name, project, variant_id, edits, times."""

    sha = hashlib.sha256(data).hexdigest()
    versions: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in _index(package).get(sha, []):
        blob = package.load_variant(entry["variant_id"])
        for profile, pairs in blob["known_pairs"].items():
            if not eligible_profile(profile):
                continue
            for pair in pairs:
                if pair.get("original_sha256") != sha or not pair["edits"]:
                    continue
                if any(data[e[0]:e[0] + len(e[1]) // 2].hex() != e[1] for e in pair["edits"]):
                    continue  # cannot happen for an intact library
                item = versions.setdefault((profile, pair["tuned_sha256"]), {
                    "profile": profile, "pair_id": pair["pair_id"], "name": pair.get("tuned_version_name") or profile,
                    "project": pair.get("project_name") or pair["pair_id"], "variant_id": entry["variant_id"],
                    "pair": pair, "times": 0})
                item["times"] += 1
    return sorted(versions.values(), key=lambda v: (v["profile"], -v["times"], v["pair_id"]))
