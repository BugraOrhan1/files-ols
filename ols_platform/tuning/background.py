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

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_FILE = "library_status.json"
STOP_FILE = "library.stop"
LOG_FILE = "library_worker.log"
HEARTBEAT_SECONDS = 3
STALE_AFTER = 45


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_status(root: Path) -> dict[str, Any]:
    try:
        status = json.loads((root / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "idle", "running": False}
    running = status.get("state") == "running" and time.time() - status.get("heartbeat", 0) < STALE_AFTER
    status["running"] = running
    if status.get("state") == "running" and not running:
        status["state"] = "interrupted"
    return status


def request_stop(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / STOP_FILE).write_text(_now(), encoding="utf-8")


def worker_command(root: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--store", str(root), "update-library"]
    return [sys.executable, "-m", "ols_platform.tuning", "--store", str(root), "update-library"]


def start_background(root: Path) -> bool:
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
        subprocess.Popen(worker_command(root), cwd=str(cwd) if (cwd / "ols_platform").exists() else None, **kwargs)
    finally:
        log.close()  # the child keeps its own handle
    # Write an initial status so the UI shows "running" immediately.
    _write_status(root, {"state": "running", "pid": None, "started_at": _now(), "heartbeat": time.time(),
                         "phase": "starten", "log": ["Bijwerken gestart…"], "sources": {}})
    return True


def _write_status(root: Path, status: dict[str, Any]) -> None:
    tmp = root / (STATUS_FILE + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, root / STATUS_FILE)


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


def run_update(root: Path, *, publish_every_pairs: int = 25, publish_every_seconds: int = 600,
               workers: int | None = None) -> int:
    """Body of ``update-library``: scan all configured folders, publish
    intermediate packages, and always publish what was found at the end."""

    from .__main__ import _reader, _sources
    from .library import Library
    from .package import PackageStore

    current = read_status(root)
    if current["running"] and current.get("pid") not in (None, os.getpid()):
        print("Er loopt al een bijwerkproces.")
        return 0
    try:
        settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
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
    last_publish = {"time": time.time(), "pairs": 0}

    def publish(reason: str) -> None:
        status.update(phase="leren")
        status.say(f"Leren en publiceren ({reason})…")
        try:
            result = library.train(progress=status.say)
        except ValueError as exc:
            status.say(str(exc))
            status.update(phase="scannen")
            return
        if result["installed"]:
            status.say(f"Nieuwe versie actief: {result['pairs_used']} voorbeelden, "
                       f"{len(result['families'])} ECU-families.")
            status.data["published"].append({"package_id": result["package_id"], "at": _now(),
                                             "pairs": result["pairs_used"]})
        status.update(phase="scannen")

    def on_pairs(new_total: int) -> None:
        if (new_total - last_publish["pairs"] >= publish_every_pairs
                or time.time() - last_publish["time"] >= publish_every_seconds):
            last_publish.update(time=time.time(), pairs=new_total)
            publish("tussentijds")

    def on_file(source: str, done_count: int, found: int) -> None:
        status.update(current={"source": source, "done": done_count, "found": found})

    exit_code = 0
    try:
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
                if st["status"].startswith("unavailable"):
                    status.say(f"{key}: niet bereikbaar")
                else:
                    status.say(f"{key}: {st['found']} projecten, {st['confirmed_pairs']} voorbeelden "
                               f"({st['new_pairs']} nieuw)")
        publish("gestopt – wat al gescand is" if stop.is_set() else "klaar")
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
