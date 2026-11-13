"""Frozen, versioned tuning packages and the local package store."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import SOFTWARE_VERSION
from .learn import DEFAULT_SETTINGS, TrainingPair, learn_modification_zones, learn_profile

PACKAGE_FORMAT = "ols-tuner-package"
PACKAGE_SCHEMA = 1
PACKAGE_SUFFIX = ".olstune"

PROFILE_LABELS = {"stage1": "Stage 1"}


def default_store_root() -> Path:
    override = os.environ.get("OLS_TUNER_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "OLS Tuner"
    return Path.home() / ".ols-tuner"


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def build_package(
    pairs: list[TrainingPair],
    *,
    other_pairs: list[TrainingPair] | None = None,
    settings: dict[str, Any] | None = None,
    holdout: list[str] | None = None,
    excluded: list[dict[str, Any]] | None = None,
    test_results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic package. ``package_id`` excludes ``created_at``."""

    cfg = dict(DEFAULT_SETTINGS)
    cfg.update(settings or {})
    holdout_set = set(holdout or [])
    training = sorted((p for p in pairs if p.pair_id not in holdout_set), key=lambda p: p.pair_id)
    families: dict[str, list[TrainingPair]] = {}
    for pair in training:
        families.setdefault(pair.family_key, []).append(pair)
    others: dict[str, list[TrainingPair]] = {}
    for pair in other_pairs or []:
        others.setdefault(pair.family_key, []).append(pair)

    family_out = []
    for family_key in sorted(families):
        members = families[family_key]
        by_profile: dict[str, list[TrainingPair]] = {}
        for pair in members:
            by_profile.setdefault(pair.profile, []).append(pair)
        variants: dict[str, dict[str, Any]] = {}
        for pair in members:
            variant = variants.setdefault(pair.variant_key, {
                "variant_key": pair.variant_key,
                "identity": pair.identity,
                "size": len(pair.original),
                "pairs": [],
                "projects": [],
                "read_forms": [],
            })
            variant["pairs"].append(pair.pair_id)
            project = pair.record.get("project_sha256")
            if project and project not in variant["projects"]:
                variant["projects"].append(project)
            read_form = pair.record.get("read_form", "onbekend")
            if read_form not in variant["read_forms"]:
                variant["read_forms"].append(read_form)
        for variant in variants.values():
            variant["pairs"].sort()
            variant["projects"].sort()
            variant["read_forms"].sort()
            variant["evidence_level"] = (
                "REPRODUCTION_ONLY" if len(variant["projects"]) < 2 else "MULTI_PROJECT"
            )
        reference_states = sorted({p.record.get("checksum_state", "UNKNOWN") for p in members})
        first = members[0]
        family_out.append({
            "family_key": family_key,
            "ecu_type": first.record.get("ecu_type", ""),
            "ecu_manufacturer": first.record.get("ecu_manufacturer", ""),
            "size": len(first.original),
            "family_identity": {"ecu_type": first.identity.get("ecu_type", [])},
            "variants": [variants[k] for k in sorted(variants)],
            "profiles": {
                profile: learn_profile(sorted(group, key=lambda p: p.pair_id), cfg)
                for profile, group in sorted(by_profile.items())
            },
            "modification_zones": learn_modification_zones(
                sorted(others.get(family_key, []), key=lambda p: p.pair_id), cfg
            ),
            "integrity": {
                "reference_checksum_states": reference_states,
                "contract": "UNKNOWN",
                "explanation": (
                    "De trainingsreferenties zijn door WinOLS gemarkeerd als "
                    + ", ".join(reference_states)
                    + ". Een ECU-checksum- of handtekeningmethode voor deze "
                    "variant is niet geverifieerd; zonder bevestigd contract "
                    "wordt geen gebruiksklare BIN geschreven."
                ),
            },
        })
    selection = [
        {k: v for k, v in sorted(p.record.items())} | {"pair_id": p.pair_id, "profile": p.profile}
        for p in training
    ]
    body = {
        "format": PACKAGE_FORMAT,
        "schema_version": PACKAGE_SCHEMA,
        "software_version": SOFTWARE_VERSION,
        "settings": cfg,
        "data_version": hashlib.sha256(
            _canonical(sorted(p.pair_id for p in training))
        ).hexdigest(),
        "training_selection": selection,
        "holdout_pairs": sorted(holdout_set),
        "excluded_candidates": sorted(excluded or [], key=lambda x: _canonical(x)),
        "families": family_out,
        "test_results": test_results or {},
    }
    body["package_id"] = hashlib.sha256(_canonical(body)).hexdigest()[:16]
    body["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return body


def package_content_id(package: dict[str, Any]) -> str:
    body = {k: v for k, v in package.items() if k not in ("package_id", "created_at")}
    return hashlib.sha256(_canonical(body)).hexdigest()[:16]


def write_package(package: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = gzip.compress(_canonical(package), mtime=0)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pkg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def read_package(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        package = json.loads(gzip.decompress(raw))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Geen geldig modelpakket: {path.name} ({exc})") from exc
    if package.get("format") != PACKAGE_FORMAT or package.get("schema_version") != PACKAGE_SCHEMA:
        raise ValueError(f"Onbekend pakketformaat in {path.name}.")
    if package_content_id(package) != package.get("package_id"):
        raise ValueError(f"Modelpakket {path.name} is beschadigd of gewijzigd (ID klopt niet).")
    return package


class PackageStore:
    """Local package store with an active pointer and rollback history."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_store_root()
        self.packages_dir = self.root / "packages"
        self.state_path = self.root / "active.json"
        self.contracts_path = self.root / "integrity_contracts.json"

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"active": None, "history": []}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def install(self, package: dict[str, Any], activate: bool = True) -> Path:
        path = self.packages_dir / f"{package['package_id']}{PACKAGE_SUFFIX}"
        if not path.exists():
            write_package(package, path)
        if activate:
            state = self._state()
            if state["active"] and state["active"] != package["package_id"]:
                state["history"].append(state["active"])
            state["active"] = package["package_id"]
            self._write_state(state)
        return path

    def install_file(self, source: Path, activate: bool = True) -> dict[str, Any]:
        package = read_package(source)
        self.install(package, activate=activate)
        return package

    def active_id(self) -> str | None:
        return self._state()["active"]

    def load_active(self) -> dict[str, Any] | None:
        active = self.active_id()
        if not active:
            return None
        return read_package(self.packages_dir / f"{active}{PACKAGE_SUFFIX}")

    def list_packages(self) -> list[dict[str, Any]]:
        items = []
        if self.packages_dir.exists():
            for path in sorted(self.packages_dir.glob(f"*{PACKAGE_SUFFIX}")):
                try:
                    package = read_package(path)
                except ValueError as exc:
                    items.append({"path": str(path), "error": str(exc)})
                    continue
                items.append({
                    "package_id": package["package_id"],
                    "created_at": package["created_at"],
                    "families": [f["family_key"] for f in package["families"]],
                    "pairs": len(package["training_selection"]),
                    "path": str(path),
                })
        return items

    def rollback(self) -> str:
        state = self._state()
        if not state["history"]:
            raise ValueError("Er is geen vorige pakketversie om naar terug te gaan.")
        previous = state["history"].pop()
        state["active"] = previous
        self._write_state(state)
        return previous

    # Integrity contracts are user statements kept outside the frozen package.
    def contracts(self) -> dict[str, Any]:
        if not self.contracts_path.exists():
            return {}
        return json.loads(self.contracts_path.read_text(encoding="utf-8"))

    def set_contract(self, family_key: str, contract: str, evidence: str) -> None:
        if contract not in ("FLASH_TOOL_CORRECTS_ON_WRITE", "UNKNOWN"):
            raise ValueError("Onbekend integriteitscontract.")
        if contract != "UNKNOWN" and len(evidence.strip()) < 10:
            raise ValueError("Leg vast waarop het contract berust (minimaal 10 tekens).")
        data = self.contracts()
        data[family_key] = {
            "contract": contract,
            "evidence": evidence.strip(),
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.contracts_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.contracts_path)
