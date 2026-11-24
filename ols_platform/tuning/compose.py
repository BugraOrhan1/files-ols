"""A combination (e.g. Stage 1 + Vmax) from its separate tunes.

Used only when the library holds no example of the combination itself. Every
part is made on its own from the same input; the changes are joined when they
do not contradict each other. Two parts that want a different value for the
same 16-bit cell cannot be joined, and nothing is averaged or guessed.
"""

from __future__ import annotations

from typing import Iterable

# Extras that are independent functions next to a stage.
COMBINABLE = ("pop_bang", "vmax", "launch_control", "hardcut", "start_stop_off")


def parts_of(profile: str) -> list[str]:
    """The separate tunes of a combination that may be joined; else []."""

    parts = profile.split("+")
    if len(parts) < 2 or len(set(parts)) != len(parts):
        return []
    if any(part not in COMBINABLE and not part.startswith("stage") for part in parts):
        return []
    return parts if sum(part.startswith("stage") for part in parts) <= 1 else []


def changed_cells(original: bytes, output: bytes) -> dict[int, bytes]:
    return {i: output[i:i + 2] for i in range(0, len(original), 2) if original[i:i + 2] != output[i:i + 2]}


def join(original: bytes, outputs: Iterable[bytes]) -> tuple[bytes, list[int]]:
    """(joined image, cells that two parts set differently)."""

    result = bytearray(original)
    seen: dict[int, bytes] = {}
    conflicts: list[int] = []
    for output in outputs:
        if len(output) != len(original):
            raise ValueError("Delen met verschillende lengte.")
        for at, value in changed_cells(original, output).items():
            if seen.setdefault(at, value) != value:
                conflicts.append(at)
                continue
            result[at:at + len(value)] = value
    return bytes(result), conflicts


def offered(profiles: list[str]) -> list[str]:
    """Combinations of a stage with one or two extras that can be tried
    because their parts are available and no example exists yet."""

    stages = [p for p in profiles if p.startswith("stage") and "+" not in p]
    extras = [p for p in COMBINABLE if p in profiles]
    out = []
    for stage in stages:
        for i, first in enumerate(extras):
            out.append(f"{stage}+{first}")
            out.extend(f"{stage}+{first}+{second}" for second in extras[i + 1:])
    return [p for p in out if p not in profiles]
