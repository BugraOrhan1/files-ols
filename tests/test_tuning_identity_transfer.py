"""Same software in another read form (e.g. 4 MB OBD read vs 5.5 MB bench
read of a Simos18.10): the reference's part numbers occur literally in the
input while whole-image similarity is low. Regression for a Seat Leon 5F
Simos18.10 (8V0906259R) that WinOLS matched but the app refused."""

import hashlib
import random
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tuning_fixtures import MAPS, SWITCH_OFFSET, Variant, make_original, make_stage1, metadata  # noqa: E402

from ols_platform.tuning.generate import GenerationRejected, _select_variant, generate  # noqa: E402
from ols_platform.tuning.identity import learn_identity, token_in  # noqa: E402
from ols_platform.tuning.learn import TrainingPair  # noqa: E402
from ols_platform.tuning.package import build_package  # noqa: E402

LEON = Variant(ecu="SIMOS18.10", hw="SCG00A1000000", sw="8V0906259R")


def _pair(original: bytes, tuned: bytes, variant: Variant, seed: int) -> TrainingPair:
    o_sha = hashlib.sha256(original).hexdigest()
    t_sha = hashlib.sha256(tuned).hexdigest()
    return TrainingPair(
        pair_id=hashlib.sha256(f"{o_sha}:{t_sha}".encode()).hexdigest()[:16],
        family_key=f"{variant.ecu}|{len(original)}",
        variant_key=f"hw_code={variant.hw}|sw_number={variant.sw}",
        identity=learn_identity(metadata(variant), original), original=original, tuned=tuned,
        profile="stage1",
        record={"project_sha256": f"proj{seed:04d}", "project_name": f"leon-{seed}.ols",
                "original_sha256": o_sha, "tuned_sha256": t_sha,
                "checksum_state": "NOT_CORRECTED_IN_REFERENCE", "ecu_type": variant.ecu,
                "tuned_version_name": "Stage 1", "vehicle": "Seat Leon 2.0 TSI"},
    )


def _other_code(image: bytes, seed: int = 3) -> bytes:
    """Same calibration and identification, different code bytes."""

    data = bytearray(image)
    keep = {i for offset, (c, r, _b, _f) in MAPS.items() for i in range(offset - 96, offset + 2 * c * r + 96)}
    keep |= set(range(SWITCH_OFFSET - 32, SWITCH_OFFSET + 32)) | set(range(0x100, 0x200))
    rng = random.Random(seed)
    for p in range(len(data)):
        if p not in keep and rng.random() < 0.5:
            data[p] = rng.randrange(256)
    return bytes(data)


class TokenBoundaryTests(unittest.TestCase):
    def test_vag_part_number_needs_whole_number(self):
        self.assertTrue(token_in("8V0906259R", b"\x00\x008V0906259R  0005\x00"))
        self.assertFalse(token_in("8V0906259", b"\x00\x008V0906259R  0005\x00"))
        self.assertTrue(token_in("8V0906259", b"\x00 8V0906259 \x00"))
        # non-VAG tokens keep plain literal search
        self.assertTrue(token_in("SCG00A1000000", b"xxSCG00A1000000yy"))

    def test_prefix_part_number_does_not_select_other_variant(self):
        other = Variant(ecu="SIMOS12", hw="SC100CC500000", sw="8V0906259")
        lib = make_original(11, other)
        package = build_package([_pair(lib, make_stage1(lib), other, 11)])
        with self.assertRaises(GenerationRejected) as ctx:
            _select_variant(package, make_original(12, LEON))
        self.assertNotIn("SC100CC500000", ctx.exception.message)


class IdentityConfirmedTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = random.Random(9)
        cls.prefix = bytes(rng.randrange(256) for _ in range(0x20000))  # extra blocks of the bench read
        cls.body = make_original(21, LEON)
        bench = cls.prefix + cls.body
        cls.package = build_package([_pair(bench, cls.prefix + make_stage1(cls.body), LEON, 21)])

    def test_obd_read_tuned_from_bench_read_of_same_software(self):
        obd = _other_code(self.body)  # smaller read, little whole-image similarity
        result = generate(obd, "stage1", self.package)
        self.assertEqual(result.evidence_level, "DONOR_TRANSFER")
        self.assertEqual(result.output, make_stage1(obd))
        self.assertEqual(result.identity["coverage"], 1.0)

    def test_values_differ_needs_more_than_identity(self):
        obd = bytearray(_other_code(self.body))
        first = min(MAPS)
        for k in range(8):  # other calibration values: would need recalculation
            value = struct.unpack_from("<H", obd, first + 2 * k)[0]
            struct.pack_into("<H", obd, first + 2 * k, value + 37)
        with self.assertRaises(GenerationRejected):
            generate(bytes(obd), "stage1", self.package)

    def test_other_software_is_not_identity_confirmed(self):
        other = Variant(ecu="SIMOS18.10", hw="SCG00A1000000", sw="8V0906259S")
        with self.assertRaises(GenerationRejected):
            generate(_other_code(make_original(21, other)), "stage1", self.package)


GOLF = Path(__file__).resolve().parents[1] / "ols files geextracteert"


@unittest.skipUnless((GOLF / "WinOLS (VW Golf (Original) - SCG0051000000)").exists(), "Golf-voorbeeld ontbreekt")
class RealSimos1810Tests(unittest.TestCase):
    """Real VW Golf Simos18.10 bench read (5.5 MB) as library reference; a
    4 MB read holding the same calibration elsewhere must get exactly
    WinOLS' Stage 1."""

    def test_obd_read_gets_winols_stage1(self):
        o = (GOLF / "WinOLS (VW Golf (Original) - SCG0051000000)").read_bytes()
        t = (GOLF / "WinOLS (VW Golf (Stage 1) - SCG0051000000)").read_bytes()
        variant = Variant(ecu="SIMOS18.10", hw="SCG0051000000", sw="5G0906259N")
        package = build_package([_pair(o, t, variant, 31)])
        rng = random.Random(1)
        obd = bytearray(rng.randrange(256) for _ in range(0x400000))
        obd[0x100000:0x1C0000] = o[0x200000:0x2C0000]
        expected = bytearray(obd)
        expected[0x100000:0x1C0000] = t[0x200000:0x2C0000]
        result = generate(bytes(obd), "stage1", package)
        self.assertEqual(result.output, bytes(expected))


S5_OTHER_HW = Variant(ecu="MG1CS002", hw="8W0907559G", sw="031471")
S5_INPUT = Variant(ecu="MG1CS002", hw="80A907559B", sw="031471")


class SameSoftwareOtherHardwareTests(unittest.TestCase):
    """Audi S5 B9 MG1CS002: WinOLS shows files with hardware 80A907559B,
    8W0907559G and 06M907309D that all carry software 031471/0005 with the
    same calibration. A tune of one must transfer to the others."""

    @classmethod
    def setUpClass(cls):
        rng = random.Random(4)
        cls.prefix = bytes(rng.randrange(256) for _ in range(0x20000))
        cls.body = make_original(41, S5_OTHER_HW)
        bench = cls.prefix + cls.body
        cls.package = build_package([_pair(bench, cls.prefix + make_stage1(cls.body), S5_OTHER_HW, 41)])

    def _input(self, variant):
        data = bytearray(_other_code(self.body))
        pos = data.find(S5_OTHER_HW.hw.encode())
        data[pos:pos + len(variant.hw)] = variant.hw.encode()
        return bytes(data)

    def test_same_software_other_hardware_transfers_exactly(self):
        data = self._input(S5_INPUT)
        result = generate(data, "stage1", self.package)
        self.assertEqual(result.output, make_stage1(data))
        self.assertTrue(any("ander hardwarenummer" in n for n in result.notes))

    def test_software_number_must_be_whole(self):
        self.assertFalse(token_in("031471", b"x1031471x"))
        self.assertTrue(token_in("031471", b"x 031471 0005"))

    def test_other_software_number_is_refused(self):
        data = bytearray(self._input(S5_INPUT))
        pos = data.find(b"031471")
        data[pos:pos + 6] = b"041612"
        with self.assertRaises(GenerationRejected):
            generate(bytes(data), "stage1", self.package)


class LargerReadContainmentTests(unittest.TestCase):
    """Library holds a larger read (e.g. 8 MB) with unknown identity tokens;
    the input is a smaller read holding the same calibration."""

    def test_larger_read_donor_used_with_identical_bytes_only(self):
        body = make_original(51, Variant(ecu="X", hw="Y", sw="Z"))  # tokens too short to be used
        rng = random.Random(6)
        tail = bytes(rng.randrange(256) for _ in range(len(body)))
        package = build_package([_pair(body + tail, make_stage1(body) + tail, Variant(), 51)])
        result = generate(body, "stage1", package)
        self.assertEqual(result.output, make_stage1(body))
        changed = bytearray(body)
        first = min(MAPS)
        for k in range(8):
            struct.pack_into("<H", changed, first + 2 * k, struct.unpack_from("<H", changed, first + 2 * k)[0] + 41)
        with self.assertRaises(GenerationRejected):
            generate(bytes(changed), "stage1", package)


if __name__ == "__main__":
    unittest.main()
