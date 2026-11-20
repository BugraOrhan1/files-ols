import gc
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


class LibraryUpgradeTests(unittest.TestCase):
    def test_projects_without_identity_old_labels_and_rescans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Database 1"
            folder.mkdir()
            variant = Variant(sw="SWNOID-0001")
            original = make_original(5, variant)
            meta = dict(metadata(variant), sw_number="NOT-IN-BYTES-123", hw_number="ALSO-NOT-456",
                        ecu_type="NOPE-789")
            write_native_project(folder / "zonder id.ols", [original, make_stage1(original)],
                                 [("Stage 1", "NOCS")], meta)
            store = PackageStore(root / "store")
            library = Library(store)
            # Simulate a library from an older version: this file was rejected before.
            library.db.execute("INSERT INTO files(path,source,size,mtime_ns,status,message,attempts,updated_at,"
                               "scan_version) VALUES(?,?,?,?,?,?,?,?,1)",
                               (str(folder / "zonder id.ols"), "Database 1",
                                (folder / "zonder id.ols").stat().st_size,
                                (folder / "zonder id.ols").stat().st_mtime_ns, "rejected", "oud", 1, "x"))
            library.db.commit()
            summary = library.scan({"Database 1": folder}, NativeOlsReader(), workers=1)
            self.assertEqual(summary["sources"]["Database 1"]["skipped_unchanged"], 0)  # looked at again
            self.assertEqual(summary["sources"]["Database 1"]["confirmed_pairs"], 1)
            # An old label is reclassified on training.
            library.db.execute("UPDATE pairs SET profile='combined'")
            library.db.commit()
            result = library.train()
            self.assertEqual(result["profiles"], {"stage1": 1})
            library.close()
            package = store.load_active()
            similar = bytearray(original)
            similar[0x2000:0x2010] = bytes(range(16))  # a slightly different file
            out = generate(bytes(similar), "stage1", package)
            self.assertEqual(out.evidence_level, "DONOR_TRANSFER")
            self.assertEqual(out.output, make_stage1(bytes(similar)))


def build_varied_archive(folder: Path, projects: int) -> None:
    """Same engine, every project its own software number and calibration."""

    folder.mkdir(parents=True, exist_ok=True)
    for n in range(projects):
        sw = f"SWALPHA-{n:04d}"
        image = bytearray(make_original(n, Variant(), calibration_scale=0.95 + 0.005 * n))
        pos = image.find(b"SWALPHA-0001")
        image[pos:pos + 12] = sw.encode()
        image = bytes(image)
        write_native_project(folder / f"p{n:02d}.ols", [image, make_stage1(image)], [("Stage 1", "NOCS")],
                             metadata(Variant(sw=sw)))


class PracticeTests(unittest.TestCase):
    def test_practice_hides_each_example_and_scores_the_transfer(self):
        from ols_platform.tuning.practice import read_stats, run_practice

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_varied_archive(root / "db", 16)
            store = PackageStore(root / "store")
            library = Library(store)
            library.scan({"db": root / "db"}, NativeOlsReader(), workers=1)
            library.train()
            first = run_practice(library, workers=2)
            self.assertEqual(first["tested"], 16)
            rows = library.db.execute("SELECT status, score, same_variant, donor_project FROM practice").fetchall()
            self.assertTrue(all(r[0] == "ok" and r[1] >= 0.99 for r in rows), rows)
            self.assertTrue(all(r[2] == 0 for r in rows))  # always learned from another software number
            pair_projects = dict(library.db.execute("SELECT pair_id, project_name FROM pairs").fetchall())
            for pair_id, donor in library.db.execute("SELECT pair_id, donor_project FROM practice"):
                self.assertNotEqual(pair_projects[pair_id], donor)  # its own project stayed hidden
            again = run_practice(library, workers=1)
            self.assertEqual(again["tested"], 0)  # resumable: nothing tested twice for the same version
            stats = read_stats(store.root)
            self.assertEqual(stats["total_good"], 16)
            library.close()

    def test_learned_floor_blocks_unreliable_similarity(self):
        import sqlite3

        from ols_platform.tuning.practice import SCHEMA, write_stats

        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(Path(tmp) / "x.sqlite3")
            db.executescript(SCHEMA)
            results = [(0.75, 0.4), (0.78, 0.6), (0.80, 0.7)] + [(0.90 + i * 0.01, 1.0) for i in range(8)]
            for i, (sim, score) in enumerate(results):
                db.execute("INSERT INTO practice(pair_id,package_id,version,family_key,profile,status,similarity,"
                           "score,same_variant,tested_at) VALUES(?,?,1,'ECU|1',?, 'ok', ?, ?, 0, 'x')",
                           (f"p{i}", "pkg", "stage1", sim, score))
            db.commit()
            stats = write_stats(Path(tmp), db, "pkg")
            db.close()  # Windows cannot remove an open database file
            self.assertEqual(stats["floors"], {"ECU|1": 0.9})
            self.assertEqual(stats["families"]["ECU|1"]["good"], 8)

    def test_background_practice_mode(self):
        from ols_platform.tuning.practice import read_stats

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_varied_archive(root / "db", 8)
            store_root = root / "store"
            store_root.mkdir()
            (store_root / "settings.json").write_text(json.dumps({"sources": [f"db={root / 'db'}"]}), encoding="utf-8")
            self.assertTrue(start_background(store_root))  # update + practice afterwards
            deadline = time.time() + 120
            while time.time() < deadline:
                status = read_status(store_root)
                if not status["running"] and status.get("state") in ("finished", "failed", "stopped"):
                    break
                time.sleep(0.5)
            self.assertEqual(status["state"], "finished", status.get("log"))
            self.assertEqual(read_stats(store_root)["total_tests"], 8)


class ContinuousPracticeTests(unittest.TestCase):
    def test_continuous_mode_keeps_going_until_stopped(self):
        from ols_platform.tuning.background import request_stop, run_update, worker_command

        self.assertIn("--continuous", worker_command(Path("x"), "continuous"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_archive(root / "db", 4)
            store_root = root / "store"
            store_root.mkdir()
            (store_root / "settings.json").write_text(json.dumps({"sources": [f"db={root / 'db'}"]}), encoding="utf-8")
            result: dict = {}

            def worker() -> None:
                result["code"] = run_update(store_root, workers=1, continuous=True, pause_minutes=1)

            # Tk objects left by the GUI tests must not be garbage-collected
            # in this worker thread (Tcl_AsyncDelete aborts the process).
            gc.collect()
            gc.disable()
            self.addCleanup(gc.enable)
            thread = threading.Thread(target=worker)
            thread.start()
            deadline = time.time() + 120
            while time.time() < deadline:
                status = read_status(store_root)
                if status.get("phase") == "wachten":
                    break
                time.sleep(0.3)
            self.assertEqual(status.get("phase"), "wachten", status.get("log"))
            self.assertTrue(thread.is_alive())  # still going: waiting for the next round
            request_stop(store_root)
            thread.join(60)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["code"], 0)
            self.assertEqual(read_status(store_root)["state"], "stopped")
            self.assertEqual(len(PackageStore(store_root).list_packages()), 1)
