"""OLS Tuner desktop app (Dutch).

Main window: three clear steps – choose original, choose tuning, make tuned
BIN – with an immediate recognition check as soon as a file is opened.
The "Bibliotheek" window holds library management on tabs: source folders
(add with a folder picker), overview, packages and checksum contracts.
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
from .generate import GenerationRejected, GenerationResult, generate
from .package import PROFILE_LABELS, PackageStore, write_package

DEFAULT_SOURCES = [
    "Database 1=\\\\Dell\\d\\Database 1",
    "Database 2=\\\\Dell\\d\\Database 2",
    "Database 3=\\\\Dell\\d\\Database 3",
    "Database 4=\\\\Dell\\d\\Database 4",
    "Default=\\\\Dell\\d\\Default",
]

COLORS = {
    "info": ("#eef3fb", "#1a3d7c"),
    "ok": ("#e8f5e9", "#1b5e20"),
    "warn": ("#fff6dd", "#7a4f00"),
    "error": ("#fdecea", "#8b1a10"),
}

EVIDENCE_LABELS = {
    "KNOWN_PAIR": "Bekend origineel – exact de Stage 1 uit uw database",
    "MULTI_PROJECT": "Geleerde Stage 1 (meerdere projecten van deze software)",
    "REPRODUCTION_ONLY": "Geleerde Stage 1 (slechts één project van deze software – extra controle aanbevolen)",
}


def split_source(entry: str) -> tuple[str, str]:
    if "=" in entry:
        name, path = entry.split("=", 1)
        return name.strip(), path.strip()
    return Path(entry).name or entry, entry


class Settings:
    def __init__(self, store: PackageStore) -> None:
        self.path = store.root / "settings.json"
        self.data = {
            "output_dir": str(Path.home() / "Documents" / "OLS Tuner uitvoer"),
            "sources": list(DEFAULT_SOURCES),
            "py_ols": "",
            "exports": "",
            "source_stats": {},
        }
        try:
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


def open_path(path: Path) -> None:
    if path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _style(root) -> None:
    from tkinter import ttk

    style = ttk.Style(root)
    if sys.platform == "win32" and "vista" in style.theme_names():
        style.theme_use("vista")
    elif "clam" in style.theme_names():
        style.theme_use("clam")
    base = ("Segoe UI", 10)
    style.configure(".", font=base)
    style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
    style.configure("Sub.TLabel", foreground="#5f6368")
    style.configure("Muted.TLabel", foreground="#5f6368", font=("Segoe UI", 9))
    style.configure("Big.TLabel", font=("Segoe UI", 11, "bold"))
    style.configure("Primary.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 8))
    style.configure("Step.TLabelframe.Label", font=("Segoe UI", 11, "bold"))
    style.configure("Treeview", rowheight=24)
    style.configure("Horizontal.TProgressbar", background="#2e6fd8", troughcolor="#e6e8eb", thickness=14)


class MessagePanel:
    def __init__(self, parent) -> None:
        import tkinter as tk

        self.frame = tk.Frame(parent, bd=0, highlightthickness=0)
        self.title = tk.Label(self.frame, anchor="w", justify="left", font=("Segoe UI", 11, "bold"))
        self.body = tk.Label(self.frame, anchor="w", justify="left", font=("Segoe UI", 10), wraplength=640)
        self.title.pack(fill="x", padx=14, pady=(10, 0))
        self.body.pack(fill="x", padx=14, pady=(2, 10))
        self.text = ""

    def show(self, kind: str, title: str, body: str = "") -> None:
        bg, fg = COLORS[kind]
        self.frame.configure(bg=bg)
        self.title.configure(text=title, bg=bg, fg=fg)
        self.body.configure(text=body, bg=bg, fg=fg)
        self.text = f"{title}\n{body}"


class TunerApp:
    def __init__(self, root, store: PackageStore | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk, self.root = tk, ttk, root
        self.store = store or PackageStore()
        self.settings = Settings(self.store)
        self.package = None
        self.original: Path | None = None
        self.result: GenerationResult | None = None
        self.last_output: Path | None = None
        self.library_window: LibraryWindow | None = None

        root.title("OLS Tuner")
        root.geometry("760x600")
        root.minsize(680, 560)
        _style(root)

        outer = ttk.Frame(root, padding=(20, 16))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        left = ttk.Frame(header)
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="OLS Tuner", style="Title.TLabel").pack(anchor="w")
        self.package_var = tk.StringVar()
        ttk.Label(left, textvariable=self.package_var, style="Sub.TLabel", wraplength=520).pack(anchor="w")
        ttk.Button(header, text="Bibliotheek…", command=self.open_library).pack(side="right", anchor="n")

        # Step 1
        step1 = ttk.LabelFrame(outer, text="  1   Origineel  ", style="Step.TLabelframe", padding=12)
        step1.pack(fill="x", pady=(16, 8))
        ttk.Button(step1, text="Open origineel…", command=self.choose_original).pack(side="left")
        info = ttk.Frame(step1)
        info.pack(side="left", fill="x", expand=True, padx=14)
        self.file_var = tk.StringVar(value="Nog geen bestand gekozen")
        self.file_detail_var = tk.StringVar(value="Kies de onversleutelde uitlezing uit AutoTuner (.bin / .ori).")
        ttk.Label(info, textvariable=self.file_var, style="Big.TLabel").pack(anchor="w")
        ttk.Label(info, textvariable=self.file_detail_var, style="Muted.TLabel", wraplength=480).pack(anchor="w")

        # Step 2
        step2 = ttk.LabelFrame(outer, text="  2   Tuning  ", style="Step.TLabelframe", padding=12)
        step2.pack(fill="x", pady=8)
        self.profile_var = tk.StringVar(value="Stage 1")
        self.profile_box = ttk.Combobox(step2, textvariable=self.profile_var, state="readonly",
                                        values=["Stage 1"], width=24)
        self.profile_box.pack(side="left")
        self.profile_box.bind("<<ComboboxSelected>>", lambda _e: self.check_original())
        ttk.Label(step2, text="Alleen profielen waarvoor de bibliotheek trainingsdata heeft.",
                  style="Muted.TLabel").pack(side="left", padx=14)

        # Step 3
        step3 = ttk.LabelFrame(outer, text="  3   Getunde BIN  ", style="Step.TLabelframe", padding=12)
        step3.pack(fill="x", pady=8)
        self.make_button = ttk.Button(step3, text="Maak getunde BIN", style="Primary.TButton",
                                      command=self.make_bin, state="disabled")
        self.make_button.pack(side="left")
        ttk.Button(step3, text="Open uitvoermap", command=self.open_output).pack(side="left", padx=(12, 0))
        self.report_button = ttk.Button(step3, text="Open rapport", command=self.open_report, state="disabled")
        self.report_button.pack(side="left", padx=(8, 0))

        self.message = MessagePanel(outer)
        self.message.frame.pack(fill="x", pady=(12, 0))

        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x")
        ttk.Label(footer, text=f"Versie {SOFTWARE_VERSION}  ·  gegevens: {self.store.root}",
                  style="Muted.TLabel").pack(side="left")

        self.reload_package()

    # ----- state ------------------------------------------------------------
    @property
    def status_text(self) -> str:
        return self.message.text

    def reload_package(self) -> None:
        try:
            self.package = self.store.load_active()
        except (OSError, ValueError) as exc:
            self.package = None
            self.message.show("error", "Modelpakket kon niet worden geladen", str(exc))
            self.package_var.set("Geen bruikbaar modelpakket")
            return
        if self.package is None:
            self.package_var.set("Nog geen bibliotheek opgebouwd")
            self.message.show(
                "warn", "Welkom – eerst de bibliotheek opbouwen",
                "Klik rechtsboven op 'Bibliotheek…'. Voeg onder 'Bronmappen' uw mappen met WinOLS-projecten "
                "toe (de vijf Dell-mappen staan er al) en klik op 'Bibliotheek bijwerken'. "
                "Heeft u al een modelpakket? Koppel het dan aan onder 'Pakketten'.")
            self.make_button.configure(state="disabled")
            return
        families = len(self.package["families"])
        variants = sum(1 for _ in self.package.variants())
        pairs = len(self.package["training_selection"])
        self.package_var.set(
            f"Bibliotheek: {pairs} Stage 1-voorbeelden · {variants} softwarevarianten · "
            f"{families} ECU-families  (pakket {self.package.package_id}, {self.package['created_at'][:10]})")
        profiles = sorted({PROFILE_LABELS.get(p, p) for _f, v in self.package.variants() for p in v["profiles"]})
        self.profile_box.configure(values=profiles or ["Stage 1"])
        if self.original is None:
            self.message.show("info", "Klaar voor gebruik", "Open een origineel bestand om te beginnen.")
        else:
            self.check_original()

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
        self.file_var.set(path.name)
        self.file_detail_var.set(f"{size:,} bytes  ·  {path.parent}".replace(",", "."))
        self.check_original()

    def check_original(self) -> None:
        """Recognise the file immediately so the user knows before clicking."""

        self.result = None
        self.make_button.configure(state="disabled")
        if self.original is None:
            return
        if self.package is None:
            self.message.show("warn", "Nog geen bibliotheek", "Bouw eerst de bibliotheek op via 'Bibliotheek…'.")
            return
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            data = self.original.read_bytes()
            result = generate(data, self._profile_key(), self.package, self.store.contracts())
        except GenerationRejected as exc:
            self.message.show("error", "Dit bestand kan niet getuned worden", exc.message)
            return
        except OSError as exc:
            self.message.show("error", "Bestand kon niet gelezen worden", str(exc))
            return
        finally:
            self.root.configure(cursor="")
        self.result = result
        lines = [EVIDENCE_LABELS.get(result.evidence_level, result.evidence_level)]
        lines.extend(result.notes)
        lines.append(f"ECU: {result.identity['family']}  ·  {len(result.changes)} gebieden worden aangepast.")
        if not result.integrity["ready_for_use"]:
            lines.append("Let op: voor deze ECU-familie is nog niet vastgelegd hoe de checksum wordt "
                         "gecorrigeerd. Doe dat onder Bibliotheek… → Checksum.")
            self.message.show("warn", "Herkend – checksum nog niet vastgelegd", "\n".join(lines))
            return
        self.make_button.configure(state="normal")
        self.message.show("ok", "Herkend – klaar om te maken", "\n".join(lines))

    def make_bin(self, ask_path: bool = True) -> Path | None:
        if self.result is None or self.original is None:
            self.check_original()
            if self.result is None:
                return None
        result = self.result
        output_dir = Path(self.settings.data["output_dir"])
        name = f"{self.original.stem}_{self.profile_var.get().replace(' ', '')}_{result.output_sha256[:8]}.bin"
        if ask_path:
            from tkinter import filedialog

            chosen = filedialog.asksaveasfilename(
                title="Getunde BIN opslaan", initialdir=str(output_dir), initialfile=name,
                defaultextension=".bin", confirmoverwrite=False,
                filetypes=[("BIN-bestand", "*.bin"), ("Alle bestanden", "*.*")],
            )
            if not chosen:
                return None
            output_dir, name = Path(chosen).parent, Path(chosen).name
            self.settings.data["output_dir"] = str(output_dir)
            self.settings.save()
        try:
            final, report = export_result(result, self.original, output_dir, name, self.package)
        except ExportBlocked as exc:
            self.message.show("error", "Niet opgeslagen", exc.message)
            return None
        self.last_output = final
        self.report_button.configure(state="normal")
        self.message.show(
            "ok", f"Opgeslagen: {final.name}",
            f"Map: {final.parent}\n"
            f"Gecontroleerd: opnieuw ingelezen, 0 wijzigingen buiten de toegestane gebieden.\n"
            f"Resterende stap: {result.integrity['remaining_step']}\n"
            f"Rapport: {report.name}")
        return final

    def open_output(self) -> None:
        open_path(Path(self.settings.data["output_dir"]))

    def open_report(self) -> None:
        if self.last_output is not None:
            open_path(self.last_output.with_name(self.last_output.name + ".rapport.json"))

    def open_library(self, tab: str | None = None) -> "LibraryWindow":
        if self.library_window is not None and self.library_window.win.winfo_exists():
            self.library_window.win.lift()
        else:
            self.library_window = LibraryWindow(self)
        if tab:
            self.library_window.select(tab)
        return self.library_window


class LibraryWindow:
    TABS = ("Bronmappen", "Overzicht", "Pakketten", "Checksum", "Instellingen")

    def __init__(self, app: TunerApp) -> None:
        tk, ttk = app.tk, app.ttk
        self.app, self.tk, self.ttk = app, tk, ttk
        self.win = tk.Toplevel(app.root)
        self.win.title("OLS Tuner – Bibliotheek")
        self.win.geometry("900x620")
        self.win.minsize(780, 540)
        self.queue: queue.Queue = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.notebook = ttk.Notebook(self.win)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=12)
        self.tabs = {}
        for name in self.TABS:
            frame = ttk.Frame(self.notebook, padding=12)
            self.notebook.add(frame, text=f"  {name}  ")
            self.tabs[name] = frame
        self._build_sources(self.tabs["Bronmappen"])
        self._build_overview(self.tabs["Overzicht"])
        self._build_packages(self.tabs["Pakketten"])
        self._build_checksum(self.tabs["Checksum"])
        self._build_settings(self.tabs["Instellingen"])
        self.refresh()
        self.win.after(200, self.poll)

    def select(self, name: str) -> None:
        self.notebook.select(self.tabs[name])

    # ----- Bronmappen -------------------------------------------------------
    def _build_sources(self, tab) -> None:
        ttk, tk = self.ttk, self.tk
        ttk.Label(tab, text="Mappen met WinOLS-projecten (.ols). Ze worden alleen gelezen; "
                  "submappen worden meegenomen.", style="Sub.TLabel").pack(anchor="w")
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(8, 6))
        ttk.Button(buttons, text="+  Map toevoegen…", command=self.add_folder).pack(side="left")
        ttk.Button(buttons, text="+  Netwerkpad toevoegen…", command=self.add_network_path).pack(side="left", padx=6)
        ttk.Button(buttons, text="Verwijderen", command=self.remove_folder).pack(side="left")
        ttk.Button(buttons, text="Dell-mappen herstellen", command=self.restore_defaults).pack(side="right")
        self.source_tree = ttk.Treeview(tab, columns=("name", "path", "status"), show="headings", height=6)
        for col, label, width in (("name", "Naam", 140), ("path", "Map", 400), ("status", "Laatste resultaat", 280)):
            self.source_tree.heading(col, text=label, anchor="w")
            self.source_tree.column(col, width=width, anchor="w")
        self.source_tree.pack(fill="x")

        run = ttk.LabelFrame(tab, text="  Bibliotheek bijwerken  ", style="Step.TLabelframe", padding=10)
        run.pack(fill="both", expand=True, pady=(12, 0))
        controls = ttk.Frame(run)
        controls.pack(fill="x")
        self.run_button = ttk.Button(controls, text="Bibliotheek bijwerken", style="Primary.TButton",
                                     command=self.update_library)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stoppen", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Label(controls, text="Onderbreken mag: een volgende keer gaat hij verder waar hij was.",
                  style="Muted.TLabel").pack(side="left", padx=8)
        self.progress_var = tk.StringVar(value="")
        self.progress = ttk.Progressbar(run, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(10, 2))
        ttk.Label(run, textvariable=self.progress_var, style="Muted.TLabel").pack(anchor="w")
        log_frame = ttk.Frame(run)
        log_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.log = tk.Text(log_frame, height=10, wrap="word", font=("Consolas", 9), relief="flat",
                           background="#f7f7f8")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")


    def _browse_into(self, var) -> None:
        from tkinter import filedialog

        path = filedialog.askdirectory(title="Kies map")
        if path:
            var.set(path)

    def _sources(self) -> list[str]:
        return list(self.app.settings.data.get("sources", []))

    def _save_sources(self, sources: list[str]) -> None:
        self.app.settings.data["sources"] = sources
        self.app.settings.save()
        self.refresh_sources()

    def _add_source(self, path: str) -> None:
        path = path.strip().rstrip("\\/") or path.strip()
        if not path:
            return
        sources = self._sources()
        if any(split_source(s)[1].lower() == path.lower() for s in sources):
            self.write(f"Map staat al in de lijst: {path}")
            return
        name = Path(path).name or path
        names = {split_source(s)[0] for s in sources}
        base, n = name, 2
        while name in names:
            name, n = f"{base} ({n})", n + 1
        sources.append(f"{name}={path}")
        self._save_sources(sources)
        self.write(f"Map toegevoegd: {path}")

    def add_folder(self) -> None:
        from tkinter import filedialog

        path = filedialog.askdirectory(title="Map met WinOLS-projecten toevoegen", mustexist=True)
        if path:
            self._add_source(str(Path(path)))

    def add_network_path(self) -> None:
        from tkinter import simpledialog

        path = simpledialog.askstring("Netwerkpad toevoegen", "Netwerkpad, bijvoorbeeld \\\\Dell\\d\\Database 1:",
                                      parent=self.win)
        if path:
            self._add_source(path)

    def remove_folder(self) -> None:
        selected = set(self.source_tree.selection())
        if not selected:
            self.write("Selecteer eerst een map om te verwijderen.")
            return
        self._save_sources([s for i, s in enumerate(self._sources()) if f"src{i}" not in selected])

    def restore_defaults(self) -> None:
        sources = self._sources()
        for entry in DEFAULT_SOURCES:
            if entry not in sources:
                sources.append(entry)
        self._save_sources(sources)

    def refresh_sources(self) -> None:
        self.source_tree.delete(*self.source_tree.get_children())
        stats = self.app.settings.data.get("source_stats", {})
        for i, entry in enumerate(self._sources()):
            name, path = split_source(entry)
            self.source_tree.insert("", "end", iid=f"src{i}", values=(name, path, stats.get(name, "nog niet gelezen")))

    def write(self, text: str) -> None:
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def update_library(self) -> None:
        from .__main__ import _reader, _sources
        from .library import Library

        lines = self._sources()
        if not lines:
            self.write("Voeg eerst minstens één map toe.")
            return
        self.app.settings.data.update({"exports": self.exports_var.get(), "py_ols": self.py_ols_var.get()})
        self.app.settings.save()
        store = self.app.store
        exports = [Path(self.exports_var.get())] if self.exports_var.get() else []
        py_ols = self.py_ols_var.get() or None
        self.stop_event = threading.Event()
        stop = self.stop_event
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.configure(value=0)
        self.progress_var.set("Bezig met starten…")

        def work():
            try:
                library = Library(store)
                try:
                    summary = library.scan(
                        _sources(lines), _reader(py_ols), export_dirs=exports, stop=stop,
                        progress=lambda m: self.queue.put(("log", m)),
                        on_file=lambda src, done, found: self.queue.put(("progress", src, done, found)),
                    )
                    self.queue.put(("scan", summary))
                    if stop.is_set():
                        self.queue.put(("log", "Gestopt. Wat al gelezen is, blijft bewaard."))
                    else:
                        self.queue.put(("log", "Leren uit de gevonden voorbeelden…"))
                        training = library.train(progress=lambda m: self.queue.put(("log", m)))
                        self.queue.put(("trained", training))
                finally:
                    library.close()
            except Exception as exc:  # shown to the user; the app keeps running
                self.queue.put(("error", str(exc)))
            self.queue.put(("done",))

        threading.Thread(target=work, daemon=True).start()

    def stop(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
            self.progress_var.set("Stoppen na het huidige project…")

    def poll(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.write(item[1])
                elif kind == "progress":
                    _, source, done, found = item
                    self.progress.configure(value=(done / found * 100) if found else 100)
                    self.progress_var.set(f"{source}: {done} van {found} projecten gelezen")
                elif kind == "scan":
                    stats = self.app.settings.data.setdefault("source_stats", {})
                    for key, st in item[1]["sources"].items():
                        if st["status"].startswith("unavailable"):
                            text = "niet bereikbaar"
                        else:
                            text = (f"{st['found']} projecten, {st.get('stage1_pairs', 0)} Stage 1-voorbeelden"
                                    + (f", {st['retry_pending']} opnieuw proberen" if st["retry_pending"] else ""))
                        stats[key] = text
                        self.write(f"{key}: {text}")
                    self.app.settings.save()
                    self.refresh_sources()
                elif kind == "trained":
                    t = item[1]
                    self.write(f"Nieuw pakket {t['package_id']}: {t['pairs_used']} voorbeelden "
                               f"(+{len(t['pairs_added_since_previous'])} nieuw).")
                elif kind == "error":
                    self.write(f"Fout: {item[1]}")
                    self.progress_var.set("Mislukt – zie melding hierboven.")
                elif kind == "done":
                    self.run_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    if not self.progress_var.get().startswith("Mislukt"):
                        self.progress.configure(value=100)
                        self.progress_var.set("Klaar.")
                    self.app.reload_package()
                    self.refresh()
        except queue.Empty:
            pass
        if self.win.winfo_exists():
            self.win.after(250, self.poll)

    # ----- Overzicht --------------------------------------------------------
    def _build_overview(self, tab) -> None:
        ttk, tk = self.ttk, self.tk
        self.summary_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.summary_var, style="Big.TLabel").pack(anchor="w")
        ttk.Label(tab, text="Per ECU-familie en softwarevariant: waarvoor de app een Stage 1 kan maken.",
                  style="Sub.TLabel").pack(anchor="w", pady=(0, 8))
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        cols = (("projects", "Projecten", 80), ("pairs", "Voorbeelden", 100),
                ("evidence", "Bewijs", 150), ("checksum", "Checksum", 170))
        self.overview = ttk.Treeview(frame, columns=[c[0] for c in cols], show="tree headings")
        self.overview.heading("#0", text="ECU-familie / softwarevariant")
        self.overview.column("#0", width=300)
        for col, label, width in cols:
            self.overview.heading(col, text=label)
            self.overview.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, command=self.overview.yview)
        self.overview.configure(yscrollcommand=scroll.set)
        self.overview.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

    def refresh_overview(self) -> None:
        self.overview.delete(*self.overview.get_children())
        package = self.app.package
        if package is None:
            self.summary_var.set("Nog geen bibliotheek.")
            return
        contracts = self.app.store.contracts()
        variants = sum(1 for _ in package.variants())
        self.summary_var.set(f"{len(package['training_selection'])} Stage 1-voorbeelden · {variants} "
                             f"softwarevarianten · {len(package['families'])} ECU-families")
        evidence = {"REPRODUCTION_ONLY": "1 project", "MULTI_PROJECT": "meerdere projecten"}
        for family in package["families"]:
            contract = contracts.get(family["family_key"], {}).get("contract", "UNKNOWN")
            check = "door AutoTuner (vastgelegd)" if contract == "FLASH_TOOL_CORRECTS_ON_WRITE" else "niet vastgelegd"
            projects = len({p for v in family["variants"] for p in v["projects"]})
            pairs = sum(len(v["pairs"]) for v in family["variants"])
            fam_id = self.overview.insert(
                "", "end", text=f"{family['ecu_type'] or family['family_key']}  ({family['size']:,} bytes)".replace(",", "."),
                values=(projects, pairs, "", check), open=False)
            for variant in family["variants"]:
                label = variant["variant_key"].replace("|", "  ·  ")
                self.overview.insert(fam_id, "end", text=label[:120], values=(
                    len(variant["projects"]), len(variant["pairs"]),
                    evidence.get(variant["evidence_level"], variant["evidence_level"]), ""))

    # ----- Pakketten --------------------------------------------------------
    def _build_packages(self, tab) -> None:
        ttk = self.ttk
        ttk.Label(tab, text="Elke keer bijwerken maakt een nieuwe versie; oudere versies blijven bewaard.",
                  style="Sub.TLabel").pack(anchor="w", pady=(0, 8))
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        cols = (("active", "", 60), ("id", "Pakket", 160), ("date", "Gemaakt", 180),
                ("variants", "Varianten", 90), ("pairs", "Voorbeelden", 100))
        self.package_tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", height=10)
        for col, label, width in cols:
            self.package_tree.heading(col, text=label)
            self.package_tree.column(col, width=width, anchor="w")
        self.package_tree.pack(side="left", fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(side="left", fill="y", padx=(10, 0))
        ttk.Button(buttons, text="Pakket aankoppelen…", command=self.attach).pack(fill="x")
        ttk.Button(buttons, text="Vorige versie herstellen", command=self.rollback).pack(fill="x", pady=4)
        ttk.Button(buttons, text="Pakket exporteren…", command=self.export_package).pack(fill="x")
        ttk.Label(tab, text="Pakketten bevatten gegevens uit klantbestanden: alleen lokaal bewaren.",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

    def refresh_packages(self) -> None:
        self.package_tree.delete(*self.package_tree.get_children())
        active = self.app.store.active_id()
        for item in reversed(self.app.store.list_packages()):
            if "error" in item:
                self.package_tree.insert("", "end", values=("", Path(item["path"]).name, item["error"][:60], "", ""))
                continue
            self.package_tree.insert("", "end", values=(
                "actief" if item["package_id"] == active else "", item["package_id"],
                item["created_at"].replace("T", " ")[:19], item["variants"], item["pairs"]))

    def attach(self) -> None:
        from tkinter import filedialog, messagebox

        path = filedialog.askopenfilename(title="Modelpakket aankoppelen",
                                          filetypes=[("Modelpakket", "*.olstune"), ("Alle bestanden", "*.*")])
        if not path:
            return
        try:
            package = self.app.store.install_file(Path(path))
        except (OSError, ValueError) as exc:
            messagebox.showerror("Aankoppelen mislukt", str(exc), parent=self.win)
            return
        self.app.reload_package()
        self.refresh()
        messagebox.showinfo("Pakket aangekoppeld", f"Pakket {package.package_id} is nu actief.", parent=self.win)

    def rollback(self) -> None:
        from tkinter import messagebox

        try:
            previous = self.app.store.rollback()
        except ValueError as exc:
            messagebox.showinfo("Geen vorige versie", str(exc), parent=self.win)
            return
        self.app.reload_package()
        self.refresh()
        messagebox.showinfo("Hersteld", f"Pakket {previous} is weer actief.", parent=self.win)

    def export_package(self) -> None:
        from tkinter import filedialog, messagebox

        if not self.app.package:
            messagebox.showinfo("Geen pakket", "Er is nog geen actief pakket.", parent=self.win)
            return
        path = filedialog.asksaveasfilename(defaultextension=".olstune",
                                            initialfile=f"{self.app.package.package_id}.olstune",
                                            filetypes=[("Modelpakket", "*.olstune")])
        if path:
            write_package(self.app.package, Path(path))
            messagebox.showinfo("Geëxporteerd", f"Pakket opgeslagen als {path}", parent=self.win)

    # ----- Checksum ---------------------------------------------------------
    def _build_checksum(self, tab) -> None:
        ttk, tk = self.ttk, self.tk
        ttk.Label(tab, text="De app wijzigt geen checksums. Leg per ECU-familie vast hoe de checksum wordt "
                  "gecorrigeerd; zonder dit maakt de app voor die familie geen bruikbare BIN.",
                  style="Sub.TLabel", wraplength=820).pack(anchor="w")
        form = ttk.Frame(tab)
        form.pack(fill="x", pady=10)
        ttk.Label(form, text="ECU-familie:").grid(row=0, column=0, sticky="w")
        self.family_var = tk.StringVar()
        self.family_box = ttk.Combobox(form, textvariable=self.family_var, state="readonly", width=42)
        self.family_box.grid(row=0, column=1, sticky="w", padx=6)
        self.corrects_var = tk.BooleanVar()
        ttk.Checkbutton(form, text="AutoTuner corrigeert de checksum bij het schrijven (door mij gecontroleerd)",
                        variable=self.corrects_var).grid(row=1, column=1, sticky="w", padx=6, pady=6)
        ttk.Label(form, text="Onderbouwing:").grid(row=2, column=0, sticky="w")
        self.evidence_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.evidence_var, width=70).grid(row=2, column=1, sticky="we", padx=6)
        ttk.Button(form, text="Vastleggen", command=self.save_contract).grid(row=3, column=1, sticky="w", padx=6, pady=8)
        self.contract_tree = ttk.Treeview(tab, columns=("family", "contract", "evidence", "date"),
                                          show="headings", height=8)
        for col, label, width in (("family", "ECU-familie", 220), ("contract", "Status", 200),
                                  ("evidence", "Onderbouwing", 280), ("date", "Vastgelegd", 150)):
            self.contract_tree.heading(col, text=label)
            self.contract_tree.column(col, width=width, anchor="w")
        self.contract_tree.pack(fill="both", expand=True)

    def refresh_checksum(self) -> None:
        families = [f["family_key"] for f in (self.app.package["families"] if self.app.package else [])]
        self.family_box.configure(values=families)
        self.contract_tree.delete(*self.contract_tree.get_children())
        for key, item in sorted(self.app.store.contracts().items()):
            status = "AutoTuner corrigeert" if item["contract"] == "FLASH_TOOL_CORRECTS_ON_WRITE" else "onbekend"
            self.contract_tree.insert("", "end", values=(key, status, item["evidence"], item["recorded_at"][:19]))

    def save_contract(self) -> None:
        from tkinter import messagebox

        if not self.family_var.get():
            messagebox.showinfo("Kies een familie", "Kies eerst een ECU-familie.", parent=self.win)
            return
        try:
            self.app.store.set_contract(
                self.family_var.get(),
                "FLASH_TOOL_CORRECTS_ON_WRITE" if self.corrects_var.get() else "UNKNOWN",
                self.evidence_var.get(),
            )
        except ValueError as exc:
            messagebox.showerror("Niet vastgelegd", str(exc), parent=self.win)
            return
        self.refresh()
        self.app.check_original()

    # ----- Instellingen -----------------------------------------------------
    def _build_settings(self, tab) -> None:
        ttk, tk = self.ttk, self.tk
        out = ttk.LabelFrame(tab, text="  Uitvoer  ", padding=8)
        out.pack(fill="x")
        self.output_var = tk.StringVar(value=self.app.settings.data["output_dir"])
        ttk.Label(out, text="Standaardmap voor getunde BIN's:").grid(row=0, column=0, sticky="w")
        ttk.Entry(out, textvariable=self.output_var, width=60).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(out, text="Kies…", command=lambda: self._browse_into(self.output_var)).grid(row=0, column=2)
        out.columnconfigure(1, weight=1)
        adv = ttk.LabelFrame(tab, text="  Geavanceerd (optioneel)  ", padding=8)
        adv.pack(fill="x", pady=(12, 0))
        self.exports_var = tk.StringVar(value=self.app.settings.data.get("exports", ""))
        self.py_ols_var = tk.StringVar(value=self.app.settings.data.get("py_ols", ""))
        for row, (label, var) in enumerate((("Map met WinOLS-exports (extra bewijs):", self.exports_var),
                                            ("py-ols i.p.v. ingebouwde lezer:", self.py_ols_var))):
            ttk.Label(adv, text=label).grid(row=row, column=0, sticky="w")
            ttk.Entry(adv, textvariable=var, width=60).grid(row=row, column=1, sticky="we", padx=6, pady=2)
            ttk.Button(adv, text="Kies…", command=lambda v=var: self._browse_into(v)).grid(row=row, column=2)
        adv.columnconfigure(1, weight=1)

        ttk.Button(tab, text="Opslaan", command=self.save_settings).pack(anchor="w", pady=12)
        ttk.Label(tab, text=f"Lokale gegevens (bibliotheek, pakketten): {self.app.store.root}",
                  style="Muted.TLabel").pack(anchor="w")
        ttk.Button(tab, text="Open gegevensmap", command=lambda: open_path(self.app.store.root)).pack(anchor="w", pady=4)

    def save_settings(self) -> None:
        self.app.settings.data.update({"output_dir": self.output_var.get(), "exports": self.exports_var.get(),
                                       "py_ols": self.py_ols_var.get()})
        self.app.settings.save()
        self.write("Instellingen opgeslagen.")

    def refresh(self) -> None:
        self.refresh_sources()
        self.refresh_overview()
        self.refresh_packages()
        self.refresh_checksum()


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
        # Build every screen, then close: used in CI.
        window = app.open_library()
        for name in LibraryWindow.TABS:
            window.select(name)
            root.update()
        root.after(1500, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
