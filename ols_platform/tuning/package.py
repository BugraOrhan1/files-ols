"""Frozen, versioned tuning packages and the local package store.

Package format v2 (``.olstune``) is a ZIP archive:

* ``manifest.json`` – package id, settings, training selection, families and
  their software variants (identity tokens, size, evidence, statistics) and
  the SHA-256 of every variant file;
* ``variants/<variant_id>.json`` – the learned profile(s), the exact edits of
  every known training pair and the modification zones for one software
  variant.

Generation reads the manifest, selects the variant from identity tokens and
size, and loads only that variant file. This keeps memory bounded when the
library holds thousands of projects.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import SOFTWARE_VERSION
from .learn import DEFAULT_SETTINGS, TrainingPair, diff_regions, learn_modification_zones, learn_profile

PACKAGE_FORMAT = "ols-tuner-package"
PACKAGE_SCHEMA = 2
PACKAGE_SUFFIX = ".olstune"
PROFILE_LABELS = {"stage1": "Stage 1"}
MAX_LEARNING_PAIRS = 32  # per variant; all pairs stay available as known pairs
_ZIP_DATE = (2020, 1, 1, 0, 0, 0)


def default_store_root() -> Path:
    override = os.environ.get("OLS_TUNER_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "OLS Tuner"
    return Path.home() / ".ols-tuner"


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def variant_id_for(family_key: str, variant_key: str) -> str:
    return hashlib.sha256(f"{family_key}\n{variant_key}".encode()).hexdigest()[:12]


def known_pair_edits(pair: TrainingPair, gap: int = 4) -> list[list[Any]]:
    return [
        [s, pair.original[s:e].hex(), pair.tuned[s:e].hex()]
        for s, e in diff_regions(pair.original, pair.tuned, gap)
    ]


class Package:
    """A loaded package: manifest in memory, variant files on demand."""

    def __init__(self, manifest: dict[str, Any], variant_blobs: dict[str, bytes] | None = None,
                 path: Path | None = None) -> None:
        self.manifest = manifest
        self._blobs = variant_blobs or {}
        self.path = path
        self._cache: dict[str, dict[str, Any]] = {}

    # dict-style access to the manifest keeps call sites simple
    def __getitem__(self, key: str) -> Any:
        return self.manifest[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.manifest.get(key, default)

    @property
    def package_id(self) -> str:
        return self.manifest["package_id"]

    def variants(self) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
        for family in self.manifest["families"]:
            for variant in family["variants"]:
                yield family, variant

    def load_variant(self, variant_id: str) -> dict[str, Any]:
        if variant_id in self._cache:
            return self._cache[variant_id]
        expected = None
        for _family, variant in self.variants():
            if variant["variant_id"] == variant_id:
                expected = variant["sha256"]
        if expected is None:
            raise KeyError(variant_id)
        raw = self._blobs.get(variant_id)
        if raw is None:
            if self.path is None:
                raise KeyError(variant_id)
            with zipfile.ZipFile(self.path) as archive:
                raw = archive.read(f"variants/{variant_id}.json")
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"Variantbestand {variant_id} in het modelpakket is beschadigd.")
        data = json.loads(raw)
        self._cache = {variant_id: data}  # keep at most one variant in memory
        return data


class PackageBuilder:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(settings or {})
        self.families: dict[str, dict[str, Any]] = {}
        self.blobs: dict[str, bytes] = {}
        self.selection: list[dict[str, Any]] = []

    def add_variant(self, target: list[TrainingPair], others: list[TrainingPair],
                    extra_known: Iterable[TrainingPair] = (), profile: str = "stage1") -> None:
        """Learn one software variant. ``extra_known`` adds pairs that are not
        used for learning (above the cap) but are kept as exact known pairs."""

        target = sorted(target, key=lambda p: p.pair_id)
        first = target[0]
        family = self.families.setdefault(first.family_key, {
            "family_key": first.family_key,
            "ecu_type": first.record.get("ecu_type", ""),
            "ecu_manufacturer": first.record.get("ecu_manufacturer", ""),
            "size": len(first.original),
            "family_identity": {"ecu_type": first.identity.get("ecu_type", [])},
            "variants": [],
            "reference_checksum_states": [],
        })
        known = sorted({p.pair_id: p for p in [*target, *extra_known]}.values(), key=lambda p: p.pair_id)
        learn_set = target[:MAX_LEARNING_PAIRS]
        learn_ids = {p.pair_id for p in learn_set}
        learned = learn_profile(learn_set, self.settings)
        variant_id = variant_id_for(first.family_key, first.variant_key)
        blob = {
            "variant_id": variant_id,
            "profiles": {profile: learned},
            "known_pairs": {profile: [
                {
                    "pair_id": p.pair_id,
                    "project_name": p.record.get("project_name", ""),
                    "original_sha256": p.record.get("original_sha256"),
                    "tuned_sha256": p.record.get("tuned_sha256"),
                    "tuned_version_name": p.record.get("tuned_version_name", ""),
                    "edits": known_pair_edits(p),
                }
                for p in known
            ]},
            "modification_zones": learn_modification_zones(sorted(others, key=lambda p: p.pair_id), self.settings),
        }
        raw = _canonical(blob)
        self.blobs[variant_id] = raw
        projects = sorted({p.record.get("project_sha256", "") for p in known} - {""})
        states = sorted({p.record.get("checksum_state", "UNKNOWN") for p in known})
        family["reference_checksum_states"] = sorted(set(family["reference_checksum_states"]) | set(states))
        family["variants"].append({
            "variant_id": variant_id,
            "variant_key": first.variant_key,
            "identity": first.identity,
            "size": len(first.original),
            "pairs": [p.pair_id for p in known],
            "learning_pairs": [p.pair_id for p in learn_set],
            "projects": projects,
            "read_forms": sorted({p.record.get("read_form", "onbekend") for p in known}),
            "evidence_level": "REPRODUCTION_ONLY" if len(projects) < 2 else "MULTI_PROJECT",
            "profiles": {profile: learned["statistics"]},
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
        for p in known:
            self.selection.append({k: v for k, v in sorted(p.record.items())} | {
                "pair_id": p.pair_id, "profile": p.profile, "variant_id": variant_id,
                "used_for_learning": p.pair_id in learn_ids,
            })

    def finish(self, *, holdout: list[str] | None = None, excluded: list[dict[str, Any]] | None = None,
               test_results: dict[str, Any] | None = None) -> Package:
        families = []
        for key in sorted(self.families):
            family = self.families[key]
            family["variants"].sort(key=lambda v: v["variant_id"])
            states = family.pop("reference_checksum_states")
            family["integrity"] = {
                "reference_checksum_states": states,
                "contract": "UNKNOWN",
                "explanation": (
                    "Trainingsreferenties: " + ", ".join(states) + ". Een ECU-checksum- of "
                    "handtekeningmethode is niet geverifieerd; zonder bevestigd contract wordt "
                    "geen gebruiksklare BIN geschreven."
                ),
            }
            families.append(family)
        selection = sorted(self.selection, key=lambda r: r["pair_id"])
        manifest = {
            "format": PACKAGE_FORMAT,
            "schema_version": PACKAGE_SCHEMA,
            "software_version": SOFTWARE_VERSION,
            "settings": self.settings,
            "data_version": hashlib.sha256(_canonical([r["pair_id"] for r in selection])).hexdigest(),
            "training_selection": selection,
            "holdout_pairs": sorted(holdout or []),
            "excluded_candidates": sorted(excluded or [], key=_canonical),
            "families": families,
            "test_results": test_results or {},
        }
        manifest["package_id"] = hashlib.sha256(_canonical(manifest)).hexdigest()[:16]
        manifest["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return Package(manifest, dict(self.blobs))


def build_package(pairs: list[TrainingPair], *, other_pairs: list[TrainingPair] | None = None,
                  settings: dict[str, Any] | None = None, holdout: list[str] | None = None,
                  excluded: list[dict[str, Any]] | None = None,
                  test_results: dict[str, Any] | None = None, profile: str = "stage1") -> Package:
    """In-memory convenience wrapper (tests, small sets)."""

    holdout_set = set(holdout or [])
    builder = PackageBuilder(settings)
    groups: dict[tuple[str, str], list[TrainingPair]] = {}
    for pair in pairs:
        if pair.pair_id not in holdout_set and pair.profile == profile:
            groups.setdefault((pair.family_key, pair.variant_key), []).append(pair)
    others: dict[tuple[str, str], list[TrainingPair]] = {}
    for pair in other_pairs or []:
        if pair.pair_id not in holdout_set:
            others.setdefault((pair.family_key, pair.variant_key), []).append(pair)
    for key in sorted(groups):
        builder.add_variant(groups[key], others.get(key, []), profile=profile)
    return builder.finish(holdout=sorted(holdout_set), excluded=excluded, test_results=test_results)


def package_content_id(manifest: dict[str, Any]) -> str:
    body = {k: v for k, v in manifest.items() if k not in ("package_id", "created_at")}
    return hashlib.sha256(_canonical(body)).hexdigest()[:16]


def write_package(package: Package, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("manifest.json", _ZIP_DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, json.dumps(package.manifest, indent=1, sort_keys=True))
        for _family, variant in package.variants():
            vid = variant["variant_id"]
            info = zipfile.ZipInfo(f"variants/{vid}.json", _ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, package._blobs.get(vid) or _read_blob(package, vid))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pkg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(buffer.getvalue())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def _read_blob(package: Package, variant_id: str) -> bytes:
    with zipfile.ZipFile(package.path) as archive:
        return archive.read(f"variants/{variant_id}.json")


def read_package(path: Path) -> Package:
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            names = set(archive.namelist())
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise ValueError(f"Geen geldig modelpakket: {path.name} ({exc})") from exc
    if manifest.get("format") != PACKAGE_FORMAT or manifest.get("schema_version") != PACKAGE_SCHEMA:
        raise ValueError(f"Onbekend of verouderd pakketformaat in {path.name}; werk de bibliotheek opnieuw bij.")
    if package_content_id(manifest) != manifest.get("package_id"):
        raise ValueError(f"Modelpakket {path.name} is beschadigd of gewijzigd (ID klopt niet).")
    package = Package(manifest, path=path)
    for _family, variant in package.variants():
        if f"variants/{variant['variant_id']}.json" not in names:
            raise ValueError(f"Modelpakket {path.name} mist variant {variant['variant_id']}.")
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

    def install(self, package: Package, activate: bool = True) -> Path:
        path = self.packages_dir / f"{package.package_id}{PACKAGE_SUFFIX}"
        if not path.exists():
            write_package(package, path)
        if activate:
            state = self._state()
            if state["active"] and state["active"] != package.package_id:
                state["history"].append(state["active"])
            state["active"] = package.package_id
            self._write_state(state)
        return path

    def install_file(self, source: Path, activate: bool = True) -> Package:
        package = read_package(source)
        self.install(package, activate=activate)
        return package

    def active_id(self) -> str | None:
        return self._state()["active"]

    def load_active(self) -> Package | None:
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
                    "package_id": package.package_id,
                    "created_at": package["created_at"],
                    "families": [f["family_key"] for f in package["families"]],
                    "variants": sum(1 for _ in package.variants()),
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
