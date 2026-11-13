"""Single path-normalisation boundary for everything stored in the index.

Paths are recorded in ``file_locations``, ``source_files``, ``import_jobs``
and friends, then later looked up by exact string match. Some callers build
those lookup keys with ``Path.resolve()`` while others used
``os.path.abspath()``. The two differ whenever the path contains a symlink,
a junction or (on Windows) an 8.3 short-name alias such as
``C:\\Users\\RUNNER~1``: ``abspath`` keeps the alias, ``resolve`` expands it.
Every path that is written to or looked up in the index must go through
:func:`canonical_path` so both sides agree.
"""

from __future__ import annotations

import os
from pathlib import Path


def canonical_path(path: str | os.PathLike[str]) -> str:
    """Return the absolute, fully resolved form of ``path`` as a string.

    Matches ``str(Path(path).resolve())``: symlinks, junctions and Windows
    8.3 short names are expanded. Non-existent paths are resolved as far as
    possible without raising.
    """
    return str(Path(os.path.abspath(path)).resolve())
