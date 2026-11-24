"""Faster lookups must give the same answers."""

import random
import unittest

from ols_platform.tuning import identity, transfer
from ols_platform.tuning.library import name_unknown_families


class TokenSearchTests(unittest.TestCase):
    def setUp(self):
        rng = random.Random(4)
        self.image = bytearray(rng.randbytes(300_000))
        self.image[1000:1006] = b"031471"
        self.image[999], self.image[1006] = 0, 0
        self.image[5000:5011] = b"8V0906259R "
        self.image[9000:9012] = b"\x00EDC17CP44\x00\x00"
        self.image[12000:12007] = b"9777123"

    def test_same_answers_as_searching_the_whole_image(self):
        image = bytes(self.image)
        small = len(image) + 1
        rng = random.Random(5)
        tokens = ["031471", "31471", "8V0906259R", "8V0906259", "EDC17CP44", "777123", "9777123", "AB", "x"]
        tokens += [image[i:i + 4].decode("latin-1") for i in (rng.randrange(0, 299_000) for _ in range(400))]
        fast = [identity.token_in(t, image) for t in tokens]
        old, identity._TEXT_MIN_IMAGE = identity._TEXT_MIN_IMAGE, small
        try:
            slow = [identity.token_in(t, image) for t in tokens]
        finally:
            identity._TEXT_MIN_IMAGE = old
        self.assertEqual(fast, slow)

    def test_another_image_is_not_answered_from_the_cache(self):
        image, other = bytes(self.image), random.Random(6).randbytes(300_000)
        self.assertTrue(identity.token_in("EDC17CP44", image))
        self.assertFalse(identity.token_in("EDC17CP44", other))
        self.assertTrue(identity.token_in("EDC17CP44", image))


class AnchorTests(unittest.TestCase):
    def test_full_match_and_unrelated(self):
        data = random.Random(1).randbytes(200_000)
        size = transfer.ANCHOR_SIZE
        blob = b"".join(data[i:i + size] for i in range(0, 64 * 1000, 1000))
        self.assertEqual(transfer.shifted_similarity(data, blob), 1.0)
        self.assertLess(transfer.shifted_similarity(random.Random(2).randbytes(200_000), blob), 0.1)


class MessageTests(unittest.TestCase):
    def test_unknown_ecu_gives_a_short_message(self):
        from ols_platform.tuning.generate import GenerationRejected, _select_variant

        class Package(dict):
            def variants(self):
                return []

        package = Package(families=[{"ecu_type": f"ECU{i}", "family_key": f"ECU{i}|1"} for i in range(6000)])
        with self.assertRaises(GenerationRejected) as ctx:
            _select_variant(package, bytes(100_000))
        self.assertEqual(ctx.exception.code, "ecu_not_recognized")
        self.assertIn("6000 ECU-typen", ctx.exception.message)
        self.assertLess(len(ctx.exception.message), 400)


class NamingTests(unittest.TestCase):
    def row(self, sha, family):
        return {"original_sha256": sha, "family_key": family}

    def test_name_is_taken_over_only_when_unambiguous(self):
        rows = [self.row("a", "onbekend|1"), self.row("a", "EDC17C46|1"), self.row("b", "UNKNOWN|1"),
                self.row("b", "MED17|1"), self.row("b", "MED17.5|1"), self.row("c", "onbekend|1")]
        self.assertEqual(name_unknown_families(rows), 1)
        self.assertEqual([r["family_key"] for r in rows],
                         ["EDC17C46|1", "EDC17C46|1", "UNKNOWN|1", "MED17|1", "MED17.5|1", "onbekend|1"])


if __name__ == "__main__":
    unittest.main()
