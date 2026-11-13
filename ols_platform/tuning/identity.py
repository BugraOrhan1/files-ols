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


def tokens_for(value: str) -> list[str]:
    tokens = []
    for token in _TOKEN_SPLIT.split(value):
        token = token.strip("-_.")
        if len(token) >= _MIN_TOKEN and token not in tokens:
            tokens.append(token)
    return tokens


def learn_identity(metadata: dict[str, str], image: bytes) -> dict[str, list[str]]:
    """Return per identity field the tokens that literally occur in ``image``."""

    found: dict[str, list[str]] = {}
    for field_name in IDENTITY_FIELDS:
        value = metadata.get(field_name, "")
        present = [t for t in tokens_for(value) if t.encode("latin-1", "ignore") in image]
        if present:
            found[field_name] = present
    return found


def match_identity(identity: dict[str, list[str]], image: bytes) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for field_name, tokens in identity.items():
        hit = [t for t in tokens if t.encode("latin-1", "ignore") in image]
        miss = [t for t in tokens if t not in hit]
        result[field_name] = {"found": hit, "missing": miss}
    return result
