"""Self-test that runs inside the packaged EXE (synthetic data only)."""

from __future__ import annotations

import hashlib
import json
import random
import struct
import tempfile
from pathlib import Path

from .export import ExportBlocked, export_result
from .generate import GenerationRejected, generate
from .identity import learn_identity
from .learn import TrainingPair
from .package import PackageStore, build_package


def _image(seed: int, scale: float) -> bytes:
    rng = random.Random(seed)
    code = random.Random("SELFTEST-SW")
    data = bytearray(code.choice(b"\x00\x01\x10\x20\xff\x3c") for _ in range(32768))
    data[0x100:0x100 + 12] = b"SELFTEST-ECU"
    data[0x140:0x140 + 12] = b"SELFTEST-SW1"
    for k in range(64):
        struct.pack_into("<H", data, 0x4000 + 2 * k, int(1000 * scale * (1 + 0.01 * k) * (1 + rng.uniform(-0.005, 0.005))))
    return bytes(data)


def _tune(data: bytes) -> bytes:
    out = bytearray(data)
    for k in range(64):
        (v,) = struct.unpack_from("<H", out, 0x4000 + 2 * k)
        struct.pack_into("<H", out, 0x4000 + 2 * k, round(v * 1.1))
    return bytes(out)


def run() -> dict:
    meta = {"ecu_type": "SELFTEST-ECU", "sw_number": "SELFTEST-SW1"}
    pairs = []
    for seed, scale in ((1, 1.0), (2, 0.95), (3, 1.05)):
        o = _image(seed, scale)
        t = _tune(o)
        pid = hashlib.sha256(o + t).hexdigest()[:16]
        pairs.append(TrainingPair(pid, "SELFTEST-ECU|32768", "sw", learn_identity(meta, o), o, t, "stage1",
                                  record={"project_sha256": pid, "checksum_state": "UNKNOWN"}))
    package = build_package(pairs)
    checks = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pad met spatie ü"
        store = PackageStore(root / "store")
        store.install(package)
        store.set_contract("SELFTEST-ECU|32768", "FLASH_TOOL_CORRECTS_ON_WRITE", "zelftest contract")
        original = root / "origineel.bin"
        original.write_bytes(_image(9, 1.02))
        result = generate(original.read_bytes(), "stage1", store.load_active(), store.contracts())
        out, report = export_result(result, original, root / "uit", "stage1.bin", package)
        checks["export"] = json.loads(report.read_text(encoding="utf-8"))["verification"]["passed"]
        try:
            generate(_tune(_image(9, 1.02)), "stage1", package)
            checks["already_tuned_rejected"] = False
        except GenerationRejected as exc:
            checks["already_tuned_rejected"] = exc.code == "already_tuned"
        try:
            export_result(result, original, root / "uit", "stage1.bin", package)
            checks["no_overwrite"] = False
        except ExportBlocked as exc:
            checks["no_overwrite"] = exc.code == "exists"
    checks["passed"] = all(checks.values())
    return checks
