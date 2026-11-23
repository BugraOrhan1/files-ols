"""Library update as an independent background process.

The app starts ``OLSTuner.exe update-library`` (or ``python -m
ols_platform.tuning update-library``) detached from the GUI, so closing the
library window or the whole app does not stop it. Progress is shared through
``library_status.json`` in the store; stopping is requested with a
``library.stop`` file. During the scan an intermediate package is published
regularly, and once more when the run ends or is stopped, so everything that
has been scanned so far is usable right away.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .fileio import read_json, write_json

STATUS_FILE = "library_status.json"
STOP_FILE = "library.stop"
LOG_FILE = "library_worker.log"
HEARTBEAT_SECONDS = 3
STALE_AFTER = 45


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_status(root: Path) -> dict[str, Any]:
    status = read_json(root / STATUS_FILE)
    if not isinstance(status, dict):
        return {"state": "idle", "running": False}
    running = status.get("state") == "running" and time.time() - status.get("heartbeat", 0) < STALE_AFTER
    status["running"] = running
    if status.get("state") == "running" and not running:
        status["state"] = "interrupted"
    return status


def request_stop(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / STOP_FILE).write_text(_now(), encoding="utf-8")


def worker_command(root: Path, mode: str = "update") -> list[str]:
    command = ["practice"] if mode == "practice" else ["update-library"]
    if mode == "continuous":
        command.append("--continuous")
    if getattr(sys, "frozen", False):
        return [sys.executable, "--store", str(root), *command]
    return [sys.executable, "-m", "ols_platform.tuning", "--store", str(root), *command]


def start_background(root: Path, mode: str = "update") -> bool:
    """Start the worker detached. Returns False when one is already running."""

    if read_status(root)["running"]:
        return False
    root.mkdir(parents=True, exist_ok=True)
    (root / STOP_FILE).unlink(missing_ok=True)
    log = open(root / LOG_FILE, "ab")
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    cwd = Path(__file__).resolve().parents[2]
    try:
        subprocess.Popen(worker_command(root, mode), cwd=str(cwd) if (cwd / "ols_platform").exists() else None, **kwargs)
    finally:
        log.close()  # the child keeps its own handle
    # Write an initial status so the UI shows "running" immediately.
    _write_status(root, {"state": "running", "pid": None, "started_at": _now(), "heartbeat": time.time(),
                         "phase": "starten", "sources": {},
                         "log": [{"practice": "Oefenen gestart…", "continuous": "Blijven oefenen gestart…"}.get(
                             mode, "Bijwerken gestart…")]})
    return True


def _write_status(root: Path, status: dict[str, Any]) -> None:
    try:
        write_json(root / STATUS_FILE, status)
    except OSError:
        pass  # a missed heartbeat is harmless; never let it stop the worker


class _Status:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.Lock()
        self.log: deque[str] = deque(maxlen=80)
        self.data: dict[str, Any] = {
            "state": "running", "pid": os.getpid(), "started_at": _now(), "phase": "scannen",
            "current": None, "sources": {}, "published": [], "message": "",
        }

    def say(self, message: str) -> None:
        with self.lock:
            self.log.append(f"{datetime.now().strftime('%H:%M:%S')}  {message}")

    def update(self, **values: Any) -> None:
        with self.lock:
            self.data.update(values)

    def flush(self) -> None:
        with self.lock:
            data = dict(self.data, heartbeat=time.time(), log=list(self.log))
        _write_status(self.root, data)


def publish_due(total: int, published: int, since: float, took: float,
                every_pairs: int, every_seconds: int) -> bool:
    """Whether an intermediate package is due.

    Training time grows with the library; publishing every few hundred
    pairs would spend most of a long run re-learning instead of scanning.
    So both thresholds scale: at least ``every_pairs`` or a quarter of the
    already published examples, and never sooner than four times the last
    training run took (scanning stays at least ~80% of the time)."""

    new = total - published
    if new <= 0:
        return False
    if since < 4 * took:
        return False
    return new >= max(every_pairs, published // 4) or since >= max(every_seconds, 4 * took)


def run_update(root: Path, *, publish_every_pairs: int = 500, publish_every_seconds: int = 1800,
               workers: int | None = None, practice: bool = True, practice_only: bool = False,
               continuous: bool = False, pause_minutes: int = 30) -> int:
    """Body of ``update-library``: scan all configured folders, publish
    intermediate packages, and always publish what was found at the end.

    With ``continuous`` the worker keeps going until stopped: scan for new
    projects, learn, practise (hide each example, re-tune it from the rest,
    compare, learn per ECU family how far transfers can be trusted), pause,
    and start over."""

    from .__main__ import _reader, _sources
    from .library import Library
    from .package import PackageStore

    current = read_status(root)
    if current["running"] and current.get("pid") not in (None, os.getpid()):
        print("Er loopt al een bijwerkproces.")
        return 0
    settings = read_json(root / "settings.json", {}) or {}
    sources = settings.get("sources") or []
    status = _Status(root)
    stop = threading.Event()
    (root / STOP_FILE).unlink(missing_ok=True)

    def heartbeat() -> None:
        while not done.is_set():
            if (root / STOP_FILE).exists():
                stop.set()
            status.flush()
            done.wait(HEARTBEAT_SECONDS)

    done = threading.Event()
    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    store = PackageStore(root)
    library = Library(store)
    last_publish = {"time": time.time(), "pairs": 0, "took": 0.0}

    def publish(reason: str) -> None:
        status.update(phase="leren")
        status.say(f"Leren en publiceren ({reason})…")
        started = time.time()
        try:
            result = library.train(progress=status.say)
        except ValueError as exc:
            status.say(str(exc))
            status.update(phase="scannen")
            return
        finally:
            last_publish["took"] = time.time() - started
        if result["installed"]:
            status.say(f"Nieuwe versie actief: {result['pairs_used']} voorbeelden, "
                       f"{len(result['families'])} ECU-families.")
            status.data["published"].append({"package_id": result["package_id"], "at": _now(),
                                             "pairs": result["pairs_used"]})
        status.update(phase="scannen")

    def on_pairs(new_total: int) -> None:
        if not publish_due(new_total, last_publish["pairs"], time.time() - last_publish["time"],
                           last_publish["took"], publish_every_pairs, publish_every_seconds):
            return
        last_publish.update(time=time.time(), pairs=new_total)
        publish("tussentijds")

    def on_file(source: str, done_count: int, found: int) -> None:
        status.update(current={"source": source, "done": done_count, "found": found})

    def practise() -> None:
        from .practice import run_practice

        status.update(phase="oefenen", current={"source": "Oefenen", "done": 0, "found": 0})
        run_practice(library, stop=stop, workers=workers, progress=status.say,
                     on_test=lambda done_count, total: status.update(
                         current={"source": "Oefenen", "done": done_count, "found": total}))

    exit_code = 0
    try:
        if practice_only:
            practise()
            status.update(state="stopped" if stop.is_set() else "finished", phase="klaar", finished_at=_now())
            status.say("Oefenen gestopt; verdergaan kan later." if stop.is_set() else "Oefenen klaar.")
            return exit_code
        round_no = 0
        while True:
            round_no += 1
            if round_no > 1:
                # settings are re-read every round: newly connected drives
                # (Database 1-4) are picked up without restarting
                settings = read_json(root / "settings.json", {}) or {}
                sources = settings.get("sources") or []
                status.say(f"Ronde {round_no}: opnieuw scannen op nieuwe projecten…")
                status.update(phase="scannen")
            new_pairs = 0
            last_publish["pairs"] = 0  # on_pairs counts per scan
            if not sources:
                status.say("Geen bronmappen ingesteld.")
            else:
                summary = library.scan(
                    _sources(sources), _reader(settings.get("py_ols") or None),
                    export_dirs=[Path(settings["exports"])] if settings.get("exports") else [],
                    progress=status.say, stop=stop, on_file=on_file, on_pairs=on_pairs, workers=workers,
                )
                status.update(sources=summary["sources"], resources=summary["resources"])
                for key, st in summary["sources"].items():
                    new_pairs += st.get("new_pairs", 0)
                    if st["status"].startswith("unavailable"):
                        status.say(f"{key}: niet bereikbaar (wat eerder geleerd is blijft bewaard)")
                    else:
                        status.say(f"{key}: {st['found']} projecten, {st.get('total_pairs', st['confirmed_pairs'])} "
                                   f"voorbeelden ({st['new_pairs']} nieuw)")
            maps_reader = settings.get("py_ols_maps") or settings.get("py_ols")
            if maps_reader and not stop.is_set():
                from .maps import learn_maps

                status.update(phase="kaarten", current=None)
                try:
                    learn_maps(root, Path(maps_reader), workers=workers or 4, progress=status.say,
                               stop=stop.is_set)
                except Exception as exc:  # map knowledge is extra: never fails the update
                    status.say(f"Kaartkennis overgeslagen: {exc}")
            if round_no == 1 or new_pairs or stop.is_set():
                publish("gestopt – wat al gescand is" if stop.is_set() else "klaar")
            else:
                status.say("Geen nieuwe voorbeelden; huidige bibliotheekversie blijft actief.")
            if not stop.is_set():
                from .consensus import learn_structure

                status.update(phase="kaarten", current=None)
                try:
                    learn_structure(root, workers=workers or 4, progress=status.say, stop=stop.is_set)
                except Exception as exc:  # extra knowledge: never fails the update
                    status.say(f"Kaartstructuur overgeslagen: {exc}")
            if practice and not stop.is_set():
                practise()
            if not continuous or stop.is_set():
                break
            status.update(phase="wachten", current=None)
            status.say(f"Blijven oefenen: volgende ronde over {pause_minutes} min (Stoppen beëindigt).")
            deadline = time.time() + pause_minutes * 60
            while time.time() < deadline and not stop.is_set():
                stop.wait(5)
            if stop.is_set():
                break
        status.update(state="stopped" if stop.is_set() else "finished", phase="klaar", finished_at=_now())
        status.say("Gestopt. Alles wat al gescand is, is nu bruikbaar." if stop.is_set() else "Klaar.")
    except Exception as exc:  # recorded for the UI
        status.update(state="failed", phase="mislukt", message=str(exc), finished_at=_now())
        status.say(f"Fout: {exc}")
        exit_code = 1
    finally:
        library.close()
        done.set()
        beat.join(timeout=5)
        status.flush()
        (root / STOP_FILE).unlink(missing_ok=True)
    return exit_code
