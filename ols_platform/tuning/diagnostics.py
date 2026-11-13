"""App log and a privacy-safe diagnostics bundle.

``app.log`` records what the user did and why something was refused (file
names, sizes, short hashes, rejection codes and messages, errors). The
diagnostics ZIP bundles the logs, status files and *counts* only: no
packages, pairs, originals or other bytes from customer files.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sqlite3
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import SOFTWARE_VERSION

APP_LOG = "app.log"


def app_logger(root: Path) -> logging.Logger:
    """One logger per data folder, writing to ``<root>/app.log``."""

    name = "ols_tuner.app." + str(abs(hash(str(Path(root).resolve()))))
    logger = logging.getLogger(name)
    if not logger.handlers:
        try:
            root.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(root / APP_LOG, maxBytes=2_000_000, backupCount=2, encoding="utf-8",
                                          delay=True)
            handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s"))
            logger.addHandler(handler)
        except OSError:
            logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def _tail(path: Path, limit: int = 2_000_000) -> bytes:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        return handle.read()


def _library_summary(root: Path) -> dict:
    db = root / "library.sqlite3"
    if not db.exists():
        return {"library": "nog niet aangemaakt"}
    out: dict = {}
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        out["files_per_source_and_status"] = [
            {"source": s, "status": st, "count": c}
            for s, st, c in connection.execute("SELECT source,status,COUNT(*) FROM files GROUP BY source,status")
        ]
        out["pairs_per_source_and_profile"] = [
            {"source": s, "profile": p, "count": c}
            for s, p, c in connection.execute("SELECT source,profile,COUNT(*) FROM pairs GROUP BY source,profile")
        ]
        reasons: Counter = Counter()
        for (message,) in connection.execute("SELECT message FROM files WHERE message IS NOT NULL AND message!=''"):
            try:
                items = json.loads(message)
                for item in items if isinstance(items, list) else []:
                    reasons[item.get("reason", "?")] += 1
            except ValueError:
                reasons[message.split(":")[0][:120]] += 1
        out["rejection_reasons"] = [{"reason": r, "count": c} for r, c in reasons.most_common(40)]
        out["runs"] = [
            {"started": a, "finished": b, "status": st}
            for a, b, st in connection.execute("SELECT started_at,finished_at,status FROM runs ORDER BY id DESC LIMIT 10")
        ]
    finally:
        connection.close()
    return out


def _package_summary(store) -> dict:
    try:
        package = store.load_active()
    except Exception as exc:  # reported, not raised
        return {"error": str(exc)}
    if package is None:
        return {"active": None}
    families = []
    for family in package["families"]:
        families.append({
            "family_key": family["family_key"],
            "ecu_type": family["ecu_type"],
            "size": family["size"],
            "variants": [
                {"variant_key": v["variant_key"], "identity_tokens": v["identity"], "projects": len(v["projects"]),
                 "pairs": len(v["pairs"]), "profiles": sorted(v["profiles"]), "evidence": v["evidence_level"]}
                for v in family["variants"]
            ],
        })
    return {"package_id": package.package_id, "created_at": package["created_at"],
            "software_version": package["software_version"], "pairs": len(package["training_selection"]),
            "families": families}


def create_bundle(store, target: Path) -> Path:
    root = store.root
    info = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "software_version": SOFTWARE_VERSION,
        "frozen_exe": bool(getattr(sys, "frozen", False)),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "store": str(root),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("systeem.json", json.dumps(info, indent=2, ensure_ascii=False))
        for name in (APP_LOG, APP_LOG + ".1", APP_LOG + ".2", "library_worker.log", "library_status.json",
                     "settings.json", "integrity_contracts.json", "active.json"):
            path = root / name
            if path.exists():
                archive.writestr(name, _tail(path))
        try:
            archive.writestr("bibliotheek.json", json.dumps(_library_summary(root), indent=2, ensure_ascii=False))
        except Exception as exc:
            archive.writestr("bibliotheek.json", json.dumps({"error": str(exc)}))
        archive.writestr("modelpakket.json", json.dumps(_package_summary(store), indent=2, ensure_ascii=False))
    return target
