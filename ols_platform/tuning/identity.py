"""Input classification and ECU identification for raw read files.

Only facts derived from the bytes themselves (and identification tokens that
were learned from training originals) are used. File names are never trusted.
"""

from __future__ import annotations

import collections
import math
import re
from dataclasses import dataclass, field

# Identification fields from the WinOLS project metadata whose tokens are
# searched for in the binary. Their order is the order of the reasons shown.
IDENTITY_FIELDS = ("ecu_type", "hw_number", "hw_code", "sw_number", "sw_version")
_TOKEN_SPLIT = re.compile(r"[\s#()\[\],;/]+|(?<=[0-9A-Z])x(?=[A-Z0-9])")
_MIN_TOKEN = 6


@dataclass
class InputClassification:
    kind: str  # raw_image | ols_project | archive | encrypted | empty | text
    message: str
    entropy: float = 0.0
    details: dict = field(default_factory=dict)


def shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = collections.Counter(data)
    total = len(data)
    return -sum(c / total * math.log2(c / total) for c in counts.values())


def classify_input(data: bytes) -> InputClassification:
    if not data:
        return InputClassification("empty", "Het bestand is leeg.")
    if data[:16] == b"\x0b\x00\x00\x00WinOLS File\x00":
        return InputClassification(
            "ols_project",
            "Dit is een WinOLS-project (.ols), geen uitlezing. Open het "
            "originele uitleesbestand uit AutoTuner.",
        )
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06") or data[:6] == b"7z\xbc\xaf\x27\x1c" \
            or data[:4] == b"Rar!":
        return InputClassification(
            "archive",
            "Dit is een gecomprimeerde container (ZIP/7z/RAR), mogelijk "
            "hernoemd naar .bin. Pak eerst het uitleesbestand uit.",
        )
    sample = data if len(data) <= 8 * 1024 * 1024 else data[: 8 * 1024 * 1024]
    entropy = shannon_entropy(sample)
    if len(data) >= 4096 and entropy > 7.985:
        return InputClassification(
            "encrypted",
            "De inhoud is vrijwel volledig willekeurig (entropie "
            f"{entropy:.3f} bit/byte): waarschijnlijk een versleuteld "
            "AutoTuner Slave-bestand. Laat het via de gekoppelde Master "
            "ontsleutelen en open daarna het onversleutelde origineel.",
            entropy,
        )
    printable = sum(1 for b in sample[:65536] if 32 <= b < 127 or b in (9, 10, 13))
    if printable / max(1, min(len(sample), 65536)) > 0.97 and len(data) < 16 * 1024 * 1024:
        return InputClassification(
            "text", "Dit is een tekstbestand (bijv. CSV/HEX-export), geen binaire uitlezing.",
            entropy,
        )
    return InputClassification("raw_image", "Binaire uitlezing.", entropy)


_GENERIC = {"MEDC17", "EDC17C", "EDC16C", "SIMOS1", "DELPHI", "BOSCH", "SIEMENS", "CONTINENTAL",
            "MARELLI", "DENSO", "VISTEON", "ORIGINAL", "UNKNOWN", "EPROM", "FLASH",
            "12A650"}  # Ford's base part number of every engine ECU (e.g. GK21-12A650-xx)
_DIGITS = "01234567890123456789"


def _counting(token: str) -> bool:
    """0123456789, 12345678, 9876543210: a placeholder, not a number."""

    return token.isdigit() and (token in _DIGITS or token in _DIGITS[::-1])


def _useful(token: str) -> bool:
    """Identification tokens must look like part/software numbers: enough
    letters/digits, at least one digit, not one repeated character and not a
    generic family name that occurs in many unrelated files."""

    alnum = sum(ch.isalnum() for ch in token)
    return (alnum >= _MIN_TOKEN and alnum / len(token) >= 0.7 and any(ch.isdigit() for ch in token)
            and len(set(token)) >= 3 and token.upper() not in _GENERIC and not _counting(token))


_VAG_PART = re.compile(r"\d[A-Z0-9]{2}9\d{5}[A-Z]{0,3}")
_ALNUM = frozenset(b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


_TEXT_RUNS = re.compile(rb"[\x20-\x7e]{4,}")
_TEXT_MIN_IMAGE = 64 * 1024
_text_cache: tuple[bytes, bytes] | None = None


def _searchable(image: bytes, raw: bytes) -> bytes:
    """The image's printable text when ``raw`` can only occur there.

    Runs are separated by a zero byte, so a match and the characters around it
    are the same as in the image itself; thousands of numbers are looked up in
    every file, and the text is a small part of it."""

    global _text_cache
    if len(image) < _TEXT_MIN_IMAGE or len(raw) < 4 or not all(32 <= b < 127 for b in raw):
        return image
    cached = _text_cache
    if cached is None or cached[0] is not image:
        image = bytes(image) if not isinstance(image, bytes) else image
        cached = _text_cache = (image, b"\0".join(_TEXT_RUNS.findall(image)))
    return cached[1]


def token_in(token: str, image: bytes) -> bool:
    """True when ``token`` literally occurs in ``image``.

    VAG part numbers must occur as a whole number: '8V0906259' is NOT found in
    '8V0906259R' (another revision index, other software). Plain substring
    search made such prefixes match and selected unrelated variants."""

    raw = token.encode("latin-1", "ignore")
    if not raw:
        return False
    image = _searchable(image, raw)
    if token.isdigit():
        # Software numbers such as 031471 must not be part of a longer number.
        pos = image.find(raw)
        while pos >= 0:
            before = image[pos - 1] if pos > 0 else None
            after = image[pos + len(raw)] if pos + len(raw) < len(image) else None
            if not (before is not None and 48 <= before <= 57) and not (after is not None and 48 <= after <= 57):
                return True
            pos = image.find(raw, pos + 1)
        return False
    if not _VAG_PART.fullmatch(token.upper()):
        return raw in image
    pos = image.find(raw)
    while pos >= 0:
        before = image[pos - 1] if pos > 0 else None
        after = image[pos + len(raw)] if pos + len(raw) < len(image) else None
        if before not in _ALNUM and after not in _ALNUM:
            return True
        pos = image.find(raw, pos + 1)
    return False


def tokens_for(value: str) -> list[str]:
    tokens = []
    for token in _TOKEN_SPLIT.split(value):
        token = token.strip("-_.!?*")
        if _useful(token) and token not in tokens:
            tokens.append(token)
    return tokens


def learn_identity(metadata: dict[str, str], image: bytes) -> dict[str, list[str]]:
    """Return per identity field the tokens that literally occur in ``image``."""

    found: dict[str, list[str]] = {}
    for field_name in IDENTITY_FIELDS:
        value = metadata.get(field_name, "")
        present = [t for t in tokens_for(value) if token_in(t, image)]
        if present:
            found[field_name] = present
    return found


def match_identity(identity: dict[str, list[str]], image: bytes) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for field_name, tokens in identity.items():
        hit = [t for t in tokens if token_in(t, image)]
        miss = [t for t in tokens if t not in hit]
        result[field_name] = {"found": hit, "missing": miss}
    return result
