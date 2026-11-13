import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tuning_fixtures import Variant, make_original, make_stage1, metadata, write_native_project  # noqa: E402

from ols_platform.tuning.background import read_status, start_background  # noqa: E402
from ols_platform.tuning.generate import available_profiles, generate  # noqa: E402
from ols_platform.tuning.library import Library  # noqa: E402
from ols_platform.tuning.ols_native import NativeOlsReader  # noqa: E402
from ols_platform.tuning.package import PackageStore  # noqa: E402


def egr(original: bytes) -> bytes:
    image = bytearray(original)
    image[0x7400:0x7402] = b"\x00\x00"
    return bytes(image)


def build_archive(folder: Path, projects: int, variants: int = 3) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(projects):
        variant = Variant(sw=f"SWSYN-{n % variants:04d}")
        original = make_original(n, variant, calibration_scale=0.9 + 0.01 * (n % 15))
        write_native_project(
            folder / f"project {n:03d}.ols",
            [original, make_stage1(original), egr(original)],
            [("Stage 1", "NOCS"), ("EGR OFF", "NOCS")],
            metadata(variant),
        )


class ParallelLibraryTests(unittest.TestCase):
    def test_parallel_scan_learns_all_profiles_and_stop_keeps_what_was_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_archive(root / "Database 1", 30)
            store = PackageStore(root / "store")
            library = Library(store)
            stop = threading.Event()
            seen = []

            def on_file(source, done, found):
                seen.append(done)
                if done >= 10:
                    stop.set()

            first = library.scan({"Database 1": root / "Database 1"}, NativeOlsReader(), workers=2,
                                 stop=stop, on_file=on_file)
            self.assertEqual(first["sources"]["Database 1"]["status"], "interrupted")
            scanned = library.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            self.assertGreaterEqual(scanned, 10)
            self.assertLess(scanned, 30)
            partial = library.train()
            self.assertEqual(partial["pairs_used"], scanned * 2)  # stage1 + egr per project
            self.assertFalse((root / "store" / "scratch").exists())  # local disk: no copies
            second = library.scan({"Database 1": root / "Database 1"}, NativeOlsReader(), workers=2)
            self.assertEqual(second["sources"]["Database 1"]["skipped_unchanged"], scanned)
            full = library.train()
            self.assertEqual(full["pairs_used"], 60)
            self.assertEqual(full["profiles"], {"stage1": 30, "egr_off": 30})
            self.assertGreaterEqual(full["variants_reused_from_cache"], 0)
            library.close()
            package = store.load_active()
            original = make_original(999, Variant(sw="SWSYN-0001"), calibration_scale=1.0)
            _f, _v, profiles = available_profiles(original, package)
            self.assertEqual(profiles, ["stage1", "egr_off"])
            self.assertEqual(generate(original, "egr_off", package).output, egr(original))


class BackgroundWorkerTests(unittest.TestCase):
    def test_background_update_runs_detached_and_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_archive(root / "Database 1", 12)
            store_root = root / "store"
            store_root.mkdir()
            (store_root / "settings.json").write_text(json.dumps({
                "sources": [f"Database 1={root / 'Database 1'}", f"Weg={root / 'bestaat niet'}"],
            }), encoding="utf-8")
            self.assertTrue(start_background(store_root))
            self.assertFalse(start_background(store_root))  # second start is refused
            deadline = time.time() + 120
            while time.time() < deadline:
                status = read_status(store_root)
                if not status["running"] and status.get("state") in ("finished", "failed", "stopped"):
                    break
                time.sleep(0.5)
            self.assertEqual(status["state"], "finished", status.get("log"))
            self.assertTrue(any("niet bereikbaar" in line for line in status["log"]))
            package = PackageStore(store_root).load_active()
            self.assertIsNotNone(package)
            self.assertEqual(len(package["training_selection"]), 24)


if __name__ == "__main__":
    unittest.main()
