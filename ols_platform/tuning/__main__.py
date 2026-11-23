"""Command line for the tuning pipeline (same code as the app buttons).

Examples::

    python -m ols_platform.tuning train --py-ols C:\\tools\\py-ols ^
        --source "Database 1=\\\\Dell\\d\\Database 1" --exports D:\\exports
    python -m ols_platform.tuning generate origineel.bin --output-dir D:\\uit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .export import ExportBlocked, export_result
from .fileio import read_json
from .generate import GenerationRejected, generate
from .library import Library
from .package import PackageStore, read_package, write_package


def _store(args) -> PackageStore:
    return PackageStore(Path(args.store) if args.store else None)


def _reader(path: str | None):
    """Built-in reader by default; py-ols only when explicitly requested."""

    if path:
        from ..reader import PyOlsReader

        reader = PyOlsReader(Path(path))
        reader.source_path = Path(path)  # lets worker processes rebuild it
        return reader
    from .ols_native import NativeOlsReader

    return NativeOlsReader()


def _sources(values: list[str]) -> dict[str, Path]:
    out = {}
    for value in values:
        if "=" in value:
            key, path = value.split("=", 1)
        else:
            key, path = Path(value).name or value, value
        out[key] = Path(path)
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ols-tuner")
    parser.add_argument("--store", help="Lokale map voor bibliotheek en pakketten.")
    sub = parser.add_subparsers(dest="command", required=True)

    train = sub.add_parser("train", help="Bibliotheek bijwerken: archief scannen en pakket trainen.")
    train.add_argument("--source", action="append", default=[], help="NAAM=MAP (herhaalbaar).")
    train.add_argument("--exports", action="append", default=[], type=Path,
                       help="Map met onafhankelijke WinOLS-exports (extra bewijs).")
    train.add_argument("--py-ols", help="Optioneel: py-ols i.p.v. de ingebouwde OLS-lezer.")
    train.add_argument("--holdout", action="append", default=[],
                       help="Projectnaam of SHA-256 die buiten training blijft (incl. afstammelingen).")
    train.add_argument("--max-files", type=int)
    train.add_argument("--workers", type=int, help="Aantal bestanden tegelijk (standaard: automatisch).")
    train.add_argument("--retry-delay", type=float, default=1.0)
    train.add_argument("--no-activate", action="store_true")
    train.add_argument("--report", type=Path)

    gen = sub.add_parser("generate", help="Maak getunde BIN.")
    gen.add_argument("original", type=Path)
    gen.add_argument("--profile", default="stage1", help="Bijv. stage1, stage2, egr_off, stage1+egr_off.")
    gen.add_argument("--output-dir", type=Path, required=True)
    gen.add_argument("--name")
    gen.add_argument("--package", type=Path, help="Pakketbestand i.p.v. het actieve pakket.")
    gen.add_argument("--dry-run", action="store_true", help="Alleen genereren en controleren.")
    gen.add_argument("--min-similarity", type=float, default=70.0, help="Minimale gelijkenis bij overzetten (%%).")
    gen.add_argument("--min-coverage", type=float, default=100.0, help="Minimaal overgezet deel (%%).")

    upd = sub.add_parser("update-library", help="Bibliotheek bijwerken met de mappen uit de app-instellingen "
                         "(zo start de app het achtergrondproces).")
    upd.add_argument("--workers", type=int)
    upd.add_argument("--no-practice", action="store_true", help="Na het bijwerken niet oefenen.")
    upd.add_argument("--continuous", action="store_true",
                     help="Blijven oefenen: scannen, leren, oefenen en na een pauze opnieuw, tot Stoppen.")
    upd.add_argument("--pause-minutes", type=int, default=30, help="Pauze tussen rondes bij --continuous.")
    prc = sub.add_parser("practice", help="Oefenen: elk voorbeeld verbergen, opnieuw tunen en vergelijken.")
    prc.add_argument("--workers", type=int)
    lmp = sub.add_parser("learn-maps", help="Kaartkennis: kaartdefinities (assen) van alle tune-voorbeelden "
                                            "lezen, zodat kaarten ook in andere software gevonden worden.")
    lmp.add_argument("--py-ols", type=Path, help="Map met py-ols (standaard: instelling py_ols).")
    lmp.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    lmp.add_argument("--max-files", type=int)
    rules = sub.add_parser("map-rules", help="Regels per kaarttype: hoe tuners elke soort kaart aanpassen.")
    rules.add_argument("--min-examples", type=int, default=3)
    rules.add_argument("--json", action="store_true")
    sub.add_parser("packages", help="Toon pakketten.")
    sub.add_parser("rollback", help="Activeer de vorige pakketversie.")
    inst = sub.add_parser("install-package")
    inst.add_argument("file", type=Path)
    exp = sub.add_parser("export-package")
    exp.add_argument("file", type=Path)
    exp_ = sub.add_parser("explain", help="Leg uit welke referenties passen, wat ontbreekt en wat de volgende "
                          "controle is (alleen lezen).")
    exp_.add_argument("original", type=Path)
    exp_.add_argument("--profile")
    exp_.add_argument("--top", type=int, default=5)
    exp_.add_argument("--check", action="store_true",
                      help="Overzetcontrole: plaats de wijzigingen van elke kandidaat in het geheugen en meet "
                           "hoeveel op identieke originele bytes landen (schrijft niets).")
    exp_.add_argument("--filter", choices=("exact", "alternative_revision", "insufficient", "conflict"))
    exp_.add_argument("--json", action="store_true")
    exp_.add_argument("--propose", action="store_true",
                      help="Leg de beste kandidaat vast als voorgestelde relatie (voor beoordeling).")
    rev = sub.add_parser("review", help="Beoordeel een voorgestelde relatie (bevestigen is geen onafhankelijk bewijs).")
    rev.add_argument("relation_id", type=int)
    rev.add_argument("decision", choices=("confirm", "reject", "conflict", "reopen"))
    rev.add_argument("--reason", required=True)
    rev.add_argument("--reviewer", required=True)
    rel = sub.add_parser("relations", help="Toon relaties en hun beoordelingsgeschiedenis.")
    rel.add_argument("--file", type=Path, help="Alleen relaties van dit bestand.")
    fpi = sub.add_parser("fingerprint-index", help="Vingerafdrukindex van een map (stromend, hervatbaar).")
    fpi.add_argument("folder", type=Path)
    fpi.add_argument("--index", type=Path, help="Indexbestand (standaard in de opslag).")
    evm = sub.add_parser("evaluate-matching", help="Meet precision/recall door elk origineel te verbergen.")
    evm.add_argument("--limit", type=int)
    con = sub.add_parser("contract", help="Leg het integriteitscontract voor een familie vast.")
    con.add_argument("family_key")
    con.add_argument("--flash-tool-corrects", action="store_true")
    con.add_argument("--evidence", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = _store(args)
    if args.command == "train":
        library = Library(store)
        try:
            summary = library.scan(
                _sources(args.source), _reader(args.py_ols), export_dirs=args.exports,
                max_files=args.max_files, retry_delay=args.retry_delay, progress=print, workers=args.workers,
            )
            training = library.train(holdout_projects=args.holdout, activate=not args.no_activate,
                                     progress=print)
            summary["training"] = training
            summary["pairs_by_source"] = library.pair_counts_by_source()
        finally:
            library.close()
        text = json.dumps(summary, indent=2, ensure_ascii=False)
        if args.report:
            args.report.write_text(text, encoding="utf-8")
        print(text)
        return 0
    if args.command == "generate":
        package = read_package(args.package) if args.package else store.load_active()
        if package is None:
            print("Geen actief modelpakket. Koppel een pakket aan of werk de bibliotheek bij.")
            return 2
        data = args.original.read_bytes()
        try:
            result = generate(data, args.profile, package, store.contracts(), hint=args.original.name,
                              min_similarity=args.min_similarity / 100, min_coverage=args.min_coverage / 100)
        except GenerationRejected as exc:
            print(json.dumps({"status": "AFGEWEZEN", "code": exc.code, "reden": exc.message},
                             indent=2, ensure_ascii=False))
            return 3
        summary = {
            "status": "GEGENEREERD", "package_id": result.package_id, "notes": result.notes,
            "evidence_level": result.evidence_level,
            "input_sha256": result.input_sha256, "output_sha256": result.output_sha256,
            "regions": len(result.changes), "integrity": result.integrity,
        }
        if args.dry_run:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0
        name = args.name or f"{args.original.stem}_stage1_{result.output_sha256[:8]}.bin"
        try:
            out, report = export_result(result, args.original, args.output_dir, name, package)
        except ExportBlocked as exc:
            summary.update({"status": "EXPORT GEBLOKKEERD", "code": exc.code, "reden": exc.message})
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 4
        summary.update({"status": "GEËXPORTEERD", "output": str(out), "report": str(report)})
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    if args.command in ("explain", "review", "relations", "fingerprint-index", "evaluate-matching"):
        return _evidence_command(args, store)
    if args.command == "update-library":
        from .background import run_update

        return run_update(store.root, workers=args.workers, practice=not args.no_practice,
                          continuous=args.continuous, pause_minutes=max(1, args.pause_minutes))
    if args.command == "practice":
        from .background import run_update

        return run_update(store.root, workers=args.workers, practice_only=True)
    if args.command == "learn-maps":
        from .maps import learn_maps

        settings = read_json(store.root / "settings.json", {}) or {}
        py_ols = args.py_ols or Path(settings.get("py_ols_maps") or settings.get("py_ols") or ".")
        if not (py_ols / "ols_reader.py").exists():
            print("py-ols niet gevonden; geef --py-ols <map> op.")
            return 2
        print(json.dumps(learn_maps(store.root, py_ols, workers=args.workers, max_files=args.max_files)))
        return 0
    if args.command == "map-rules":
        from .maps import map_rules

        found = map_rules(store.root, args.min_examples)
        if args.json:
            print(json.dumps(found, indent=2))
            return 0
        for rule in found:
            low, high = rule["spread"]
            extra = f", {rule['to_max_share']:.0%} naar maximum" if rule["to_max_share"] else ""
            print(f"{rule['map'][:60]:60} {rule['profile']:18} {rule['median_change']:+7.1%} "
                  f"(meestal {low:+.0%} tot {high:+.0%}{extra}; {rule['examples']} voorbeelden, "
                  f"{len(rule['ecu_families'])} ECU-families)")
        return 0
    if args.command == "packages":
        print(json.dumps({"active": store.active_id(), "packages": store.list_packages()}, indent=2))
        return 0
    if args.command == "rollback":
        print(f"Actief pakket: {store.rollback()}")
        return 0
    if args.command == "install-package":
        package = store.install_file(args.file)
        print(f"Pakket {package.package_id} geïnstalleerd en actief.")
        return 0
    if args.command == "export-package":
        package = store.load_active()
        if package is None:
            print("Geen actief pakket.")
            return 2
        write_package(package, args.file)
        print(f"Pakket {package.package_id} geschreven naar {args.file}")
        return 0
    if args.command == "contract":
        store.set_contract(
            args.family_key,
            "FLASH_TOOL_CORRECTS_ON_WRITE" if args.flash_tool_corrects else "UNKNOWN",
            args.evidence,
        )
        print("Contract vastgelegd.")
        return 0
    return 1


def _evidence_command(args, store: PackageStore) -> int:
    import hashlib

    from .evidence import EvidenceGraph

    if args.command == "fingerprint-index":
        from .ecu_fingerprint import FingerprintIndex

        index = FingerprintIndex(args.index or store.root / "fingerprints.sqlite")
        try:
            print(json.dumps(index.index_tree(args.folder, progress=print), indent=2))
        finally:
            index.close()
        return 0
    graph = EvidenceGraph(store.root)
    try:
        if args.command == "review":
            row = graph.review(args.relation_id, args.decision, args.reason, args.reviewer)
            print(json.dumps({"relation": row, "history": graph.history(args.relation_id)}, indent=2,
                             ensure_ascii=False))
            return 0
        if args.command == "relations":
            subject = None
            if args.file:
                subject = "file:" + hashlib.sha256(args.file.read_bytes()).hexdigest()
                graph.mark_stale(f"path:{args.file.resolve()}", subject[5:])
            rows = graph.relations(subject)
            print(json.dumps([dict(r, history=graph.history(r["id"])) for r in rows], indent=2, ensure_ascii=False))
            return 0
        package = store.load_active()
        if package is None:
            print("Geen actief modelpakket.")
            return 2
        if args.command == "evaluate-matching":
            from .explain import evaluate

            print(json.dumps(evaluate(package, store.root, limit=args.limit), indent=2))
            return 0
        from .explain import explain, format_report

        data = args.original.read_bytes()
        report = explain(data, package, store_root=store.root, hint=args.original.name, profile=args.profile,
                         top=args.top, check=args.check, graph=graph)
        if args.filter:
            report["candidates"] = [c for c in report["candidates"] if c["category"] == args.filter]
        if args.propose and report["candidates"]:
            best = report["candidates"][0]
            kind = {"exact": "exact_content" if best["identity_status"] == "IDENTICAL_CONTENT" else "same_family",
                    "alternative_revision": "related_revision", "insufficient": "same_family",
                    "conflict": "conflict"}[best["category"]]
            evidence = [{"kind": "explain_report", "detail": best["reason"],
                         "source_sha256": report["input"]["sha256"]}]
            if best["identity_status"] == "IDENTICAL_CONTENT":
                evidence.append({"kind": "sha256_identical", "detail": "SHA-256 gelijk",
                                 "source_sha256": report["input"]["sha256"]})
            if best["compatibility_status"] == "TRANSFER_CHECK_IDENTICAL_BYTES":
                evidence.append({"kind": "transfer_dry_run_identical_bytes", "detail": best["compatibility"]["detail"],
                                 "source_sha256": report["input"]["sha256"]})
            report["proposed_relation"] = graph.propose(
                kind, f"file:{report['input']['sha256']}", f"file:{best['reference']['original_sha256']}",
                evidence=evidence, source_sha256=report["input"]["sha256"], algorithm=report["engine"],
                original_values={"input_identifiers": report["input"].get("identifiers", []),
                                 "path": str(args.original)})
        print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else format_report(report))
        return 0
    finally:
        graph.close()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
