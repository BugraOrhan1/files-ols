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
        self.app = TunerApp(self.tk, self.store, synchronous=True)
        self.app.settings.data["output_dir"] = str(self.root_dir / "uit")

    def tearDown(self):
        self.app.close()
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
        self.assertTrue(self.app.checksum_button.winfo_manager())  # button offered
        from unittest import mock

        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            self.app.confirm_checksum()
        self.assertEqual(self.store.contracts()["SYNECU-77|65536"]["contract"], "FLASH_TOOL_CORRECTS_ON_WRITE")
        self.assertFalse(self.app.checksum_button.winfo_manager())
        self.assertEqual(str(self.app.make_button["state"]), "normal")
        output = self.app.make_bin(ask_path=False)
        self.assertEqual(output.read_bytes(), pair.tuned)
        # Explanation window: candidates with separate statuses, filterable.
        from ols_platform.tuning.app import ExplainWindow

        window = ExplainWindow(self.app, original, self.app.package)
        self.assertEqual(window.report["candidates"][0]["identity_status"], "IDENTICAL_CONTENT")
        self.assertIn("identiteit IDENTICAL_CONTENT", window.text.get("1.0", "end"))
        window.filter_var.set("Tegenstrijdig")
        window.render()
        self.assertNotIn("identiteit IDENTICAL_CONTENT", window.text.get("1.0", "end"))
        window.win.destroy()
        self.assertTrue(os.path.exists(str(output) + ".rapport.json"))
        tuned = self.root_dir / "al getuned.bin"
        tuned.write_bytes(make_stage1(make_original(1)))
        self.app.set_original(tuned)
        self.assertIn("al bewerkt", self.app.status_text)
        self.assertEqual(str(self.app.make_button["state"]), "disabled")
        # Everything above is in the app log; the diagnostics bundle holds no customer bytes.
        log = (self.store.root / "app.log").read_text(encoding="utf-8")
        self.assertIn("Origineel geopend", log)
        self.assertIn("already_tuned", log)
        self.assertIn("Checksumcontract vastgelegd", log)
        import zipfile
        from unittest import mock

        target = self.root_dir / "diag.zip"
        with mock.patch("tkinter.filedialog.asksaveasfilename", return_value=str(target)), \
                mock.patch("tkinter.messagebox.showinfo"):
            self.app.save_diagnostics()
        with zipfile.ZipFile(target) as archive:
            names = set(archive.namelist())
            self.assertIn("app.log", names)
            self.assertIn("modelpakket.json", names)
            self.assertFalse(any(n.startswith(("pairs", "originals", "packages")) for n in names))
            self.assertNotIn(pair.original[0x4000:0x4040], b"".join(archive.read(n) for n in names))


if __name__ == "__main__":
    unittest.main()


class FileIoTests(unittest.TestCase):
    def test_replace_is_retried_while_another_process_holds_the_file(self):
        from unittest import mock

        from ols_platform.tuning import fileio

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "status.json"
            real_replace = os.replace
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] <= 3:
                    raise PermissionError(32, "in gebruik door een ander proces")
                return real_replace(src, dst)

            with mock.patch.object(fileio.os, "replace", flaky):
                fileio.write_json(target, {"state": "running"})
            self.assertEqual(fileio.read_json(target), {"state": "running"})
            self.assertEqual(calls["n"], 4)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["status.json"])  # no leftovers

    def test_background_status_write_never_raises(self):
        from unittest import mock

        from ols_platform.tuning import background, fileio

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(fileio.os, "replace", side_effect=PermissionError(32, "bezet")), \
                    mock.patch.object(fileio, "DELAY", 0):
                background._write_status(Path(tmp), {"state": "running"})  # must not raise


@unittest.skipUnless(HAVE_DISPLAY, "tkinter met scherm niet beschikbaar")
class ResponsiveAppTests(unittest.TestCase):
    def test_recognition_runs_in_background_and_window_stays_responsive(self):
        import time

        from ols_platform.tuning.app import TunerApp

        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp)
            store = PackageStore(root_dir / "store")
            pair = make_pair(1)
            store.install(build_package([pair]))
            window = tk.Tk()
            window.withdraw()
            app = TunerApp(window, store)  # asynchronous, as in the real app
            try:
                original = root_dir / "origineel.bin"
                original.write_bytes(pair.original)
                app.set_original(original)
                self.assertTrue("wordt herkend" in app.status_text or "wordt geladen" in app.status_text,
                                app.status_text)  # returned immediately, work continues in the background
                deadline = time.time() + 30
                while app.result is None and time.time() < deadline:
                    window.update()
                    time.sleep(0.02)
                self.assertIsNotNone(app.result)
                self.assertEqual(app.result.output, pair.tuned)
            finally:
                app.close()
                window.destroy()
