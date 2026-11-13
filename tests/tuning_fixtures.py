"""Synthetic ECU images for tuning regression tests (no customer data)."""

from __future__ import annotations

import hashlib
import random
import struct
from dataclasses import dataclass
from pathlib import Path

from ols_platform.tuning.identity import learn_identity
from ols_platform.tuning.learn import TrainingPair

SIZE = 64 * 1024
MAPS = {  # offset: (columns, rows, base, stage1 factor)
    0x4000: (8, 8, 1200, 1.12),
    0x5000: (10, 6, 800, 1.08),
    0x6000: (6, 6, 2500, 1.15),
}
SWITCH_OFFSET = 0x7000
ID_OFFSETS = {"ecu_type": 0x100, "hw_number": 0x140, "sw_number": 0x180}


@dataclass
class Variant:
    ecu: str = "SYNECU-77"
    hw: str = "HW0815-4711"
    sw: str = "SWALPHA-0001"


def make_original(seed: int, variant: Variant | None = None, calibration_scale: float = 1.0,
                  map_shift: int = 0) -> bytes:
    variant = variant or Variant()
    rng = random.Random(seed if map_shift == 0 else seed)
    # "Code" area: deterministic per SW variant, low entropy pseudo instructions.
    code_rng = random.Random(variant.sw)
    image = bytearray(code_rng.choice(b"\x00\x01\x02\x10\x20\x40\x80\xff\x3c\x4d") for _ in range(SIZE))
    for field, offset in ID_OFFSETS.items():
        text = {"ecu_type": variant.ecu, "hw_number": variant.hw, "sw_number": variant.sw}[field]
        image[offset:offset + len(text)] = text.encode()
    for offset, (cols, rows, base, _factor) in MAPS.items():
        offset += map_shift
        # axis header
        for i in range(cols):
            struct.pack_into("<H", image, offset - 2 * cols - 2 * rows + 2 * i, 500 + 250 * i)
        for j in range(rows):
            struct.pack_into("<H", image, offset - 2 * rows + 2 * j, 10 + 10 * j)
        for j in range(rows):
            for i in range(cols):
                value = base * calibration_scale * (1 + 0.04 * i + 0.03 * j) * (1 + rng.uniform(-0.01, 0.01))
                struct.pack_into("<H", image, offset + 2 * (j * cols + i), int(value))
    image[SWITCH_OFFSET] = 0x00
    return bytes(image)


def make_stage1(original: bytes, map_shift: int = 0) -> bytes:
    image = bytearray(original)
    for offset, (cols, rows, _base, factor) in MAPS.items():
        offset += map_shift
        for k in range(cols * rows):
            (value,) = struct.unpack_from("<H", image, offset + 2 * k)
            struct.pack_into("<H", image, offset + 2 * k, min(0xFFFF, round(value * factor)))
    image[SWITCH_OFFSET] = 0x01
    return bytes(image)


def metadata(variant: Variant | None = None) -> dict[str, str]:
    variant = variant or Variant()
    return {"ecu_type": variant.ecu, "hw_number": variant.hw, "sw_number": variant.sw,
            "make": "Test", "model": "Synth"}


def make_pair(seed: int, variant: Variant | None = None, scale: float = 1.0,
              profile: str = "stage1") -> TrainingPair:
    variant = variant or Variant()
    original = make_original(seed, variant, scale)
    tuned = make_stage1(original)
    identity = learn_identity(metadata(variant), original)
    o_sha = hashlib.sha256(original).hexdigest()
    t_sha = hashlib.sha256(tuned).hexdigest()
    pair_id = hashlib.sha256(f"{o_sha}:{t_sha}".encode()).hexdigest()[:16]
    return TrainingPair(
        pair_id=pair_id,
        family_key=f"{variant.ecu}|{SIZE}",
        variant_key=f"hw_number={variant.hw}|sw_number={variant.sw}",
        identity=identity, original=original, tuned=tuned, profile=profile,
        record={"project_sha256": f"proj{seed:04d}", "project_name": f"synthetic-{seed}.ols",
                "original_sha256": o_sha, "tuned_sha256": t_sha, "checksum_state": "NOT_CORRECTED_IN_REFERENCE",
                "ecu_type": variant.ecu, "tuned_version_name": "Stage 1"},
    )


def revision_tag_bytes(names: list[tuple[str, str]]) -> bytes:
    inner = ",\t ".join(["Original"] + [f"{n}:\t {note}" for n, note in names])
    text = f"{len(names)} ({inner})".encode("latin-1")
    return struct.pack("<I", len(text)) + text


def write_fake_project(path: Path, images: list[bytes], names: list[tuple[str, str]]) -> dict:
    """Write an OLS-like container: header + revision tag + fixed-stride slots.

    Returns the slot layout that :class:`FakeReader` reports for it.
    """

    header = b"\x0b\x00\x00\x00WinOLS File\x00" + b"\x00" * 64 + revision_tag_bytes(names)
    header += b"\x00" * (4096 - len(header) % 4096)
    size = len(images[0])
    base = len(header) - 4  # images start at blob_offset + 4
    body = bytearray(header)
    for image in images:
        body += image
    body += b"\x00" * 4
    path.write_bytes(bytes(body))
    return {"base": base, "size": size, "count": len(images)}


class _Slot:
    def __init__(self, index, offset, size):
        self.slot_index = index
        self.blob_offset = offset
        self.blob_size = size
        self.raw_sha256 = ""
        self.binary_status = "present_nonempty_unverified"


class _Snapshot:
    def __init__(self, meta, slots):
        self.metadata = meta
        self.version_slots = slots
        self.maps = ()


class FakeReader:
    """Stands in for the py-ols adapter on synthetic project files."""

    parser_name = "fake"
    parser_version = "1"

    def __init__(self) -> None:
        self.layouts: dict[str, tuple[dict, dict]] = {}
        self.fail_paths: dict[str, int] = {}

    # Keyed by content: the library parses a local copy, not the source path.
    @staticmethod
    def _key(path: Path) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def register(self, path: Path, layout: dict, meta: dict) -> None:
        self.layouts[self._key(path)] = (layout, meta)

    def fail(self, path: Path, times: int) -> None:
        self.fail_paths[self._key(path)] = times

    def inspect(self, source: Path):
        key = self._key(source)
        remaining = self.fail_paths.get(key, 0)
        if remaining:
            self.fail_paths[key] = remaining - 1
            raise OSError("netwerkpad tijdelijk niet beschikbaar")
        layout, meta = self.layouts[key]
        slots = [_Slot(i, layout["base"] + i * layout["size"], layout["size"]) for i in range(layout["count"])]
        return _Snapshot(meta, slots)
