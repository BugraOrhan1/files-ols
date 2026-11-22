"""Self-contained WinOLS project reader for the tuning library.

Reads only what training needs, so the app can analyse an archive without
WinOLS and without the GPL py-ols parser:

* header strings (make, model, ..., ecu_type, sw_number, hw_number, hw_code)
  stored as length-prefixed cp1252 strings from offset 0x18;
* the binary-version record (class marker 0x42007899) that gives the base
  offset, blob size and number of version slots;
* version images at ``base + 4 + i * blob_size`` (verified against 14
  independent WinOLS exports, see TUNING_STATUS.md).

Format facts were cross-checked against py-ols' output on the repository
samples (tests/test_tuning_native.py); no parser code was copied.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"\x0b\x00\x00\x00WinOLS File\x00"
HEADER_FIELDS = (
    "make", "model", "engine", "year", "fuel_type", "displacement", "power",
    "gearbox", "memory_type", "ecu_manufacturer", "ecu_type", "sw_number",
    "hw_number", "hw_code", "user",
)
VERSION_MARKER = struct.pack("<I", 0x42007899)
IMAGE_SHIFT = 4


@dataclass
class NativeSlot:
    slot_index: int
    blob_offset: int
    blob_size: int
    raw_sha256: str = ""
    binary_status: str = "present"


@dataclass
class NativeSnapshot:
    format_version: int
    metadata: dict[str, str]
    version_slots: list[NativeSlot]
    maps: tuple = ()
    issues: list[str] = field(default_factory=list)


class NativeOlsReader:
    parser_name = "ols-tuner-native"
    parser_version = "1"

    def inspect(self, source: Path) -> NativeSnapshot:
        with source.open("rb") as handle:
            head = handle.read(0x4000)
            handle.seek(0, 2)
            size = handle.tell()
        return parse_header_and_layout(head, size)


def _string(data: bytes, pos: int) -> tuple[str | None, int]:
    if pos + 4 > len(data):
        return None, 0
    (length,) = struct.unpack_from("<I", data, pos)
    if length > 200:
        return None, 0
    raw = data[pos + 4:pos + 4 + length]
    if len(raw) != length:
        return None, 0
    return raw.decode("cp1252", errors="replace").rstrip("\x00").strip(), 4 + length


def parse_header_and_layout(head: bytes, file_size: int) -> NativeSnapshot:
    if not head.startswith(MAGIC):
        raise ValueError("Geen WinOLS-project (handtekening ontbreekt).")
    (format_version,) = struct.unpack_from("<H", head, 16)
    metadata: dict[str, str] = {}
    pos = 0x18
    for name in HEADER_FIELDS:
        value, consumed = _string(head, pos)
        if value is None:
            break
        metadata[name] = value
        pos += consumed
    issues: list[str] = []
    slots: list[NativeSlot] = []
    search = 0x100
    while True:
        marker = head.find(VERSION_MARKER, search, 0x2000)
        if marker < 0:
            issues.append("Geen binair versierecord gevonden.")
            break
        start = marker - 12
        search = marker + 4
        if start < 0:
            continue
        index, base, blob, _m, count = struct.unpack_from("<5I", head, start)
        if not (0 < count <= 20 and index < count and 0x10000 <= blob <= 0x10000000):
            continue
        if base + count * blob > file_size + 4:
            continue
        slots = [NativeSlot(i, base + i * blob, blob) for i in range(count)]
        break
    return NativeSnapshot(format_version, metadata, slots, (), issues)
