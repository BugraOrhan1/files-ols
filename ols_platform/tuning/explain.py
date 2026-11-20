"""Explainable matching: why a reference candidate fits, what is missing,
and which check would settle it.

Four values are kept strictly apart per candidate:

``similarity_score``
    measured share of comparable content (same-offset samples, or anchor
    snippets for another file size). A measurement, not a probability.
``identity_status``
    ``IDENTICAL_CONTENT`` | ``IDENTITY_CONFIRMED`` | ``SAME_FAMILY_OTHER_REVISION`` |
    ``SAME_FAMILY_OTHER_IDS`` | ``INSUFFICIENT_EVIDENCE`` | ``CONFLICT``
``compatibility_status``
    ``EXACT_VARIANT_KNOWN_PAIR`` | ``TRANSFER_CHECK_IDENTICAL_BYTES`` |
    ``TRANSFER_CHECK_WITH_RECALCULATION`` | ``TRANSFER_CHECK_INCOMPLETE`` |
    ``ALREADY_TUNED`` | ``NOT_CHECKED`` | ``BLOCKED``
``reference_status``
    ``INDEPENDENT_EXPORT_MATCH`` | ``PROJECT_PAIR`` | ``VALIDATED_RELATION`` |
    ``USER_REVIEWED_NOT_INDEPENDENT`` | ``REJECTED_BY_REVIEW`` | ``NO_REFERENCE``

A status only rises because of a new, reproducible check (all identity
tokens literally present, identical hashes, donor edits landing on identical
original bytes, an independent export hash, a validated relation). A high
similarity never lifts a status and never outweighs a conflict.

Matching is read-only: nothing in the store, the library or the input file
is written.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

from .ecu_fingerprint import (
    ALGORITHM_VERSION, Identifier, block_digests, compare_blocks, compare_ids, extract_identifiers,
    identifiers_by_field, normalize_id, vag_base,
)
from .identity import _useful, classify_input

ENGINE_VERSION = "explain/1"
CANDIDATE_MIN_SIMILARITY = 0.30   # show weak candidates too, they are labelled as such
IDENTITY_RANK = {"IDENTICAL_CONTENT": 0, "IDENTITY_CONFIRMED": 1, "SAME_FAMILY_OTHER_REVISION": 2,
                 "SAME_FAMILY_OTHER_IDS": 3, "INSUFFICIENT_EVIDENCE": 4, "CONFLICT": 5}
CATEGORY = {"IDENTICAL_CONTENT": "exact", "IDENTITY_CONFIRMED": "exact",
            "SAME_FAMILY_OTHER_REVISION": "alternative_revision", "SAME_FAMILY_OTHER_IDS": "insufficient",
            "INSUFFICIENT_EVIDENCE": "insufficient", "CONFLICT": "conflict"}
# Which extraction rules describe the same kind of number as a library token.
_COMPARABLE_RULES = {"sw_number": ("bosch_sw_1037", "vag_part_number"),
                     "sw_version": ("bosch_sw_1037", "vag_part_number"),
                     "hw_number": ("bosch_hw_0281", "bosch_hw_0261", "vag_part_number"),
                     "hw_code": (), "ecu_type": ("ecu_family_name",)}


def _rule_for(token: str) -> str | None:
    norm = normalize_id(token)
    for rule, prefix in (("bosch_sw_1037", "1037"), ("bosch_hw_0281", "0281"), ("bosch_hw_0261", "0261")):
        if norm.startswith(prefix) and len(norm) == 10 and norm.isdigit():
            return rule
    if vag_base(norm):
        return "vag_part_number"
    return None


def _ecu_root(name: str) -> str:
    return normalize_id(name)


class _Store:
    """Read-only access to the local library store (originals, pairs, practice)."""

    def __init__(self, root: Path | None) -> None:
        self.root = root

    def original(self, sha: str) -> bytes | None:
        if self.root is None:
            return None
        path = self.root / "originals" / f"{sha}.bin.gz"
        try:
            data = gzip.decompress(path.read_bytes())
        except OSError:
            return None
        return data if hashlib.sha256(data).hexdigest() == sha else None

    def pair_evidence(self, pair_id: str) -> list[str]:
        if self.root is None:
            return []
        path = self.root / "pairs" / f"{pair_id}.json.gz"
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError):
            return []
        return list(payload.get("record", {}).get("evidence", []))

    def practice(self) -> dict[str, Any]:
        if self.root is None:
            return {}
        from .practice import read_stats

        try:
            return read_stats(self.root) or {}
        except (OSError, ValueError):
            return {}


def _identity_fields(variant: dict[str, Any], family: dict[str, Any], data: bytes,
                     input_ids: dict[str, list[Identifier]]) -> dict[str, list[dict[str, Any]]]:
    matched, missing, revisions, conflicts = [], [], [], []
    tokens: dict[str, list[str]] = {k: [t for t in v if _useful(t)] for k, v in variant.get("identity", {}).items()}
    for token in family.get("family_identity", {}).get("ecu_type", []):
        if _useful(token) and token not in tokens.setdefault("ecu_type", []):
            tokens["ecu_type"].append(token)
    all_input = [i for items in input_ids.values() for i in items]
    for field_name, values in sorted(tokens.items()):
        for token in values:
            raw = token.encode("latin-1", "ignore")
            offset = data.find(raw)
            if offset >= 0:
                matched.append({"field": field_name, "reference": token, "input": token, "input_offset": offset,
                                "provenance": "token uit WinOLS-projectgegevens, letterlijk in referentie-origineel "
                                              "en letterlijk in invoer"})
                continue
            rule = _rule_for(token)
            same_kind = [i for i in all_input if rule and i.rule == rule]
            related = [i for i in same_kind if compare_ids(token, i.raw) == "same_base_other_revision"]
            if related:
                for item in related:
                    revisions.append({"field": field_name, "reference": token, "input": item.raw,
                                      "input_offset": item.offset, "base": vag_base(item.normalized),
                                      "relation": "same_base_other_revision",
                                      "provenance": f"regel {item.rule} ({item.status})"})
            elif same_kind and field_name in ("sw_number", "sw_version", "hw_number"):
                conflicts.append({"field": field_name, "reference": token,
                                  "input": [i.raw for i in same_kind][:4], "hard": False,
                                  "provenance": f"regel {rule} ({same_kind[0].status})"})
            else:
                missing.append({"field": field_name, "reference": token})
    # ECU family names: a different family name in the input and none of the
    # reference's names present is a hard conflict.
    ref_names = {_ecu_root(t) for t in tokens.get("ecu_type", [])} | ({_ecu_root(family["ecu_type"])}
                                                                       if family.get("ecu_type") else set())
    input_names = {i.normalized for i in input_ids.get("ecu_type", [])}
    family_seen = any(m["field"] == "ecu_type" for m in matched) or bool(ref_names & input_names) or any(
        n.startswith(r) or r.startswith(n) for n in input_names for r in ref_names if len(r) >= 5 and len(n) >= 5)
    if input_names and ref_names and not family_seen:
        conflicts.append({"field": "ecu_type", "reference": sorted(ref_names), "input": sorted(input_names),
                          "hard": True, "provenance": "regel ecu_family_name (pattern_unvalidated)"})
    return {"matched": matched, "missing": missing, "revisions": revisions, "conflicts": conflicts,
            "family_seen": [family_seen]}


def _identity_status(fields: dict[str, list], same_content: bool) -> tuple[str, str]:
    if same_content:
        return "IDENTICAL_CONTENT", "SHA-256 van invoer en referentie-origineel zijn gelijk."
    hard = [c for c in fields["conflicts"] if c.get("hard")]
    if hard:
        return "CONFLICT", f"ECU-familie tegenstrijdig: referentie {hard[0]['reference']}, invoer {hard[0]['input']}."
    if fields["matched"] and not (fields["missing"] or fields["revisions"] or fields["conflicts"]):
        return "IDENTITY_CONFIRMED", "Alle identificatietokens van de referentie staan letterlijk in de invoer."
    family_seen = fields["family_seen"][0] or any(m["field"] == "ecu_type" for m in fields["matched"])
    if (family_seen or fields["matched"]) and fields["revisions"] and not fields["conflicts"]:
        return ("SAME_FAMILY_OTHER_REVISION",
                "Zelfde softwarebasis, andere revisie-index: " + ", ".join(
                    f"{r['reference']} -> {r['input']}" for r in fields["revisions"][:3]) + ".")
    if family_seen or fields["matched"]:
        return "SAME_FAMILY_OTHER_IDS", "ECU-familie overeenkomend; andere of ontbrekende SW/HW-identificatie."
    return "INSUFFICIENT_EVIDENCE", "Geen identificatieveld van de referentie is in de invoer terug te vinden."


def _transfer_check(data: bytes, package, variant_id: str, original_sha: str, profile: str) -> dict[str, Any]:
    from .transfer import choose_known_pair, place_edits, tuned_presence

    pair = choose_known_pair(package.load_variant(variant_id), profile, original_sha)
    if pair is None or not pair.get("edits"):
        return {"status": "NOT_CHECKED", "detail": f"geen '{profile}'-voorbeeld bij dit referentie-origineel"}
    fraction, _large = tuned_presence(data, pair["edits"])
    if fraction >= 0.5:
        return {"status": "ALREADY_TUNED", "detail": f"{fraction:.0%} van de wijzigingen zit al in de invoer",
                "pair_id": pair["pair_id"]}
    _output, placed, missing = place_edits(data, pair["edits"])
    how: dict[str, int] = {}
    for item in placed:
        how[item["how"]] = how.get(item["how"], 0) + 1
    missing = [m for m in missing if not m.get("present")]
    total = len(pair["edits"])
    if missing:
        status = "TRANSFER_CHECK_INCOMPLETE"
    elif how.get("omgerekend"):
        status = "TRANSFER_CHECK_WITH_RECALCULATION"
    else:
        status = "TRANSFER_CHECK_IDENTICAL_BYTES"
    return {"status": status, "pair_id": pair["pair_id"], "edits": total, "placed": len(placed),
            "missing": len(missing), "placement": how,
            "detail": f"{len(placed)} van {total} wijzigingen geplaatst ({how}); {len(missing)} niet plaatsbaar"}


def _already_tuned(data: bytes, pairs: list[dict[str, Any]]) -> str | None:
    """Reason text when the reference's tuned bytes are already in ``data``
    (whole tune, or large edits partially) - such a file is not an original."""

    from .transfer import tuned_presence

    for pair in pairs:
        if not pair.get("edits"):
            continue
        fraction, large = tuned_presence(data, pair["edits"])
        if fraction >= 0.5:
            return f"{fraction:.0%} van de wijzigingen van '{pair.get('tuned_version_name', '')}' zit al in de invoer"
        if large:
            return f"{large} grote wijziging(en) van '{pair.get('tuned_version_name', '')}' zitten al in de invoer"
    return None


def _next_step(identity: str, compatibility: str, reference: str, fields: dict[str, list]) -> str:
    if identity == "CONFLICT":
        return "Controleer of dit dezelfde ECU is (sticker/uitleesrapport); deze referentie niet gebruiken."
    if reference == "REJECTED_BY_REVIEW":
        return "Eerder afgewezen door een beoordelaar; zie de reviewgeschiedenis."
    if compatibility == "ALREADY_TUNED":
        return "Open het originele (ongetunede) bestand."
    if compatibility in ("EXACT_VARIANT_KNOWN_PAIR", "TRANSFER_CHECK_IDENTICAL_BYTES"):
        return "Checksum met de flashtool corrigeren en het resultaat op de bank/auto valideren."
    if compatibility == "TRANSFER_CHECK_WITH_RECALCULATION":
        return ("Omgerekende kaarten handmatig in WinOLS nalopen, of een origineel van exact deze "
                "softwarerevisie met getunede versie toevoegen.")
    if compatibility == "TRANSFER_CHECK_INCOMPLETE":
        return "Een project van deze softwarerevisie toevoegen; niet alle wijzigingen zijn veilig te plaatsen."
    if fields["revisions"]:
        return (f"Een WinOLS-project met SW {fields['revisions'][0]['input']} toevoegen, of de "
                "overzetcontrole uitvoeren (explain --check).")
    if fields["missing"]:
        return "Identificatie ontbreekt in de invoer: controleer de uitleesvorm (volledig/kalibratie)."
    return "Overzetcontrole uitvoeren (explain --check) of een referentie van deze ECU toevoegen."


def explain(data: bytes, package, *, store_root: Path | None = None, hint: str = "", profile: str | None = None,
            top: int = 5, check: bool = False, exclude: set[str] | None = None,
            graph=None) -> dict[str, Any]:
    """Ranked, explained reference candidates for ``data`` (read-only)."""

    from .transfer import rank_donors

    input_sha = hashlib.sha256(data).hexdigest()
    report: dict[str, Any] = {"engine": ENGINE_VERSION, "fingerprint_algorithm": ALGORITHM_VERSION,
                              "input": {"sha256": input_sha, "size": len(data)}, "candidates": []}
    classification = classify_input(data)
    if classification.kind != "raw_image":
        report["verdict"] = {"status": "NOT_COMPARABLE", "reason": classification.message}
        return report
    ids = extract_identifiers(data)
    by_field = identifiers_by_field(ids)
    report["input"]["identifiers"] = [{"field": i.field, "raw": i.raw, "normalized": i.normalized,
                                       "offset": i.offset, "rule": i.rule, "status": i.status} for i in ids]
    store = _Store(store_root)
    practice = store.practice().get("families", {})
    lookup = {variant["variant_id"]: (family, variant) for family, variant in package.variants()}
    exclude = set(exclude or ())

    # Candidates: the most similar originals plus every variant whose identity
    # tokens all occur in the input (it may have other content).
    donors = rank_donors(package, data, hint, profile, CANDIDATE_MIN_SIMILARITY, exclude, None)
    seen = {d["original_sha256"] for d in donors}
    by_variant: dict[str, dict[str, Any]] = {}
    for entry in package.fingerprints():
        if entry["original_sha256"] not in exclude:
            by_variant.setdefault(entry["variant_id"], entry)
    for variant_id, entry in sorted(by_variant.items()):
        family, variant = lookup.get(variant_id, (None, None))
        if variant is None or entry["original_sha256"] in seen:
            continue
        tokens = [t for v in variant.get("identity", {}).values() for t in v if _useful(t)]
        if tokens and all(t.encode("latin-1", "ignore") in data for t in tokens):
            donors.append(dict(entry, similarity=0.0, same_offset_similarity=0.0, hint_overlap=0,
                               via="identity_tokens"))
            seen.add(entry["original_sha256"])
    input_blocks = None
    candidates = []
    for donor in donors[:max(top * 3, top)]:
        family, variant = lookup.get(donor["variant_id"], (None, None))
        if variant is None:
            continue
        fields = _identity_fields(variant, family, data, by_field)
        same_content = donor["original_sha256"] == input_sha
        identity_status, identity_reason = _identity_status(fields, same_content)
        binary: dict[str, Any] = {"available": False, "reason": "referentie-origineel niet in de lokale opslag"}
        reference_bytes = store.original(donor["original_sha256"])
        if reference_bytes is not None:
            if input_blocks is None:
                input_blocks = block_digests(data)
            binary = {"available": True, "sha256_identical": same_content,
                      **compare_blocks(input_blocks, block_digests(reference_bytes))}
        wanted = profile or next((p for p in ("stage1", "stage2") if p in donor.get("profiles", [])),
                                 (donor.get("profiles") or [None])[0])
        # reference status
        pairs = [p for p in package.load_variant(donor["variant_id"])["known_pairs"].get(wanted or "", [])
                 if p.get("original_sha256") == donor["original_sha256"]] if wanted else []
        evidence_items = sorted({e for p in pairs for e in store.pair_evidence(p["pair_id"])})
        reference_status = ("INDEPENDENT_EXPORT_MATCH" if any("independent_winols_export" in e for e in evidence_items)
                            else "PROJECT_PAIR" if pairs else "NO_REFERENCE")
        relations = []
        if graph is not None:
            relations = graph.between(f"file:{input_sha}", f"file:{donor['original_sha256']}")
            statuses = {r["status"] for r in relations}
            if "rejected" in statuses:
                reference_status = "REJECTED_BY_REVIEW"
            elif "validated" in statuses:
                reference_status = "VALIDATED_RELATION"
            elif "user_confirmed" in statuses and reference_status == "NO_REFERENCE":
                reference_status = "USER_REVIEWED_NOT_INDEPENDENT"
        # compatibility status
        same_size = donor["size"] == len(data)
        tuned_already = _already_tuned(data, pairs)
        if identity_status == "CONFLICT" or reference_status == "REJECTED_BY_REVIEW":
            compatibility = {"status": "BLOCKED", "detail": identity_reason}
        elif tuned_already is not None:
            compatibility = {"status": "ALREADY_TUNED", "detail": tuned_already}
        elif identity_status in ("IDENTICAL_CONTENT", "IDENTITY_CONFIRMED") and same_size and pairs:
            compatibility = {"status": "EXACT_VARIANT_KNOWN_PAIR",
                             "detail": f"bekende '{wanted}'-versie van exact deze softwarevariant"}
        elif check and wanted:
            compatibility = _transfer_check(data, package, donor["variant_id"], donor["original_sha256"], wanted)
        else:
            compatibility = {"status": "NOT_CHECKED", "detail": "overzetcontrole niet uitgevoerd"}
        fam_practice = practice.get(family["family_key"], {}) if family else {}
        uncertainties = []
        if any(i.status == "pattern_unvalidated" for i in ids):
            uncertainties.append("Uit de invoer gelezen nummers komen uit patroonregels (niet per ECU gevalideerd).")
        if donor.get("cross_size"):
            uncertainties.append("Andere bestandsgrootte: vergeleken via ankerfragmenten, niet op gelijke offsets.")
        if not binary["available"]:
            uncertainties.append("Geen blokvergelijking: referentie-origineel ontbreekt lokaal.")
        if compatibility["status"] == "TRANSFER_CHECK_WITH_RECALCULATION":
            uncertainties.append("Een deel van de waarden is omgerekend, niet byte-identiek overgenomen.")
        if fam_practice.get("tests", 0) < 5:
            uncertainties.append("Te weinig oefentests voor deze ECU-familie om de overzetgrens te leren.")
        candidates.append({
            "reference": {"variant_id": donor["variant_id"], "family_key": family["family_key"] if family else None,
                          "original_sha256": donor["original_sha256"], "size": donor["size"],
                          "project": donor.get("project", ""), "vehicle": donor.get("vehicle", ""),
                          "profiles": donor.get("profiles", [])},
            "similarity_score": round(float(donor.get("similarity", 0.0)), 4),
            "similarity_method": ("anker-fragmenten (andere grootte)" if donor.get("cross_size")
                                  else "gevonden via identificatietokens" if donor.get("via")
                                  else "steekproef op gelijke offsets / ankerfragmenten"),
            "identity_status": identity_status,
            "category": CATEGORY[identity_status],
            "compatibility_status": compatibility["status"],
            "compatibility": compatibility,
            "reference_status": reference_status,
            "matched_fields": fields["matched"],
            "missing_fields": fields["missing"],
            "revision_differences": fields["revisions"],
            "conflicting_fields": fields["conflicts"],
            "binary": binary,
            "provenance": {"reference": "WinOLS-project (versievolgorde via RevisionTag, slots hash-geverifieerd)",
                           "pair_evidence": evidence_items, "relations": [
                               {"id": r["id"], "kind": r["kind"], "status": r["status"]} for r in relations],
                           "parser_offsets_used_as_identity": False},
            "practice": {k: fam_practice.get(k) for k in ("tests", "good", "learned_min_similarity")
                         if k in fam_practice},
            "uncertainties": uncertainties,
            "reason": identity_reason,
            "next_step": _next_step(identity_status, compatibility["status"], reference_status, fields),
        })
    candidates.sort(key=lambda c: (IDENTITY_RANK[c["identity_status"]], -c["similarity_score"],
                                   c["reference"]["original_sha256"]))
    report["candidates"] = candidates[:top]
    report["verdict"] = _verdict(report["candidates"])
    return report


def _verdict(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidates:
        return {"status": "NO_CANDIDATE", "reason": "Geen referentie met meetbare overeenkomst in de bibliotheek."}
    best = candidates[0]
    decided = best["compatibility_status"] in ("EXACT_VARIANT_KNOWN_PAIR", "TRANSFER_CHECK_IDENTICAL_BYTES")
    return {"status": "DECIDED" if decided else ("BLOCKED" if best["compatibility_status"] == "BLOCKED"
                                                   else "UNDECIDED"),
            "identity_status": best["identity_status"], "compatibility_status": best["compatibility_status"],
            "reference_status": best["reference_status"], "reason": best["reason"], "next_step": best["next_step"]}


def format_report(report: dict[str, Any]) -> str:
    """Short Dutch text for the app and the console."""

    lines = [f"Invoer: {report['input']['size']} bytes, SHA-256 {report['input']['sha256'][:16]}…"]
    ids = report["input"].get("identifiers", [])
    if ids:
        lines.append("Gevonden in invoer: " + ", ".join(f"{i['raw']} ({i['rule']})" for i in ids[:6]))
    verdict = report["verdict"]
    lines.append(f"Oordeel: {verdict['status']}" + (f" – {verdict.get('reason', '')}" if verdict.get("reason") else ""))
    for n, c in enumerate(report["candidates"], 1):
        ref = c["reference"]
        lines.append(f"{n}. {ref['vehicle'] or ref['project'] or ref['variant_id']} [{ref['family_key']}] "
                     f"overeenkomst {c['similarity_score']:.1%} (gemeten) · identiteit {c['identity_status']} · "
                     f"compatibiliteit {c['compatibility_status']} · referentie {c['reference_status']}")
        for r in c["revision_differences"][:3]:
            lines.append(f"     revisie: {r['reference']} ↔ {r['input']} (zelfde basis {r['base']})")
        for m in c["missing_fields"][:3]:
            lines.append(f"     ontbreekt: {m['field']} {m['reference']}")
        for x in c["conflicting_fields"][:3]:
            lines.append(f"     conflict: {x['field']} referentie {x['reference']} / invoer {x['input']}")
        if c["binary"].get("available"):
            b = c["binary"]
            lines.append(f"     blokken: {b['identical_same_offset']} gelijk, {b['identical_moved']} verschoven, "
                         f"{b['different']} anders, {b['missing_in_reference']} ontbrekend")
        lines.append(f"     volgende stap: {c['next_step']}")
    return "\n".join(lines)


def evaluate(package, store_root: Path, *, limit: int | None = None) -> dict[str, Any]:
    """Leave-one-out measurement on the library's own originals.

    Every stored original is hidden and explained against the rest. Truth is
    its own ECU family. ``decided`` = best candidate's identity is
    IDENTICAL/CONFIRMED/OTHER_REVISION. Counts and rates are reported, no
    probabilities are derived."""

    store = _Store(store_root)
    entries = sorted({e["original_sha256"]: e for e in package.fingerprints()}.values(),
                     key=lambda e: e["original_sha256"])
    family_count: dict[str, int] = {}
    for e in entries:
        family_count[e["family_key"]] = family_count.get(e["family_key"], 0) + 1
    tp = fp = fn = undecided = tested = conflicts = 0
    for entry in entries:
        if limit is not None and tested >= limit:
            break
        data = store.original(entry["original_sha256"])
        if data is None:
            continue
        tested += 1
        report = explain(data, package, store_root=store_root, top=3, exclude={entry["original_sha256"]})
        best = report["candidates"][0] if report["candidates"] else None
        has_peer = family_count.get(entry["family_key"], 0) > 1
        decided = best is not None and best["identity_status"] in (
            "IDENTICAL_CONTENT", "IDENTITY_CONFIRMED", "SAME_FAMILY_OTHER_REVISION")
        if best is not None and best["identity_status"] == "CONFLICT":
            conflicts += 1
        if decided:
            if best["reference"]["family_key"] == entry["family_key"]:
                tp += 1
            else:
                fp += 1
        else:
            undecided += 1
            if has_peer:
                fn += 1
    return {"tested": tested, "true_positive": tp, "false_positive": fp, "false_negative": fn,
            "undecided": undecided, "best_is_conflict": conflicts,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "undecided_share": undecided / tested if tested else None,
            "note": "Leave-one-out op de eigen bibliotheek; waarheid = ECU-familie van het verborgen origineel."}
