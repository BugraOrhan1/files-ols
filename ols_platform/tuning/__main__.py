"""Command line for the tuning pipeline (same code as the app buttons).

Examples::

    python -m ols_platform.tuning train --py-ols C:\\tools\\py-ols ^
        --source "Database 1=\\\\Dell\\d\\Database 1" --exports D:\\exports
    python -m ols_platform.tuning generate origineel.bin --output-dir D:\\uit
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .export import ExportBlocked, export_result
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

    upd = sub.add_parser("update-library", help="Bibliotheek bijwerken met de mappen uit de app-instellingen "
                         "(zo start de app het achtergrondproces).")
    upd.add_argument("--workers", type=int)
    sub.add_parser("packages", help="Toon pakketten.")
    sub.add_parser("rollback", help="Activeer de vorige pakketversie.")
    inst = sub.add_parser("install-package")
    inst.add_argument("file", type=Path)
    exp = sub.add_parser("export-package")
    exp.add_argument("file", type=Path)
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
            result = generate(data, args.profile, package, store.contracts())
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
    if args.command == "update-library":
        from .background import run_update

        return run_update(store.root, workers=args.workers)
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


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
