"""Windows-safe small-file writes and reads.

On Windows ``os.replace`` fails with PermissionError while another process
has the target open (e.g. the app reading the status file the background
worker is replacing). These helpers retry briefly instead of crashing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

RETRIES = 40
DELAY = 0.05


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == RETRIES - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(DELAY)


def write_json(path: Path, data: Any, indent: int | None = None) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))


def read_json(path: Path, default: Any = None) -> Any:
    for attempt in range(RETRIES):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except PermissionError:
            time.sleep(DELAY)
        except ValueError:
            # half-visible write from an older version: try again shortly
            if attempt >= 3:
                return default
            time.sleep(DELAY)
    return default
