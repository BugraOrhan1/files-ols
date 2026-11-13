"""Dutch desktop app: Open origineel -> Kies tuning -> Maak getunde BIN.

Library management and diagnostics live on a separate window. The app runs
fully offline against the locally installed package; WinOLS and the Dell
shares are only needed when rebuilding the library.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from . import SOFTWARE_VERSION
from .export import ExportBlocked, export_result
from .generate import GenerationRejected, generate
from .package import PROFILE_LABELS, PackageStore

DEFAULT_SOURCES = [
    "Database 1=\\\\Dell\\d\\Database 1",
    "Database 2=\\\\Dell\\d\\Database 2",
    "Database 3=\\\\Dell\\d\\Database 3",
    "Database 4=\\\\Dell\\d\\Database 4",
    "Default=\\\\Dell\\d\\Default",
]


class Settings:
    def __init__(self, store: PackageStore) -> None:
        self.path = store.root / "settings.json"
        self.data = {
            "output_dir": str(Path.home() / "Documents" / "OLS Tuner uitvoer"),
            "sources": DEFAULT_SOURCES,
            "py_ols": "",
            "exports": "",
        }
        try:
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class TunerApp:
    def __init__(self, root, store: PackageStore | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.store = store or PackageStore()
        self.settings = Settings(self.store)
        self.package = None
        self.original: Path | None = None
        self.profile_var = tk.StringVar(value="Stage 1")
        self.status_var = tk.StringVar()
        self.file_var = tk.StringVar(value="Nog geen origineel gekozen.")
        self.package_var = tk.StringVar()
        root.title(f"OLS Tuner {SOFTWARE_VERSION}")
        root.geometry("720x500")
        root.minsize(600, 460)

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="OLS Tuner", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.package_var, foreground="#555", wraplength=640).pack(
            anchor="w", pady=(0, 12))

        row1 = ttk.Frame(frame)
        row1.pack(fill="x", pady=4)
        ttk.Button(row1, text="1. Open origineel", width=22, command=self.choose_original).pack(side="left")
        ttk.Label(row1, textvariable=self.file_var, wraplength=420).pack(side="left", padx=10)

        row2 = ttk.Frame(frame)
        row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="2. Kies tuning", width=22).pack(side="left")
        self.profile_box = ttk.Combobox(row2, textvariable=self.profile_var, state="readonly",
                                        values=["Stage 1"], width=20)
        self.profile_box.pack(side="left", padx=10)

        row3 = ttk.Frame(frame)
        row3.pack(fill="x", pady=12)
        self.make_button = ttk.Button(row3, text="3. Maak getunde BIN", width=22, command=self.make_bin)
        self.make_button.pack(side="left")
        ttk.Button(row3, text="Open uitvoermap", command=self.open_output).pack(side="left", padx=10)

        self.status = tk.Text(frame, height=9, wrap="word", relief="flat", background="#f6f6f6")
        self.status.pack(fill="both", expand=True, pady=(8, 8))
        self.status.configure(state="disabled")

        bottom = ttk.Frame(frame)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Bibliotheek en diagnose…", command=self.open_library).pack(side="right")
        self.reload_package()

    # ----- helpers ----------------------------------------------------------
    def say(self, text: str, error: bool = False) -> None:
        self.status.configure(state="normal")
        self.status.delete("1.0", "end")
        self.status.insert("end", text)
        self.status.configure(state="disabled", foreground="#a40000" if error else "#000000")

    def reload_package(self) -> None:
        try:
            self.package = self.store.load_active()
        except (OSError, ValueError) as exc:
            self.package = None
            self.say(f"Het actieve modelpakket kon niet worden geladen: {exc}", error=True)
        if self.package is None:
            self.package_var.set("Geen modelpakket actief.")
            self.say(
                "Eerste start: er is nog geen modelpakket.\n\n"
                "Open 'Bibliotheek en diagnose…' en kies 'Pakket aankoppelen…' voor een eigen "
                "gevalideerd pakket, of 'Bibliotheek bijwerken' om het vanuit uw WinOLS-projecten op te bouwen."
            )
            return
        families = len(self.package["families"])
        variants = sum(1 for _ in self.package.variants())
        pairs = len(self.package["training_selection"])
        self.package_var.set(
            f"Modelpakket {self.package.package_id} ({self.package['created_at'][:10]}) – "
            f"{families} ECU-families, {variants} softwarevarianten, {pairs} Stage 1-paren"
        )
        profiles = sorted({PROFILE_LABELS.get(p, p) for _f, v in self.package.variants() for p in v["profiles"]})
        self.profile_box.configure(values=profiles or ["Stage 1"])

    def _profile_key(self) -> str:
        reverse = {v: k for k, v in PROFILE_LABELS.items()}
        return reverse.get(self.profile_var.get(), self.profile_var.get())

    # ----- actions ----------------------------------------------------------
    def choose_original(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title="Open origineel (AutoTuner-uitlezing)",
            filetypes=[("Uitleesbestanden", "*.bin *.ori *.BIN *.ORI"), ("Alle bestanden", "*.*")],
        )
        if path:
            self.set_original(Path(path))

    def set_original(self, path: Path) -> None:
        self.original = path
        size = path.stat().st_size
        self.file_var.set(f"{path.name} ({size:,} bytes)".replace(",", "."))
        self.say("Origineel gekozen. Kies de tuning en klik op 'Maak getunde BIN'.")

    def make_bin(self, ask_path: bool = True) -> Path | None:
        if self.package is None:
            self.say("Er is geen modelpakket actief. Koppel eerst een pakket aan.", error=True)
            return None
        if self.original is None:
            self.say("Open eerst een origineel bestand.", error=True)
            return None
        try:
            data = self.original.read_bytes()
            result = generate(data, self._profile_key(), self.package, self.store.contracts())
        except GenerationRejected as exc:
            self.say(f"Geen BIN gemaakt.\n\nReden: {exc.message}", error=True)
            return None
        except OSError as exc:
            self.say(f"Het bestand kon niet gelezen worden: {exc}", error=True)
            return None
        output_dir = Path(self.settings.data["output_dir"])
        name = f"{self.original.stem}_{self.profile_var.get().replace(' ', '')}_{result.output_sha256[:8]}.bin"
        if ask_path:
            from tkinter import filedialog

            chosen = filedialog.asksaveasfilename(
                title="Getunde BIN opslaan", initialdir=str(output_dir), initialfile=name,
                defaultextension=".bin", confirmoverwrite=False,
            )
            if not chosen:
                self.say("Opslaan geannuleerd; er is niets geschreven.")
                return None
            output_dir, name = Path(chosen).parent, Path(chosen).name
            self.settings.data["output_dir"] = str(output_dir)
            self.settings.save()
        try:
            final, report = export_result(result, self.original, output_dir, name, self.package)
        except ExportBlocked as exc:
            self.say(f"Geen BIN opgeslagen.\n\nReden: {exc.message}", error=True)
            return None
        if result.evidence_level == "KNOWN_PAIR":
            changed = f"{len(result.changes)} gewijzigde gebieden, exact volgens het trainingsproject"
        else:
            maps = sum(1 for c in result.changes if c["kind"] == "calibration")
            changed = f"{maps} kaarten en {len(result.changes) - maps} overige geleerde gebieden"
        notes = ("\n".join(result.notes) + "\n") if result.notes else ""
        self.say(
            notes +
            f"Klaar: {final.name}\n"
            f"Modelpakket {result.package_id}, familie {result.identity['family']}.\n"
            f"Aangepast: {changed}.\n"
            f"Controle: uitvoer opnieuw ingelezen, 0 wijzigingen buiten toegestane gebieden.\n"
            f"Bewijsniveau: {result.evidence_level}.\n"
            f"Resterende stap: {result.integrity['remaining_step']}\n"
            f"Rapport: {report.name}"
        )
        return final

    def open_output(self) -> None:
        open_folder(Path(self.settings.data["output_dir"]))

    def open_library(self) -> "LibraryWindow":
        return LibraryWindow(self)


class LibraryWindow:
    def __init__(self, app: TunerApp) -> None:
        tk, ttk = app.tk, app.ttk
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("Bibliotheek en diagnose")
        self.win.geometry("760x560")
        self.queue: queue.Queue = queue.Queue()
        frame = ttk.Frame(self.win, padding=12)
        frame.pack(fill="both", expand=True)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Pakket aankoppelen…", command=self.attach).pack(side="left")
        ttk.Button(buttons, text="Bibliotheek bijwerken", command=self.update_library).pack(side="left", padx=6)
        ttk.Button(buttons, text="Vorige versie herstellen", command=self.rollback).pack(side="left")
        ttk.Button(buttons, text="Pakket exporteren…", command=self.export_package).pack(side="left", padx=6)

        ttk.Label(frame, text="Bronmappen (NAAM=MAP per regel), alleen lezen:").pack(anchor="w", pady=(10, 0))
        self.sources = tk.Text(frame, height=5)
        self.sources.insert("1.0", "\n".join(app.settings.data["sources"]))
        self.sources.pack(fill="x")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="py-ols (optioneel):").pack(side="left")
        self.py_ols = tk.StringVar(value=app.settings.data["py_ols"])
        ttk.Entry(row, textvariable=self.py_ols, width=40).pack(side="left", padx=4)
        ttk.Label(row, text="WinOLS-exports:").pack(side="left")
        self.exports = tk.StringVar(value=app.settings.data["exports"])
        ttk.Entry(row, textvariable=self.exports, width=24).pack(side="left", padx=4)

        contract = ttk.LabelFrame(frame, text="Integriteit (checksum) per familie", padding=6)
        contract.pack(fill="x", pady=6)
        self.family = tk.StringVar()
        families = [f["family_key"] for f in (app.package or {}).get("families", [])]
        ttk.Label(contract, text="Familie:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(contract, textvariable=self.family, values=families, state="readonly",
                     width=32).grid(row=0, column=1, sticky="w", padx=4)
        self.corrects = tk.BooleanVar()
        ttk.Checkbutton(contract, text="AutoTuner corrigeert de checksum bij schrijven (door mij gecontroleerd)",
                        variable=self.corrects).grid(row=0, column=2, sticky="w", padx=6)
        ttk.Label(contract, text="Onderbouwing:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.evidence = tk.StringVar()
        ttk.Entry(contract, textvariable=self.evidence, width=60).grid(
            row=1, column=1, columnspan=2, sticky="we", padx=4, pady=(4, 0))
        ttk.Button(contract, text="Vastleggen", command=self.save_contract).grid(
            row=1, column=3, padx=4, pady=(4, 0))

        self.log = tk.Text(frame, height=14, wrap="word")
        self.log.pack(fill="both", expand=True)
        self.show_info()
        self.win.after(200, self.poll)

    def write(self, text: str) -> None:
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def show_info(self) -> None:
        package = self.app.package
        self.log.delete("1.0", "end")
        self.write(f"Lokale opslag: {self.app.store.root}")
        if not package:
            self.write("Geen actief pakket.")
            return
        self.write(f"Actief pakket {package.package_id} – data {package['data_version'][:12]} – "
                   f"software {package['software_version']}")
        contracts = self.app.store.contracts()
        for family in package["families"][:200]:
            c = contracts.get(family["family_key"], {}).get("contract", "UNKNOWN")
            self.write(f"\nFamilie {family['family_key']} – {len(family['variants'])} variant(en); "
                       f"integriteit: referentie {', '.join(family['integrity']['reference_checksum_states'])}, "
                       f"contract {c}")
            for variant in family["variants"][:20]:
                st = variant["profiles"].get("stage1", {})
                self.write(f"  {variant['variant_key'][:90]}: {len(variant['projects'])} project(en), "
                           f"{len(variant['pairs'])} paren, {st.get('included_regions', 0)} gebieden, "
                           f"bewijs {variant['evidence_level']}")
        if len(package["families"]) > 200:
            self.write(f"… en nog {len(package['families']) - 200} families")
        self.write("\nPakketten: " + ", ".join(p.get("package_id", "?") for p in self.app.store.list_packages()))

    def attach(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(title="Modelpakket aankoppelen",
                                          filetypes=[("Modelpakket", "*.olstune"), ("Alle", "*.*")])
        if not path:
            return
        try:
            package = self.app.store.install_file(Path(path))
        except (OSError, ValueError) as exc:
            self.write(f"Aankoppelen mislukt: {exc}")
            return
        self.app.reload_package()
        self.write(f"Pakket {package.package_id} aangekoppeld en actief.")
        self.show_info()

    def rollback(self) -> None:
        try:
            previous = self.app.store.rollback()
        except ValueError as exc:
            self.write(str(exc))
            return
        self.app.reload_package()
        self.show_info()
        self.write(f"Teruggezet naar pakket {previous}.")

    def export_package(self) -> None:
        from tkinter import filedialog

        if not self.app.package:
            self.write("Geen actief pakket.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".olstune",
                                            initialfile=f"{self.app.package.package_id}.olstune")
        if path:
            from .package import write_package

            write_package(self.app.package, Path(path))
            self.write(f"Pakket geschreven naar {path} (bevat klantgegevens: lokaal houden).")

    def save_contract(self) -> None:
        if not self.family.get():
            self.write("Kies eerst een familie.")
            return
        try:
            self.app.store.set_contract(
                self.family.get(),
                "FLASH_TOOL_CORRECTS_ON_WRITE" if self.corrects.get() else "UNKNOWN",
                self.evidence.get(),
            )
        except ValueError as exc:
            self.write(str(exc))
            return
        self.show_info()

    def update_library(self) -> None:
        from .__main__ import _sources
        from .library import Library

        lines = [line.strip() for line in self.sources.get("1.0", "end").splitlines() if line.strip()]
        self.app.settings.data.update({"sources": lines, "py_ols": self.py_ols.get(),
                                       "exports": self.exports.get()})
        self.app.settings.save()
        store = self.app.store

        def work():
            try:
                from .__main__ import _reader

                reader = _reader(self.py_ols.get() or None)
                library = Library(store)
                try:
                    summary = library.scan(
                        _sources(lines), reader,
                        export_dirs=[Path(self.exports.get())] if self.exports.get() else [],
                        progress=lambda m: self.queue.put(m),
                    )
                    for key, stats in summary["sources"].items():
                        self.queue.put(
                            f"{key}: {stats['status']} – gevonden {stats['found']}, gehasht {stats['hashed']}, "
                            f"geparseerd {stats['parsed']}, bevestigde paren {stats['confirmed_pairs']}, "
                            f"opnieuw proberen {stats['retry_pending']}")
                    training = library.train(progress=lambda m: self.queue.put(m))
                    self.queue.put(f"Nieuw pakket {training['package_id']} met {training['pairs_used']} paren "
                                   f"(+{len(training['pairs_added_since_previous'])} t.o.v. vorige).")
                finally:
                    library.close()
            except Exception as exc:  # shown to the user, app keeps running
                self.queue.put(f"Bijwerken mislukt: {exc}")
            self.queue.put("__done__")

        self.write("Bibliotheek bijwerken gestart…")
        threading.Thread(target=work, daemon=True).start()

    def poll(self) -> None:
        try:
            while True:
                message = self.queue.get_nowait()
                if message == "__done__":
                    self.app.reload_package()
                    self.show_info()
                else:
                    self.write(message)
        except queue.Empty:
            pass
        if self.win.winfo_exists():
            self.win.after(300, self.poll)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.stdout is None or sys.stderr is None:
        # Windowed EXE has no console: send text output to OLS_TUNER_LOG.
        target = os.environ.get("OLS_TUNER_LOG") or os.devnull
        stream = open(target, "a", encoding="utf-8")
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    if argv and argv[0] == "--selftest":
        from .selftest import run

        result = run()
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1
    if argv and argv[0] in ("train", "generate", "packages", "rollback", "install-package",
                            "export-package", "contract", "--store"):
        from .__main__ import main as cli_main

        return cli_main(argv)
    import tkinter as tk

    root = tk.Tk()
    app = TunerApp(root)
    if argv and argv[0] == "--gui-smoke":
        # Build the window, open the library screen, then close: used in CI.
        app.open_library()
        root.after(1500, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
