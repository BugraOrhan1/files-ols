"""Version names of older WinOLS projects and small memories."""

import struct
import unittest

from ols_platform.tuning.ols_native import MAGIC, VERSION_MARKER, parse_header_and_layout
from ols_platform.tuning.ols_versions import parse_revision_tag


def head(tag: bytes) -> bytes:
    return b"\0" * 64 + struct.pack("<I", len(tag)) + tag + b"\0" * 64


class OldTagTests(unittest.TestCase):
    def names(self, tag):
        found = parse_revision_tag(head(tag))
        return found and [v.name for v in found.versions]

    def test_names_without_tabs(self):
        self.assertEqual(self.names(b"2 (Original, Stage1, Stage2)"), ["Original", "Stage1", "Stage2"])

    def test_notes_are_kept_apart(self):
        tag = parse_revision_tag(head(b"1 (Original:\t ohne CS,\t Stage1:\t ohne CS)"))
        self.assertEqual([(v.name, v.checksum_note) for v in tag.versions],
                         [("Original", "ohne CS"), ("Stage1", "ohne CS")])

    def test_ambiguous_comma_is_refused(self):
        self.assertIsNone(self.names(b"1 (Original, Stage1, Vmax)"))

    def test_text_that_is_not_a_tag_is_refused(self):
        self.assertIsNone(parse_revision_tag(b"\0" * 32 + b"1 (Original, Stage1)" + b"\0" * 32))


class OldHeaderTests(unittest.TestCase):
    def test_terminated_header_strings(self):
        values = ["VW", "Golf", "", "2009", "", "", "", "", "Eprom", "Bosch", "EDC17CP14", "03L997025H", "", "", ""]
        for version, end in ((397, b"\0"), (503, b"")):
            data = bytearray(MAGIC)
            data += b"\0" * (0x18 - len(data))
            struct.pack_into("<H", data, 16, version)
            for v in values:
                data += struct.pack("<I", len(v)) + v.encode() + (end if v else b"")
            meta = parse_header_and_layout(bytes(data) + b"\0" * 64, 0x1000).metadata
            self.assertEqual(list(meta.values()), values, version)


class SmallMemoryTests(unittest.TestCase):
    def layout(self, blob):
        data = bytearray(0x2000)
        data[:len(MAGIC)] = MAGIC
        struct.pack_into("<3I", data, 0x400, 0, 0x2000, blob)
        data[0x40C:0x410] = VERSION_MARKER
        struct.pack_into("<I", data, 0x410, 2)
        return parse_header_and_layout(bytes(data), 0x2000 + 2 * blob).version_slots

    def test_32_kb_is_read_and_tiny_is_not(self):
        self.assertEqual([s.blob_size for s in self.layout(0x8000)], [0x8000, 0x8000])
        self.assertEqual(self.layout(0x1000), [])


if __name__ == "__main__":
    unittest.main()
