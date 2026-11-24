"""Checksum and signature bytes are no calibration changes."""

import random
import struct
import unittest

from ols_platform.tuning.transfer import is_integrity_field, split_integrity


def noise(seed, size):
    return random.Random(seed).randbytes(size)


class IntegrityFieldTests(unittest.TestCase):
    def test_signature_at_a_block_end(self):
        self.assertTrue(is_integrity_field(0x1BFF74, noise(1, 140), noise(2, 140)))
        self.assertTrue(is_integrity_field(0x1FDF78, noise(3, 136), noise(4, 136)))

    def test_checksum_at_a_block_end(self):
        self.assertTrue(is_integrity_field(0x7FFC, noise(1, 4), noise(2, 4)))
        self.assertFalse(is_integrity_field(0x7000, noise(1, 4), noise(2, 4)))

    def test_a_table_is_never_a_signature(self):
        table = struct.pack(">64H", *range(1000, 1640, 10))
        self.assertFalse(is_integrity_field(0x1BFF80, table, struct.pack(">64H", *range(1200, 1840, 10))))

    def test_random_bytes_elsewhere_are_not_skipped(self):
        self.assertFalse(is_integrity_field(0x1BF123, noise(1, 140), noise(2, 140)))

    def test_partly_changed_block_is_not_skipped(self):
        data = noise(1, 140)
        self.assertFalse(is_integrity_field(0x1BFF74, data, noise(2, 60) + data[60:]))

    def test_split_keeps_real_changes_and_present_findings(self):
        sig = [0x1BFF74, noise(1, 140).hex(), noise(2, 140).hex()]
        table = [0x5000, struct.pack(">8H", *range(8)).hex(), struct.pack(">8H", *range(5, 13)).hex()]
        missing = [{"start": sig[0], "length": 140}, {"start": 0x5000, "length": 16},
                   {"start": sig[0], "length": 140, "present": True}]
        real, fields = split_integrity(missing, [sig, table])
        self.assertEqual(real, missing[1:])
        self.assertEqual(fields, missing[:1])


if __name__ == "__main__":
    unittest.main()
