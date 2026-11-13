import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tuning_fixtures import make_original, make_pair, make_stage1  # noqa: E402

from ols_platform.tuning.package import PackageStore, build_package  # noqa: E402

try:
    import tkinter as tk

    _root = tk.Tk()
    _root.destroy()
    HAVE_DISPLAY = True
except Exception:  # no tkinter or no display
    HAVE_DISPLAY = False


@unittest.skipUnless(HAVE_DISPLAY, "tkinter met scherm niet beschikbaar")
class AppTests(unittest.TestCase):
    def setUp(self):
        from ols_platform.tuning.app import TunerApp

        self.tmp = tempfile.TemporaryDirectory()
        self.root_dir = Path(self.tmp.name)
        self.store = PackageStore(self.root_dir / "store")
        self.tk = tk.Tk()
        self.tk.withdraw()
        self.app = TunerApp(self.tk, self.store)
        self.app.settings.data["output_dir"] = str(self.root_dir / "uit")

    def tearDown(self):
        self.tk.destroy()
        self.tmp.cleanup()

    def test_first_start_explains_what_to_do(self):
        self.assertIn("bibliotheek", self.app.status_text.lower())
        self.assertEqual(str(self.app.make_button["state"]), "disabled")

    def test_folders_are_added_with_duplicates_refused_and_removed(self):
        window = self.app.open_library()
        self.app.settings.data["sources"] = []
        folder = self.root_dir / "Database 1"
        folder.mkdir()
        window._add_source(str(folder))
        window._add_source(str(folder))
        window._add_source(str(self.root_dir / "Default"))
        self.assertEqual(len(self.app.settings.data["sources"]), 2)
        self.assertEqual(len(window.source_tree.get_children()), 2)
        window.source_tree.selection_set("src0")
        window.remove_folder()
        self.assertEqual(len(self.app.settings.data["sources"]), 1)
        window.restore_defaults()
        self.assertEqual(len(self.app.settings.data["sources"]), 6)
        for name in window.TABS:
            window.select(name)
            self.tk.update()

    def test_recognition_contract_and_export_flow(self):
        pair = make_pair(1)
        self.store.install(build_package([pair, make_pair(2, scale=0.95)]))
        self.app.reload_package()
        original = self.root_dir / "origineel ü.bin"
        original.write_bytes(pair.original)
        self.app.set_original(original)
        self.assertIn("checksum", self.app.status_text.lower())
        self.assertEqual(str(self.app.make_button["state"]), "disabled")
        self.store.set_contract("SYNECU-77|65536", "FLASH_TOOL_CORRECTS_ON_WRITE", "test-onderbouwing")
        self.app.check_original()
        self.assertEqual(str(self.app.make_button["state"]), "normal")
        output = self.app.make_bin(ask_path=False)
        self.assertEqual(output.read_bytes(), pair.tuned)
        self.assertTrue(os.path.exists(str(output) + ".rapport.json"))
        tuned = self.root_dir / "al getuned.bin"
        tuned.write_bytes(make_stage1(make_original(1)))
        self.app.set_original(tuned)
        self.assertIn("al getuned", self.app.status_text)
        self.assertEqual(str(self.app.make_button["state"]), "disabled")


if __name__ == "__main__":
    unittest.main()
