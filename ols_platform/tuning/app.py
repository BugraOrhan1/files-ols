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

from .fileio import read_json, write_json
from . import SOFTWARE_VERSION
from .export import ExportBlocked, export_result
from .background import read_status, request_stop, start_background
from .diagnostics import app_logger, close_logger, create_bundle
from .practice import family_line, read_stats, summary_line
from .generate import GenerationRejected, GenerationResult, available_profiles, generate
from .ols_versions import profile_label
from .package import PackageStore, contract_for, write_package

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
    "KNOWN_PAIR": "Bekend origineel – exact de tuning uit uw database",
    "MULTI_PROJECT": "Geleerde tuning (meerdere projecten van deze software)",
    "REPRODUCTION_ONLY": "Geleerde tuning (slechts één project van deze software – extra controle aanbevolen)",
    "COMBINED": "Samengesteld uit losse tunes – controleer het resultaat",
    "DONOR_TRANSFER": "Overgezet van het meest gelijkende bestand uit uw database – controleer het resultaat",
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
            "min_similarity": 70,
            "min_coverage": 100,
        }
        stored = read_json(self.path, {})
        if isinstance(stored, dict):
            self.data.update(stored)

    def save(self) -> None:
        try:
            write_json(self.path, self.data, indent=2)
        except OSError:
            pass  # settings are retried on the next save


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
        self.body = tk.Label(self.frame, anchor="w", justify="left", font=("Segoe UI", 10), wraplength=700)
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
    def __init__(self, root, store: PackageStore | None = None, synchronous: bool = False) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk, self.root = tk, ttk, root
        self.store = store or PackageStore()
        self.settings = Settings(self.store)
        self.log = app_logger(self.store.root)
        self.log.info("App gestart (versie %s)", SOFTWARE_VERSION)
        root.report_callback_exception = self._on_error
        self.package = None
        self.original: Path | None = None
        self.result: GenerationResult | None = None
        self.last_output: Path | None = None
        self.library_window: LibraryWindow | None = None
        self.synchronous = synchronous  # tests: run recognition inline
        self._results: queue.Queue = queue.Queue()
        self._token = None
        self._closed = False
        self._loading = False
        self._after_ids: dict[str, str] = {}

        root.title("OLS Tuner")
        root.geometry("780x700")
        root.minsize(700, 640)
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
        self.bg_var = tk.StringVar()
        ttk.Label(left, textvariable=self.bg_var, foreground="#1a5fb4", wraplength=520).pack(anchor="w")
        ttk.Button(header, text="Bibliotheek…", command=self.open_library).pack(side="right", anchor="n")

        # Step 1
        step1 = ttk.LabelFrame(outer, text="  1   Origineel  ", style="Step.TLabelframe", padding=12)
        step1.pack(fill="x", pady=(16, 8))
        self.open_button = ttk.Button(step1, text="Open origineel…", command=self.choose_original)
        self.open_button.pack(side="left")
        info = ttk.Frame(step1)
        info.pack(side="left", fill="x", expand=True, padx=14)
        self.file_var = tk.StringVar(value="Nog geen bestand gekozen")
        self.file_detail_var = tk.StringVar(value="Kies de onversleutelde uitlezing uit AutoTuner (.bin / .ori).")
        ttk.Label(info, textvariable=self.file_var, style="Big.TLabel").pack(anchor="w")
        ttk.Label(info, textvariable=self.file_detail_var, style="Muted.TLabel", wraplength=480).pack(anchor="w")

        # Step 2
        step2 = ttk.LabelFrame(outer, text="  2   Tuning  ", style="Step.TLabelframe", padding=12)
        step2.pack(fill="x", pady=8)
        self.profile_var = tk.StringVar(value="")
        self.profile_map: dict[str, str] = {}
        self.profile_box = ttk.Combobox(step2, textvariable=self.profile_var, state="readonly",
                                        values=[], width=34)
        self.profile_box.pack(side="left")
        self.profile_box.bind("<<ComboboxSelected>>", lambda _e: self.check_original())
        ttk.Label(step2, text="De lijst toont welke tunings uw database voor deze software bevat.",
                  style="Muted.TLabel", wraplength=330).pack(side="left", padx=14)

        # Step 3
        step3 = ttk.LabelFrame(outer, text="  3   Getunde BIN  ", style="Step.TLabelframe", padding=12)
        step3.pack(fill="x", pady=8)
        self.make_button = ttk.Button(step3, text="Maak getunde BIN", style="Primary.TButton",
                                      command=self.make_bin, state="disabled")
        self.make_button.pack(side="left")
        ttk.Button(step3, text="Open uitvoermap", command=self.open_output).pack(side="left", padx=(12, 0))
        self.report_button = ttk.Button(step3, text="Open rapport", command=self.open_report, state="disabled")
        self.report_button.pack(side="left", padx=(8, 0))
        self.checksum_button = ttk.Button(step3, text="Checksum vastleggen…", command=self.confirm_checksum)
        self.current_family: str | None = None

        self.message = MessagePanel(outer)
        self.message.frame.pack(fill="x", pady=(12, 0))

        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x")
        ttk.Label(footer, text=f"Versie {SOFTWARE_VERSION}  ·  gegevens: {self.store.root}",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(footer, text="Diagnose opslaan…", command=self.save_diagnostics).pack(side="right")
        self.explain_button = ttk.Button(footer, text="Uitleg: welke referentie past?", command=self.show_explanation)
        self.explain_button.pack(side="right", padx=8)
        self.proposal_button = ttk.Button(footer, text="Kaartvoorstel…", command=self.make_proposal)
        self.proposal_button.pack(side="right")

        self.active_id = None
        self.reload_package()
        self._after_ids["poll"] = self.root.after(1500, self.poll_background)
        self._after_ids["drain"] = self.root.after(120, self._drain)

    # ----- state ------------------------------------------------------------
    @property
    def status_text(self) -> str:
        return self.message.text

    def reload_package(self) -> None:
        """Load the active library version off the GUI thread (it can be large)."""

        if self.synchronous:
            try:
                self._apply_package(self.store.load_active(), None)
            except (OSError, ValueError) as exc:
                self._apply_package(None, exc)
            return
        if self._loading:
            return
        self._loading = True
        self.package_var.set("Bibliotheek wordt geladen…")
        store = self.store

        def work() -> None:
            try:
                try:
                    store.prune()  # old versions from earlier runs can take GBs and slow everything down
                except OSError:
                    pass
                self._results.put((None, self._package_loaded, store.load_active(), None))
            except Exception as exc:
                self._results.put((None, self._package_loaded, None, exc))

        threading.Thread(target=work, daemon=True).start()

    def _package_loaded(self, package, error) -> None:
        self._loading = False
        self._apply_package(package, error)

    def _apply_package(self, package, exc) -> None:
        if exc is not None:
            self.package = None
            if "verouderd" in str(exc):
                self.message.show("warn", "Bibliotheek omzetten naar de nieuwe versie",
                                  "Deze versie van de app gebruikt een verbeterd bibliotheekformaat. Open "
                                  "'Bibliotheek…' en klik op 'Bibliotheek bijwerken'. Al gescande bestanden worden "
                                  "niet opnieuw gelezen; het omzetten gaat daardoor snel.")
            else:
                self.message.show("error", "Bibliotheek kon niet worden geladen", str(exc))
            self.package_var.set("Bibliotheek moet worden bijgewerkt")
            return
        self.package = package
        if self.package is None:
            self.package_var.set("Nog geen bibliotheek opgebouwd")
            self.message.show(
                "warn", "Welkom – eerst de bibliotheek opbouwen",
                "Klik rechtsboven op 'Bibliotheek…'. Voeg onder 'Bronmappen' uw mappen met WinOLS-projecten "
                "toe (de vijf Dell-mappen staan er al) en klik op 'Bibliotheek bijwerken'. Het bijwerken loopt "
                "op de achtergrond; zodra de eerste voorbeelden verwerkt zijn, kunt u al beginnen.")
            self.make_button.configure(state="disabled")
            return
        self.active_id = self.package.package_id
        families = len(self.package["families"])
        variants = sum(1 for _ in self.package.variants())
        pairs = len(self.package["training_selection"])
        profiles = len({p for _f, v in self.package.variants() for p in v["profiles"]})
        self.package_var.set(
            f"Bibliotheek: {pairs} voorbeelden · {profiles} {'soort' if profiles == 1 else 'soorten'} tuning · "
            f"{variants} softwarevarianten · "
            f"{families} ECU-families  (bijgewerkt {self.package['created_at'][:16].replace('T', ' ')} UTC)")
        if self.original is None:
            self.message.show("info", "Klaar voor gebruik", "Open een origineel bestand om te beginnen.")
        else:
            self.update_profiles()

    def _profile_key(self) -> str:
        return self.profile_map.get(self.profile_var.get(), "")

    def _transfer_options(self) -> dict:
        def pct(key, default):
            try:
                return max(1, min(100, float(self.settings.data.get(key, default)))) / 100
            except (TypeError, ValueError):
                return default / 100

        return {"hint": self.original.name if self.original else "",
                "min_similarity": pct("min_similarity", 70), "min_coverage": pct("min_coverage", 100),
                # minimum similarity per ECU family learned from practice
                "family_floor": read_stats(self.store.root).get("floors", {})}

    def poll_background(self) -> None:
        """Show background library progress and load new versions automatically."""

        try:
            status = read_status(self.store.root)
            if status["running"]:
                cur = status.get("current") or {}
                where = f" – {cur['source']}: {cur['done']} van {cur['found']} projecten" if cur else ""
                phase = " (nieuwe versie wordt gemaakt)" if status.get("phase") == "leren" else ""
                self.bg_var.set(f"Bibliotheek wordt op de achtergrond bijgewerkt{where}{phase}. U kunt gewoon doorwerken.")
            else:
                self.bg_var.set("")
            active = self.store.active_id()
            if active and active != self.active_id:
                self.reload_package()
                if self.package is not None and status["running"]:
                    self.bg_var.set(self.bg_var.get() + "  Nieuwe bibliotheekversie geladen.")
        except Exception:
            pass
        if not self._closed and self.root.winfo_exists():
            self._after_ids["poll"] = self.root.after(2000, self.poll_background)

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
        self.log.info("Origineel geopend: %s (%d bytes)", path.name, size)
        self.update_profiles()

    # ----- background work (keeps the window responsive) --------------------
    def _run(self, label: str, func, done) -> None:
        """Run ``func`` off the GUI thread; ``done(result, error)`` runs on the
        GUI thread. Only the newest request's result is applied."""

        token = object()
        self._token = token
        if self.synchronous:
            try:
                result, error = func(), None
            except Exception as exc:  # handed to done()
                result, error = None, exc
            done(result, error)
            return
        self._set_busy(label)

        def work() -> None:
            try:
                self._results.put((token, done, func(), None))
            except Exception as exc:
                self._results.put((token, done, None, exc))

        threading.Thread(target=work, daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                token, done, result, error = self._results.get_nowait()
                if token is None or token is self._token:
                    if token is not None:
                        self._set_busy(None)
                    try:
                        done(result, error)
                    except Exception:
                        self.log.exception("Fout bij verwerken van resultaat")
                        self.message.show("error", "Onverwachte fout",
                                          "De details staan in het logboek (Diagnose opslaan…).")
        except queue.Empty:
            pass
        if not self._closed and self.root.winfo_exists():
            self._after_ids["drain"] = self.root.after(120, self._drain)

    def _set_busy(self, label: str | None) -> None:
        state = "disabled" if label else "normal"
        self.open_button.configure(state=state)
        self.profile_box.configure(state="disabled" if label else "readonly")
        if label:
            self.make_button.configure(state="disabled")
            self.message.show("info", label, "Even geduld; u kunt de app gewoon blijven gebruiken.")
            self.root.configure(cursor="watch")
        else:
            self.root.configure(cursor="")

    def show_explanation(self) -> None:
        """Explain candidates for the open original (read-only, off the GUI thread)."""

        if self.original is None or self.package is None:
            self.message.show("info", "Uitleg", "Open eerst een origineel en laad de bibliotheek.")
            return
        ExplainWindow(self, self.original, self.package)

    def _show_error(self, error: Exception, what: str) -> None:
        if isinstance(error, GenerationRejected):
            self.log.warning("%s geweigerd [%s]: %s", what, error.code, error.message)
            self.message.show("error", "Dit bestand kan niet getuned worden", error.message)
        elif isinstance(error, OSError):
            self.log.error("Lezen mislukt: %s", error)
            self.message.show("error", "Bestand kon niet gelezen worden", str(error))
        else:
            self.log.error("Onverwachte fout bij %s: %r", what, error, exc_info=error)
            self.message.show("error", "Onverwachte fout",
                              f"{error}\nDe details staan in het logboek. Gebruik 'Diagnose opslaan…'.")

    def update_profiles(self) -> None:
        """Fill the tuning list with the profiles this file's software supports."""

        self.result = None
        self.make_button.configure(state="disabled")
        self.checksum_button.pack_forget()
        if self.original is None:
            return
        if self.package is None:
            if self._loading:
                self.message.show("info", "Bibliotheek wordt geladen…", "Het bestand wordt herkend zodra dat klaar is.")
            else:
                self.message.show("warn", "Nog geen bibliotheek", "Bouw eerst de bibliotheek op via 'Bibliotheek…'.")
            return
        path, package, options = self.original, self.package, self._transfer_options()

        def work():
            return available_profiles(path.read_bytes(), package, options["hint"], options["min_similarity"],
                                      options["family_floor"])

        def done(result, error) -> None:
            if error is not None:
                self.profile_map = {}
                self.profile_box.configure(values=[])
                self.profile_var.set("")
                self._show_error(error, "Herkenning")
                return
            family, variant, profiles = result
            previous = self._profile_key()
            self.log.info("Herkend: familie %s, variant %s, tunings: %s",
                          family["family_key"] if family else "-",
                          variant["variant_key"] if variant else "geen exacte variant (overzetten)",
                          ", ".join(profiles))
            self.profile_map = {profile_label(p): p for p in profiles}
            self.profile_box.configure(values=list(self.profile_map))
            choice = previous if previous in profiles else ("stage1" if "stage1" in profiles else profiles[0])
            self.profile_var.set(profile_label(choice))
            self.check_original()

        self._run("Bestand wordt herkend…", work, done)

    def check_original(self) -> None:
        """Recognise and prepare the tune so the user knows before clicking."""

        self.result = None
        self.make_button.configure(state="disabled")
        self.checksum_button.pack_forget()
        if self.original is None:
            return
        if self.package is None:
            self.message.show("warn", "Nog geen bibliotheek", "Bouw eerst de bibliotheek op via 'Bibliotheek…'.")
            return
        profile = self._profile_key()
        if not profile:
            return
        path, package, options, contracts = self.original, self.package, self._transfer_options(), self.store.contracts()

        def work():
            return generate(path.read_bytes(), profile, package, contracts, **options)

        def done(result, error) -> None:
            if error is not None:
                self._show_error(error, f"Tuning {profile}")
                return
            self._show_result(result)

        self._run(f"{profile_label(profile)} wordt voorbereid…", work, done)

    def _show_result(self, result) -> None:
        self.result = result
        self.current_family = result.family_key
        self.log.info("Klaar om te maken: %s, %s, %d gebieden, bewijs %s, checksumcontract %s",
                      result.profile, result.family_key, len(result.changes), result.evidence_level,
                      result.integrity["contract"])
        lines = [EVIDENCE_LABELS.get(result.evidence_level, result.evidence_level)]
        lines.extend(result.notes)
        if self.original is not None:
            from .export import overview_note

            overview = overview_note(self.original.read_bytes(), result.output)
            if overview:
                lines.append(overview)
        lines.append(f"Tuning: {profile_label(result.profile)}  ·  ECU: {result.identity['family']}  ·  "
                     f"{len(result.changes)} gebieden worden aangepast.")
        practice = family_line(read_stats(self.store.root), result.family_key)
        if practice:
            lines.append(practice)
        if not result.integrity["ready_for_use"]:
            lines.append("De app past geen checksum aan. Klik op 'Checksum vastleggen…' als AutoTuner de "
                         "checksum voor deze ECU bij het schrijven corrigeert; daarna kunt u de BIN maken.")
            self.message.show("warn", "Herkend – checksum nog niet vastgelegd", "\n".join(lines))
            self.checksum_button.pack(side="left", padx=(8, 0))
            return
        self.checksum_button.pack_forget()
        self.make_button.configure(state="normal")
        if result.evidence_level == "DONOR_TRANSFER":
            self.message.show("ok", "Overgezet – klaar om te maken", "\n".join(lines))
        else:
            self.message.show("ok", "Herkend – klaar om te maken", "\n".join(lines))

    def make_proposal(self) -> None:
        """Review list for software without an example (never a flash file)."""

        if self.original is None:
            self.message.show("warn", "Kaartvoorstel", "Open eerst een origineel bestand.")
            return
        from .consensus import write_proposal

        path, root = self.original, self.store.root
        profile = self._profile_key() or "stage1"
        target = Path(self.settings.data["output_dir"]) / f"{path.name}_{profile}_kaartvoorstel.x"

        def work():
            return write_proposal(path.read_bytes(), root, profile, target, source_name=path.name)

        def done(report, error) -> None:
            if error is not None:
                self.message.show("warn", "Geen kaartvoorstel", str(error))
                return
            self.message.show(
                "warn", f"Kaartvoorstel: {len(report['kaarten'])} kaarten ({report['ecu']})",
                "Lijst om in WinOLS te controleren; dit is geen complete tune en geen flashbestand.\n"
                f"Gebaseerd op {report['voorbeelden_gevonden']} voorbeelden. Opgeslagen: {report['csv']}")

        self._run("Kaartvoorstel wordt gemaakt…", work, done)

    def make_bin(self, ask_path: bool = True) -> Path | None:
        if self.result is None or self.original is None:
            return None
        result = self.result
        output_dir = Path(self.settings.data["output_dir"])
        tag = "".join(ch if ch.isalnum() else "-" for ch in profile_label(result.profile)).strip("-")
        name = f"{self.original.stem}_{tag}_{result.output_sha256[:8]}.bin".replace("--", "-")
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
            self.log.warning("Export geblokkeerd [%s]: %s", exc.code, exc.message)
            self.message.show("error", "Niet opgeslagen", exc.message)
            return None
        self.log.info("Opgeslagen: %s (sha256 %s)", final, result.output_sha256[:16])
        self.last_output = final
        self.report_button.configure(state="normal")
        self.message.show(
            "ok", f"Opgeslagen: {final.name}",
            f"Map: {final.parent}\n"
            f"Gecontroleerd: opnieuw ingelezen, 0 wijzigingen buiten de toegestane gebieden.\n"
            f"Resterende stap: {result.integrity['remaining_step']}\n"
            f"Rapport: {report.name}")
        return final

    def close(self) -> None:
        self._closed = True
        for after_id in self._after_ids.values():
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        close_logger(self.log)

    def confirm_checksum(self) -> None:
        from tkinter import messagebox

        if not self.current_family:
            return
        ecu = self.current_family.rsplit("|", 1)[0]
        if not messagebox.askyesno(
                "Checksum vastleggen",
                f"Corrigeert AutoTuner de checksum voor ECU {ecu} bij het schrijven?\n\n"
                "De app past zelf geen checksum aan. Bevestig alleen als u dit voor deze ECU zeker weet; "
                "een BIN met een onjuiste checksum kan de ECU laten weigeren te starten.",
                parent=self.root):
            return
        self.store.set_contract(self.current_family, "FLASH_TOOL_CORRECTS_ON_WRITE",
                                f"Bevestigd door gebruiker via het hoofdscherm voor ECU {ecu}")
        self.log.info("Checksumcontract vastgelegd voor %s", self.current_family)
        self.checksum_button.pack_forget()
        self.check_original()

    def save_diagnostics(self) -> Path | None:
        from datetime import datetime
        from tkinter import filedialog, messagebox

        name = f"OLSTuner-diagnose-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
        path = filedialog.asksaveasfilename(title="Diagnose opslaan", initialfile=name, defaultextension=".zip",
                                            initialdir=str(Path.home() / "Desktop"),
                                            filetypes=[("ZIP-bestand", "*.zip")])
        if not path:
            return None
        bundle = create_bundle(self.store, Path(path))
        self.log.info("Diagnose opgeslagen: %s", bundle)
        messagebox.showinfo("Diagnose opgeslagen",
                            f"Opgeslagen als:\n{bundle}\n\nDit bestand bevat alleen logboeken en tellingen, "
                            "geen klantbestanden. U kunt het naar de ontwikkelaar sturen.", parent=self.root)
        return bundle

    def _on_error(self, exc_type, exc, tb) -> None:
        import traceback

        self.log.error("Onverwachte fout: %s", "".join(traceback.format_exception(exc_type, exc, tb)))
        self.message.show("error", "Onverwachte fout",
                          f"{exc}\nDe details staan in het logboek. Gebruik 'Diagnose opslaan…' om ze te versturen.")

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


class ExplainWindow:
    """Candidates with matched / missing / conflicting fields and the next check."""

    FILTERS = {"Alle": None, "Exacte identiteit": "exact", "Andere revisie": "alternative_revision",
               "Onvoldoende bewijs": "insufficient", "Tegenstrijdig": "conflict"}

    def __init__(self, app: "TunerApp", path: Path, package) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.app, self.path, self.package, self.report = app, path, package, None
        self.win = tk.Toplevel(app.root)
        self.win.title(f"Uitleg – {path.name}")
        self.win.geometry("980x620")
        bar = ttk.Frame(self.win, padding=8)
        bar.pack(fill="x")
        self.filter_var = tk.StringVar(value="Alle")
        ttk.Label(bar, text="Filter:").pack(side="left")
        box = ttk.Combobox(bar, textvariable=self.filter_var, values=list(self.FILTERS), state="readonly", width=22)
        box.pack(side="left", padx=6)
        box.bind("<<ComboboxSelected>>", lambda _e: self.render())
        self.check_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Overzetcontrole uitvoeren (alleen in geheugen)", variable=self.check_var,
                        command=self.compute).pack(side="left", padx=12)
        ttk.Label(self.win, padding=(8, 0), style="Muted.TLabel", wraplength=940, text=(
            "Overeenkomst is een meting, geen kans. Identiteit, compatibiliteit en referentie zijn aparte "
            "bewijsstatussen; een hoge overeenkomst verhoogt ze nooit.")).pack(fill="x")
        self.text = tk.Text(self.win, wrap="word", font=("Consolas", 9), relief="flat")
        self.text.pack(fill="both", expand=True, padx=8, pady=8)
        self.compute()

    def compute(self) -> None:
        from .explain import explain

        self.text.delete("1.0", "end")
        self.text.insert("end", "Bezig met vergelijken…\n")
        options = self.app._transfer_options()
        path, package, check, root = self.path, self.package, self.check_var.get(), self.app.store.root

        def work() -> None:
            try:
                from .evidence import EvidenceGraph

                graph = EvidenceGraph(root)
                try:
                    result = explain(path.read_bytes(), package, store_root=root, hint=options["hint"], top=8,
                                     check=check, graph=graph)
                finally:
                    graph.close()
                self.app._results.put((None, self._done, result, None))
            except Exception as exc:
                self.app._results.put((None, self._done, None, exc))

        if self.app.synchronous:  # tests
            work()
            _token, done, result, error = self.app._results.get_nowait()
            done(result, error)
            return
        threading.Thread(target=work, daemon=True).start()

    def _done(self, report, error) -> None:
        if not self.win.winfo_exists():
            return
        if error is not None:
            self.app.log.error("Uitleg mislukt: %r", error, exc_info=error)
            self.text.delete("1.0", "end")
            self.text.insert("end", f"Uitleg mislukt: {error}")
            return
        self.report = report
        self.app.log.info("Uitleg: %s, %d kandidaten", report["verdict"]["status"], len(report["candidates"]))
        self.render()

    def render(self) -> None:
        from .explain import format_report

        if self.report is None:
            return
        wanted = self.FILTERS[self.filter_var.get()]
        shown = dict(self.report, candidates=[c for c in self.report["candidates"]
                                              if wanted is None or c["category"] == wanted])
        self.text.delete("1.0", "end")
        self.text.insert("end", format_report(shown) + "\n")


def checksum_choices(package, contracts: dict) -> list[tuple[str, int, bool]]:
    """(ECU type, examples, confirmed) of the library, most examples first.
    Types without a name cannot be confirmed: that would cover anything."""

    counts: dict[str, int] = {}
    for family in (package["families"] if package is not None else []):
        label = family["family_key"].rpartition("|")[0]
        if label.upper() in ("", "ONBEKEND", "UNKNOWN", "UNDEFINED", "NODATA"):
            continue
        counts[label] = counts.get(label, 0) + sum(len(v["pairs"]) for v in family["variants"])
    return [(label, count, contracts.get(f"{label}|*", {}).get("contract") == "FLASH_TOOL_CORRECTS_ON_WRITE")
            for label, count in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]


class LibraryWindow:
    TABS = ("Bronmappen", "Overzicht", "Versies", "Checksum", "Instellingen")

    def __init__(self, app: TunerApp) -> None:
        tk, ttk = app.tk, app.ttk
        self.app, self.tk, self.ttk = app, tk, ttk
        self.win = tk.Toplevel(app.root)
        self.win.title("OLS Tuner – Bibliotheek")
        self.win.geometry("980x640")
        self.win.minsize(820, 560)
        self.last_log: list[str] = []
        self.last_state = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._variant_rows: dict[str, list] = {}
        self.notebook = ttk.Notebook(self.win)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=12)
        self.tabs = {}
        for name in self.TABS:
            frame = ttk.Frame(self.notebook, padding=12)
            self.notebook.add(frame, text=f"  {name}  ")
            self.tabs[name] = frame
        self._build_sources(self.tabs["Bronmappen"])
        self._build_overview(self.tabs["Overzicht"])
        self._build_packages(self.tabs["Versies"])
        self._build_checksum(self.tabs["Checksum"])
        self._build_settings(self.tabs["Instellingen"])
        self.refresh()
        self._poll_id = self.win.after(200, self.poll)
        self._fill_id = self.win.after(100, self._drain_ui)
        self.win.bind("<Destroy>", self._on_destroy, add="+")

    def _drain_ui(self) -> None:
        try:
            while True:
                self._fill(self._ui_queue.get_nowait())
        except queue.Empty:
            pass
        except Exception as exc:
            self.summary_var.set(f"Overzicht kon niet worden getoond: {exc}")
        if self.win.winfo_exists():
            self._fill_id = self.win.after(150, self._drain_ui)

    def _on_destroy(self, event) -> None:
        if event.widget is self.win and getattr(self, "_fill_id", None):
            try:
                self.win.after_cancel(self._fill_id)
            except Exception:
                pass
        if event.widget is self.win and getattr(self, "_poll_id", None):
            try:
                self.win.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None

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
        self.practice_button = ttk.Button(controls, text="Oefenen", command=self.practise)
        self.practice_button.pack(side="left", padx=(8, 0))
        self.continuous_button = ttk.Button(controls, text="Blijven oefenen", command=self.keep_practising)
        self.continuous_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(controls, text="Stoppen", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Label(controls, text="Loopt op de achtergrond door, ook als u de app sluit. Na het bijwerken "
                  "oefent hij vanzelf: elk voorbeeld verbergen, opnieuw tunen en vergelijken.",
                  style="Muted.TLabel", wraplength=400).pack(side="left", padx=8)
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
        self.last_log = []  # force the next status poll to redraw

    def update_library(self) -> None:
        if not self._sources():
            self.write("Voeg eerst minstens één map toe.")
            return
        self.app.settings.save()
        if start_background(self.app.store.root):
            self.write("Bijwerken gestart op de achtergrond.")
        else:
            self.write("Het bijwerken loopt al.")
        self.poll(once=True)

    def practise(self) -> None:
        if self.app.package is None:
            self.write("Werk eerst de bibliotheek bij.")
            return
        if start_background(self.app.store.root, "practice"):
            self.write("Oefenen gestart op de achtergrond: elk voorbeeld wordt verborgen, opnieuw getuned uit de "
                       "rest van de database en vergeleken met de echte tune.")
        else:
            self.write("Er loopt al een achtergrondtaak.")
        self.poll(once=True)

    def keep_practising(self) -> None:
        self.app.settings.save()
        if start_background(self.app.store.root, "continuous"):
            self.write("Blijven oefenen gestart: nieuwe projecten scannen, leren, elk voorbeeld verbergen en "
                       "opnieuw tunen, en elke 30 minuten opnieuw. Loopt door tot u op Stoppen drukt.")
        else:
            self.write("Er loopt al een achtergrondtaak.")
        self.poll(once=True)

    def stop(self) -> None:
        request_stop(self.app.store.root)
        self.progress_var.set("Stoppen… lopende projecten worden afgemaakt, daarna wordt geleerd van wat al gescand is.")

    def poll(self, once: bool = False) -> None:
        try:
            status = read_status(self.app.store.root)
            running = status["running"]
            self.run_button.configure(state="disabled" if running else "normal")
            self.practice_button.configure(state="disabled" if running else "normal")
            self.continuous_button.configure(state="disabled" if running else "normal")
            self.stop_button.configure(state="normal" if running else "disabled")
            cur = status.get("current") or {}
            if running:
                if cur.get("found"):
                    self.progress.configure(value=cur["done"] / cur["found"] * 100)
                text = (f"{cur.get('source', '')}: {cur.get('done', 0)} van {cur.get('found', 0)} projecten gelezen"
                        if cur else "Bezig…")
                if status.get("phase") == "leren":
                    text += " – nieuwe bibliotheekversie wordt gemaakt"
                if status.get("phase") == "oefenen":
                    text = f"Oefenen: {cur.get('done', 0)} van {cur.get('found', 0)} voorbeelden getest"
                if not self.progress_var.get().startswith("Stoppen"):
                    self.progress_var.set(text)
            elif status.get("state") in ("finished", "stopped", "failed", "interrupted"):
                labels = {"finished": "Klaar.", "stopped": "Gestopt – wat al gescand is, is bruikbaar.",
                          "failed": f"Mislukt: {status.get('message', '')}",
                          "interrupted": "Onderbroken (bijv. pc uitgezet). Klik op 'Bibliotheek bijwerken' om verder te gaan."}
                self.progress.configure(value=100 if status["state"] == "finished" else self.progress["value"])
                self.progress_var.set(labels[status["state"]])
            log = status.get("log") or []
            if log != self.last_log:
                self.log.delete("1.0", "end")
                self.log.insert("end", "\n".join(log) + "\n")
                self.log.see("end")
                self.last_log = log
            if status.get("state") != self.last_state:
                self.last_state = status.get("state")
                if status.get("sources"):
                    stats = self.app.settings.data.setdefault("source_stats", {})
                    for key, st in status["sources"].items():
                        stats[key] = "niet bereikbaar" if st["status"].startswith("unavailable") else (
                            f"{st['found']} projecten, {st.get('total_pairs', st['confirmed_pairs'])} voorbeelden"
                            + (f", {st['retry_pending']} opnieuw proberen" if st["retry_pending"] else ""))
                    self.app.settings.save()
                if not running:
                    self.refresh()
        except Exception as exc:
            self.progress_var.set(f"Status kon niet worden gelezen: {exc}")
        if not once and self.win.winfo_exists():
            self._poll_id = self.win.after(1000, self.poll)

    # ----- Overzicht --------------------------------------------------------
    def _build_overview(self, tab) -> None:
        ttk, tk = self.ttk, self.tk
        self.summary_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.summary_var, style="Big.TLabel").pack(anchor="w")
        self.scan_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.scan_var, style="Sub.TLabel", wraplength=840, justify="left").pack(anchor="w", pady=(2, 8))
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Button(top, text="Vernieuwen", command=self.refresh_overview).pack(side="right")
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True, pady=(6, 0))
        cols = (("tunings", "Soorten tuning", 180), ("projects", "Projecten", 80), ("pairs", "Voorbeelden", 95),
                ("practice", "Oefentests", 150), ("checksum", "Checksum", 120))
        self.overview = ttk.Treeview(frame, columns=[c[0] for c in cols], show="tree headings")
        self.overview.heading("#0", text="ECU-familie / softwarevariant")
        self.overview.column("#0", width=260)
        for col, label, width in cols:
            self.overview.heading(col, text=label, anchor="w")
            self.overview.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, command=self.overview.yview)
        self.overview.configure(yscrollcommand=scroll.set)
        self.overview.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.overview.bind("<<TreeviewOpen>>", self._open_family)

    def _scan_text(self) -> str:
        db = self.app.store.root / "library.sqlite3"
        if not db.exists():
            return "Nog niets gescand."
        import sqlite3

        try:
            connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                files = dict(connection.execute("SELECT source, COUNT(*) FROM files GROUP BY source").fetchall())
                pairs = dict(connection.execute("SELECT source, COUNT(*) FROM pairs GROUP BY source").fetchall())
            finally:
                connection.close()
        except sqlite3.Error as exc:
            return f"Scanstatus niet leesbaar: {exc}"
        parts = [f"{src}: {files.get(src, 0)} gescand, {pairs.get(src, 0)} voorbeelden" for src in sorted(set(files) | set(pairs))]
        status = read_status(self.app.store.root)
        prefix = "Scan loopt nog. " if status["running"] else ""
        return prefix + ("Gescand per map – " + "  ·  ".join(parts) if parts else "Nog niets gescand.")

    def refresh_overview(self) -> None:
        self.refresh()

    def _fill(self, data: dict) -> None:
        if "error" in data:
            self.summary_var.set(f"Overzicht kon niet worden geladen: {data['error']}")
            return
        self._fill_overview(data)
        self._fill_packages(data)

    def _fill_overview(self, data: dict) -> None:
        self.overview.delete(*self.overview.get_children())
        self._variant_rows = {}
        package = self.app.package
        stats = data["stats"]
        self.scan_var.set(data["scan"] + "\n" + summary_line(stats))
        if package is None:
            self.summary_var.set("Nog geen bibliotheekversie. Start 'Bibliotheek bijwerken'; tussentijds verschijnen "
                                 "hier de resultaten.")
            return
        contracts = data["contracts"]
        variants = sum(len(f["variants"]) for f in package["families"])
        self.summary_var.set(f"{len(package['training_selection'])} voorbeelden · {variants} softwarevarianten · "
                             f"{len(package['families'])} ECU-families")
        for family in package["families"]:
            contract = contract_for(contracts, family["family_key"]).get("contract", "UNKNOWN")
            check = "vastgelegd" if contract == "FLASH_TOOL_CORRECTS_ON_WRITE" else "niet vastgelegd"
            projects = len({p for v in family["variants"] for p in v["projects"]})
            pairs = sum(len(v["pairs"]) for v in family["variants"])
            fam_profiles = {p for v in family["variants"] for p in v["profiles"]}
            fam_id = self.overview.insert(
                "", "end", text=f"{family['ecu_type'] or family['family_key']}  ({family['size']:,} B)".replace(",", "."),
                values=(f"{len(fam_profiles)} {'soort' if len(fam_profiles) == 1 else 'soorten'}", projects, pairs,
                        self._practice_text(stats, family["family_key"]), check),
                open=False)
            if family["variants"]:
                self.overview.insert(fam_id, "end", text="…")  # filled when opened
                self._variant_rows[fam_id] = family["variants"]

    def _open_family(self, _event=None) -> None:
        item = self.overview.focus()
        variants = self._variant_rows.pop(item, None)
        if variants is None:
            return
        self.overview.delete(*self.overview.get_children(item))
        for variant in variants[:500]:
            label = variant["variant_key"].replace("|", "  ·  ")
            tunings = ", ".join(profile_label(p) for p in sorted(variant["profiles"]))
            self.overview.insert(item, "end", text=label[:120], values=(
                tunings, len(variant["projects"]), len(variant["pairs"]), "", ""))
        if len(variants) > 500:
            self.overview.insert(item, "end", text=f"… en nog {len(variants) - 500} varianten")

    @staticmethod
    def _practice_text(stats: dict, family_key: str) -> str:
        f = stats.get("families", {}).get(family_key)
        if not f or not f["tests"]:
            return "nog niet"
        return f"{f['good']}/{f['tests']} goed ({f['average_score']:.0%})"

    # ----- Versies ---------------------------------------------------------
    def _build_packages(self, tab) -> None:
        ttk = self.ttk
        ttk.Label(tab, text="U hoeft hier normaal niets te doen. Elke keer dat de bibliotheek wordt bijgewerkt, "
                  "maakt de app automatisch een nieuwe versie van wat hij geleerd heeft en gebruikt die meteen. "
                  "Oudere versies blijven bewaard, zodat u terug kunt.", style="Sub.TLabel", wraplength=840,
                  justify="left").pack(anchor="w", pady=(0, 8))
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        cols = (("active", "", 70), ("date", "Gemaakt", 170), ("pairs", "Voorbeelden", 100),
                ("variants", "Softwarevarianten", 130), ("id", "Kenmerk", 150))
        self.package_tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", height=10)
        for col, label, width in cols:
            self.package_tree.heading(col, text=label, anchor="w")
            self.package_tree.column(col, width=width, anchor="w")
        self.package_tree.pack(side="left", fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(side="left", fill="y", padx=(10, 0))
        ttk.Button(buttons, text="Terug naar vorige versie", command=self.rollback).pack(fill="x")
        ttk.Button(buttons, text="Back-up maken…", command=self.export_package).pack(fill="x", pady=(16, 4))
        ttk.Button(buttons, text="Back-up terugzetten…", command=self.attach).pack(fill="x")
        ttk.Label(tab, text="Een back-up (.olstune) bevat gegevens uit klantbestanden: alleen lokaal bewaren. "
                  "Handig om de bibliotheek naar een andere pc te verhuizen.", style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

    def refresh_packages(self) -> None:
        self.refresh()

    def _fill_packages(self, data: dict) -> None:
        self.package_tree.delete(*self.package_tree.get_children())
        for item in sorted(data["packages"], key=lambda i: i.get("created_at", ""), reverse=True):
            if "error" in item:
                continue
            self.package_tree.insert("", "end", values=(
                "in gebruik" if item["package_id"] == data["active"] else "", item["created_at"].replace("T", " ")[:16],
                item["pairs"], item["variants"], item["package_id"]))

    def attach(self) -> None:
        from tkinter import filedialog, messagebox

        path = filedialog.askopenfilename(title="Back-up terugzetten",
                                          filetypes=[("Bibliotheek-back-up", "*.olstune"), ("Alle bestanden", "*.*")])
        if not path:
            return
        try:
            self.app.store.install_file(Path(path))
        except (OSError, ValueError) as exc:
            messagebox.showerror("Terugzetten mislukt", str(exc), parent=self.win)
            return
        self.app.reload_package()
        self.refresh()
        messagebox.showinfo("Teruggezet", "De back-up is teruggezet en in gebruik.", parent=self.win)

    def rollback(self) -> None:
        from tkinter import messagebox

        try:
            self.app.store.rollback()
        except ValueError as exc:
            messagebox.showinfo("Geen vorige versie", str(exc), parent=self.win)
            return
        self.app.reload_package()
        self.refresh()
        messagebox.showinfo("Teruggezet", "De vorige versie is weer in gebruik.", parent=self.win)

    def export_package(self) -> None:
        from tkinter import filedialog, messagebox

        if not self.app.package:
            messagebox.showinfo("Geen versie", "Er is nog geen bibliotheekversie.", parent=self.win)
            return
        path = filedialog.asksaveasfilename(defaultextension=".olstune",
                                            initialfile=f"bibliotheek-{self.app.package['created_at'][:10]}.olstune",
                                            filetypes=[("Bibliotheek-back-up", "*.olstune")])
        if path:
            write_package(self.app.package, Path(path))
            messagebox.showinfo("Back-up gemaakt", f"Opgeslagen als {path}", parent=self.win)

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
        self._build_checksum_choice(tab)

    def _build_checksum_choice(self, tab) -> None:
        """One line per ECU type with the number of examples: tick once what
        the flash tool corrects, for every read size of that type."""

        ttk, tk = self.ttk, self.tk
        box = ttk.LabelFrame(tab, text="  Keuzelijst per ECU-type  ", padding=8)
        box.pack(fill="both", expand=True, pady=(10, 0))
        bar = ttk.Frame(box)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Zoeken:").pack(side="left")
        self.choice_filter = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.choice_filter, width=22)
        entry.pack(side="left", padx=6)
        entry.bind("<KeyRelease>", lambda _e: self.fill_checksum_choice())
        ttk.Button(bar, text="AutoTuner corrigeert deze", command=lambda: self.set_checksum_choice(True)).pack(side="left")
        ttk.Button(bar, text="Niet (meer) zeker", command=lambda: self.set_checksum_choice(False)).pack(side="left", padx=6)
        self.choice_summary = tk.StringVar()
        ttk.Label(bar, textvariable=self.choice_summary, style="Muted.TLabel").pack(side="left", padx=6)
        frame = ttk.Frame(box)
        frame.pack(fill="both", expand=True)
        self.choice_tree = ttk.Treeview(frame, columns=("ecu", "count", "status"), show="headings", height=9,
                                        selectmode="extended")
        for col, label, width in (("ecu", "ECU-type", 240), ("count", "Voorbeelden", 110),
                                  ("status", "Checksum", 260)):
            self.choice_tree.heading(col, text=label, anchor="w")
            self.choice_tree.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, command=self.choice_tree.yview)
        self.choice_tree.configure(yscrollcommand=scroll.set)
        self.choice_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

    def fill_checksum_choice(self) -> None:
        rows = checksum_choices(self.app.package, self.app.store.contracts())
        wanted = self.choice_filter.get().strip().upper()
        self.choice_tree.delete(*self.choice_tree.get_children())
        for label, count, confirmed in [x for x in rows if wanted in x[0]][:400]:
            self.choice_tree.insert("", "end", iid=label, values=(
                label, count, "AutoTuner corrigeert (door u bevestigd)" if confirmed else "niet bevestigd"))
        total = sum(count for _l, count, _c in rows)
        done = sum(count for _l, count, confirmed in rows if confirmed)
        self.choice_summary.set(f"Bevestigd: {done / total:.0%} van de voorbeelden" if total else "")

    def set_checksum_choice(self, confirmed: bool) -> None:
        from tkinter import messagebox

        labels = list(self.choice_tree.selection())
        if not labels:
            messagebox.showinfo("Kies ECU-typen", "Selecteer eerst een of meer regels.", parent=self.win)
            return
        shown = ", ".join(labels[:8]) + (f" en nog {len(labels) - 8}" if len(labels) > 8 else "")
        question = (f"Weet u zeker dat AutoTuner bij deze ECU's de checksum corrigeert bij het schrijven?\n\n{shown}\n\n"
                    "Klopt dit niet, dan kan een ECU na het schrijven niet meer starten."
                    if confirmed else f"De bevestiging intrekken voor:\n\n{shown}?")
        if not messagebox.askyesno("Checksum", question, parent=self.win):
            return
        for label in labels:
            self.app.store.set_contract(
                f"{label}|*", "FLASH_TOOL_CORRECTS_ON_WRITE" if confirmed else "UNKNOWN",
                "Door gebruiker bevestigd in de keuzelijst: AutoTuner corrigeert bij schrijven")
        self.refresh()
        self.refresh_checksum()
        self.app.check_original()

    def refresh_checksum(self) -> None:
        self.fill_checksum_choice()
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
        match = ttk.LabelFrame(tab, text="  Overzetten van een vergelijkbaar bestand  ", padding=8)
        match.pack(fill="x", pady=(12, 0))
        self.sim_var = tk.StringVar(value=str(self.app.settings.data.get("min_similarity", 70)))
        self.cov_var = tk.StringVar(value=str(self.app.settings.data.get("min_coverage", 100)))
        ttk.Label(match, text="Minimale gelijkenis met een bestand uit de database (%):").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(match, from_=30, to=100, textvariable=self.sim_var, width=6).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(match, text="Minimaal deel van de wijzigingen dat geplaatst moet worden (%):").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(match, from_=50, to=100, textvariable=self.cov_var, width=6).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(match, text="Standaard 70% en 100%. Lager zetten maakt meer mogelijk, maar controleer het resultaat dan zelf "
                  "in WinOLS.", style="Muted.TLabel", wraplength=780).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
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
        ttk.Button(tab, text="Diagnose opslaan…", command=self.app.save_diagnostics).pack(anchor="w", pady=4)
        ttk.Label(tab, text="Logboeken: app.log (tunen), library_worker.log en library_status.json (bijwerken) "
                  "in de gegevensmap. 'Diagnose opslaan' bundelt ze zonder klantbestanden.",
                  style="Muted.TLabel", wraplength=820).pack(anchor="w")

    def save_settings(self) -> None:
        self.app.settings.data.update({"output_dir": self.output_var.get(), "exports": self.exports_var.get(),
                                       "py_ols": self.py_ols_var.get(), "min_similarity": self.sim_var.get(),
                                       "min_coverage": self.cov_var.get()})
        self.app.settings.save()
        self.write("Instellingen opgeslagen.")

    def refresh(self) -> None:
        """Gather overview data off the GUI thread, then fill the tabs."""

        self.refresh_sources()
        self.refresh_checksum()
        self.summary_var.set("Gegevens worden geladen…")
        store = self.app.store

        def work() -> None:
            try:
                data = {"scan": self._scan_text(), "packages": store.list_packages(), "active": store.active_id(),
                        "contracts": store.contracts(), "stats": read_stats(store.root)}
                self._ui_queue.put(data)
            except Exception as exc:
                self._ui_queue.put({"error": str(exc)})

        threading.Thread(target=work, daemon=True).start()


def _install_crash_logging(root: Path) -> None:
    """Record every crash: Python errors anywhere (also in threads) go to
    app.log, hard crashes (e.g. a native fault) to crash.log."""

    import faulthandler
    import traceback

    log = app_logger(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        crash_file = open(root / "crash.log", "a", encoding="utf-8")
        crash_file.write(f"\n--- start {SOFTWARE_VERSION} ---\n")
        crash_file.flush()
        faulthandler.enable(file=crash_file, all_threads=True)
    except OSError:
        pass

    def excepthook(exc_type, exc, tb):
        log.error("Onverwachte fout: %s", "".join(traceback.format_exception(exc_type, exc, tb)))

    def thread_hook(args):
        log.error("Onverwachte fout in achtergrondtaak: %s",
                  "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook


def main(argv: list[str] | None = None) -> int:
    import multiprocessing

    multiprocessing.freeze_support()  # worker processes of the frozen EXE
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
                            "export-package", "contract", "update-library", "practice", "explain", "review",
                            "relations", "fingerprint-index", "evaluate-matching", "--store"):
        from .__main__ import main as cli_main

        return cli_main(argv)
    _install_crash_logging(PackageStore().root)
    import tkinter as tk

    root = tk.Tk()
    app = TunerApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.close(), root.destroy()))
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
