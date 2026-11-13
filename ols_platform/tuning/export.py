"""Atomic, verified export of a generated image plus a local report."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import SOFTWARE_VERSION
from .generate import GenerationResult
from .package import PROFILE_LABELS


class ExportBlocked(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def verify_output_file(original_path: Path, output_path: Path, allowed: list[tuple[int, int]],
                       expected_sha256: str) -> dict[str, Any]:
    """Independent read route: re-read both files from disk and diff them.

    It does not reuse the generator's in-memory buffers or region logic; it
    only knows the allowed byte ranges declared in the package profile.
    """

    original = original_path.read_bytes()
    written = output_path.read_bytes()
    checks: dict[str, Any] = {}
    checks["length_equal"] = len(original) == len(written)
    checks["sha256_matches_generation"] = hashlib.sha256(written).hexdigest() == expected_sha256
    outside = 0
    changed = 0
    allowed_sorted = sorted(allowed)
    pointer = 0
    if checks["length_equal"]:
        block = 65536
        for base in range(0, len(original), block):
            a = original[base:base + block]
            b = written[base:base + block]
            if a == b:
                continue
            for i in range(len(a)):
                if a[i] != b[i]:
                    position = base + i
                    changed += 1
                    while pointer < len(allowed_sorted) and allowed_sorted[pointer][1] <= position:
                        pointer += 1
                    if not (pointer < len(allowed_sorted) and allowed_sorted[pointer][0] <= position):
                        outside += 1
    checks["changed_bytes"] = changed
    checks["changes_outside_allowed_regions"] = outside
    checks["passed"] = (
        checks["length_equal"] and checks["sha256_matches_generation"] and outside == 0 and changed > 0
    )
    return checks


def _report(result: GenerationResult, original_path: Path, output_path: Path,
            checks: dict[str, Any], package: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_type": "ols-tuner-export",
        "software_version": SOFTWARE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": {"file": original_path.name, "sha256": result.input_sha256},
        "output": {"file": output_path.name, "sha256": result.output_sha256},
        "profile": PROFILE_LABELS.get(result.profile, result.profile),
        "notes": result.notes,
        "package": {
            "package_id": result.package_id,
            "data_version": package.get("data_version"),
            "created_at": package.get("created_at"),
        },
        "ecu_identity": result.identity,
        "evidence_level": result.evidence_level,
        "changed_regions": [
            {k: v for k, v in change.items() if k != "map"} | (
                {"map_definition": change["map"]} if change.get("map") else {}
            )
            for change in result.changes
        ],
        "integrity": result.integrity,
        "verification": checks,
    }


def export_result(result: GenerationResult, original_path: Path, output_dir: Path,
                  output_name: str, package: dict[str, Any]) -> tuple[Path, Path]:
    """Write ``output_name`` atomically; never overwrite; leave no partial file."""

    if not result.integrity.get("ready_for_use"):
        raise ExportBlocked(
            "integrity_unknown",
            "Export geblokkeerd: de integriteitsmethode (checksum/handtekening) voor "
            f"familie {result.identity.get('family')} is niet bevestigd. Leg in "
            "'Bibliotheek en diagnose' vast of AutoTuner de checksum bij schrijven corrigeert.",
        )
    if hashlib.sha256(original_path.read_bytes()).hexdigest() != result.input_sha256:
        raise ExportBlocked("input_changed", "Het originele bestand is gewijzigd sinds het genereren.")
    output_dir.mkdir(parents=True, exist_ok=True)
    final = output_dir / output_name
    report_path = output_dir / (output_name + ".rapport.json")
    resolved_original = original_path.resolve()
    if final.resolve() == resolved_original:
        raise ExportBlocked("would_overwrite_original", "De uitvoer zou het origineel overschrijven.")
    if final.exists() or report_path.exists():
        raise ExportBlocked("exists", f"{final.name} bestaat al; eerdere exports worden niet overschreven.")
    fd, tmp_name = tempfile.mkstemp(dir=output_dir, prefix=".tune-", suffix=".part")
    tmp = Path(tmp_name)
    try:
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as handle:
            handle.write(result.output)
            handle.flush()
            os.fsync(handle.fileno())
        checks = verify_output_file(original_path, tmp, result.allowed_ranges, result.output_sha256)
        if not checks["passed"]:
            raise ExportBlocked(
                "verification_failed",
                f"Controle van het geschreven bestand mislukt: {json.dumps(checks)}",
            )
        report = _report(result, original_path, final, checks, package)
        report_tmp = output_dir / (".tune-" + os.urandom(6).hex() + ".json.part")
        report_tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            # os.link fails if the target exists: atomic no-overwrite publish.
            try:
                os.link(tmp, final)
            except (AttributeError, NotImplementedError, PermissionError):
                if final.exists():
                    raise FileExistsError(final)
                os.replace(tmp, final)
            except FileExistsError:
                raise
            try:
                try:
                    os.link(report_tmp, report_path)
                except (AttributeError, NotImplementedError, PermissionError):
                    os.replace(report_tmp, report_path)
            except BaseException:
                # Never leave a BIN without its report.
                final.unlink(missing_ok=True)
                raise
        except FileExistsError as exc:
            raise ExportBlocked("exists", f"{final.name} bestaat al (gelijktijdige export).") from exc
        finally:
            if report_tmp.exists():
                report_tmp.unlink()
    except OSError as exc:
        if isinstance(exc, FileExistsError):
            raise
        raise ExportBlocked("write_failed", f"Schrijven mislukt: {exc}") from exc
    finally:
        if tmp.exists():
            tmp.unlink()
    return final, report_path
