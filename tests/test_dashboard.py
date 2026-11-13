import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import patch

from ols_platform.dashboard import create_handler
from ols_platform.database import IndexDatabase


class DashboardTests(unittest.TestCase):
    def test_candidate_selection_export_and_standalone_index_routes(self):
        from tests.test_ecu_workbench import EcuWorkbenchTests

        fixture = EcuWorkbenchTests("test_streaming_bin_import_persists_hash_and_never_invents_identifiers")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture._import_project("project.ols", b"project", slot_payload=b"embedded")
        first_id, first = fixture._add_export_artifact(1, "first.bin", b"A" * 100)
        second_id, second = fixture._add_export_artifact(1, "second.bin", b"O" * 95 + b"B" * 5)
        with fixture.index.connection:
            fixture.index.connection.execute(
                "UPDATE reference_artifacts SET version_name='Stage 1' WHERE id=?", (first_id,)
            )
            fixture.index.connection.execute(
                "UPDATE reference_artifacts SET version_name='Stage 2 + EGR OFF' WHERE id=?", (second_id,)
            )
        standalone = fixture.root / "standalone.bin"
        standalone.write_bytes(b"S" * 100)
        source_id = fixture.index.register_source(
            source_key="test", path=str(fixture.root), status="available"
        )
        location_id = fixture.index.record_file_location(
            source_id=source_id, path=str(standalone), file_kind="other",
            size_bytes=standalone.stat().st_size, modified_ns=standalone.stat().st_mtime_ns,
            filename=standalone.name, extension="bin",
        )
        original = fixture.root / "original.bin"
        original.write_bytes(b"O" * 100)
        output = fixture.root / "output"
        output.mkdir()
        fixture.index.close()
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        before = {str(path): digest(path) for path in (original, first, second, standalone, fixture.index_path)}
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(fixture.index_path))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def post(path, payload):
            request = Request(base + path, data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json", "Origin": base},
                              method="POST")
            with urlopen(request) as response:
                return json.loads(response.read())

        try:
            request = Request(
                base + "/api/workbench/import?ecu_type=ECU-A&hw_number=CONFLICT",
                data=original.read_bytes(),
                headers={"Content-Type": "application/octet-stream", "X-File-Name": "original.bin",
                         "Origin": base}, method="POST",
            )
            with urlopen(request) as response:
                analysis = json.loads(response.read())
            query = urlencode({"analysis_id": analysis["analysis_id"]})
            with urlopen(base + "/api/workbench/export-candidates?" + query) as response:
                listing = json.loads(response.read())
            self.assertEqual([x["tuning_label"] for x in listing["candidates"]],
                             ["Stage 1", "Stage 2 + EGR OFF"])
            self.assertEqual(listing["candidates"][1]["tuning_label_status"], "UNCONFIRMED_INDEXED_VERSION")
            self.assertTrue(listing["candidates"][1]["differing_features"])
            self.assertFalse(listing["unexportable_slots"][0]["exportable"])
            selection = {"analysis_id": analysis["analysis_id"], "source_kind": "reference",
                         "artifact_id": second_id}
            with self.assertRaises(HTTPError) as error:
                post("/api/workbench/export-preview", {**selection, "artifact_id": 999999})
            self.assertEqual(error.exception.code, 400)
            preview = post("/api/workbench/export-preview", selection)
            self.assertEqual(preview["candidate"]["similarity"]["percent"], 95.0)
            payload = {**selection, "confirmed": True, "confirmation_token": preview["confirmation_token"],
                       "output_directory": str(output), "output_name": "result.bin",
                       "user_label": "Stage 2 + EGR OFF"}
            with self.assertRaises(HTTPError) as error:
                post("/api/workbench/export", {**payload, "confirmed": False})
            self.assertEqual(error.exception.code, 400)
            self.assertFalse((output / "result.bin").exists())
            second.write_bytes(b"X" * 100)
            with self.assertRaises(HTTPError) as error:
                post("/api/workbench/export", payload)
            self.assertEqual(error.exception.code, 400)
            second.write_bytes(b"O" * 95 + b"B" * 5)
            preview = post("/api/workbench/export-preview", selection)
            payload["confirmation_token"] = preview["confirmation_token"]
            existing = output / "result.bin"
            existing.write_bytes(b"untouched")
            with self.assertRaises(HTTPError) as error:
                post("/api/workbench/export", payload)
            self.assertEqual(error.exception.code, 409)
            self.assertEqual(existing.read_bytes(), b"untouched")
            payload["output_name"] = "chosen.bin"
            result = post("/api/workbench/export", payload)
            manifest = json.loads((output / "chosen.bin.manifest.json").read_text(encoding="utf-8"))
            self.assertEqual((output / "chosen.bin").read_bytes(), second.read_bytes())
            self.assertEqual(result["source_artifact_id"], second_id)
            self.assertEqual(manifest["candidate"]["sha256"], manifest["output"]["sha256"])
            self.assertEqual(manifest["original"]["sha256"], digest(original))
            self.assertEqual(manifest["user_label"], "Stage 2 + EGR OFF")
            self.assertEqual(manifest["compatibility"], "COMPATIBILITY_NOT_VALIDATED")

            query = urlencode({"directory": str(fixture.root)})
            with urlopen(base + "/api/workbench/indexed-binaries?" + query) as response:
                locations = json.loads(response.read())["candidates"]
            self.assertEqual([item["artifact_id"] for item in locations], [location_id])
            self.assertEqual(locations[0]["tuning_label"], "UNKNOWN")
            location_selection = {**selection, "source_kind": "location", "artifact_id": location_id}
            location_preview = post("/api/workbench/export-preview", location_selection)
            self.assertEqual(location_preview["candidate"]["sha256"], digest(standalone))
            original_stat = standalone.stat()
            standalone.write_bytes(b"T" * 100)
            with self.assertRaises(HTTPError) as error:
                post("/api/workbench/export", {
                    **location_selection, "confirmation_token": location_preview["confirmation_token"],
                    "confirmed": True, "output_directory": str(output), "output_name": "changed.bin",
                })
            self.assertEqual(error.exception.code, 400)
            standalone.write_bytes(b"S" * 100)
            os.utime(standalone, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            location_preview = post("/api/workbench/export-preview", location_selection)
            location_result = post("/api/workbench/export", {
                **location_selection, "confirmation_token": location_preview["confirmation_token"],
                "confirmed": True, "output_directory": str(output), "output_name": "standalone-copy.bin",
            })
            self.assertEqual(location_result["source_kind"], "location")
            self.assertEqual((output / "standalone-copy.bin").read_bytes(), standalone.read_bytes())
            self.assertEqual(before, {str(path): digest(path) for path in
                                      (original, first, second, standalone, fixture.index_path)})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_get_status_does_not_create_missing_index(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "missing.sqlite3"
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), create_handler(database_path)
            )
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                with self.assertRaises(HTTPError) as error:
                    urlopen(f"http://127.0.0.1:{server.server_port}/api/status")
                self.assertEqual(error.exception.code, 500)
                self.assertFalse(database_path.exists())
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=3)

    def test_ori_upload_keeps_index_read_only_and_workbench_storage_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "index.sqlite3"
            database = IndexDatabase(database_path)
            database.close()

            def index_snapshot():
                uri = f"{database_path.resolve().as_uri()}?mode=ro"
                connection = sqlite3.connect(uri, uri=True)
                try:
                    tables = connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type='table' AND name NOT LIKE 'sqlite_%'
                        ORDER BY name
                        """
                    ).fetchall()
                    counts = tuple(
                        (
                            name,
                            connection.execute(
                                f'SELECT COUNT(*) FROM "{name}"'
                            ).fetchone()[0],
                        )
                        for (name,) in tables
                    )
                    return connection.execute("PRAGMA user_version").fetchone()[0], counts
                finally:
                    connection.close()

            before = index_snapshot()
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), create_handler(database_path)
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                request = Request(
                    f"{base_url}/api/workbench/import",
                    data=b"ori-fixture-data",
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-File-Name": "customer.ori",
                        "Origin": base_url,
                    },
                    method="POST",
                )
                with urlopen(request) as response:
                    result = json.loads(response.read())
                    self.assertEqual(response.status, 201)
                    self.assertEqual(result["source"]["size_bytes"], 16)
                    self.assertEqual(result["report"]["file"]["format"], "raw_ori")
                self.assertEqual(index_snapshot(), before)
                self.assertTrue(
                    (Path(directory) / "ecu-workbench" / "workbench.sqlite3").is_file()
                )
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)

    def test_ecu_workbench_import_history_reanalysis_and_compare_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "index.sqlite3"
            database = IndexDatabase(database_path)
            database.close()
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), create_handler(database_path)
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                rebound_request = Request(
                    f"{base_url}/api/workbench/history",
                    headers={"Host": "attacker.invalid"},
                )
                with self.assertRaises(HTTPError) as error:
                    urlopen(rebound_request)
                self.assertEqual(error.exception.code, 403)

                rebound_post = Request(
                    f"{base_url}/api/workbench/reference",
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "Host": "attacker.invalid",
                        "Origin": "http://attacker.invalid",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as error:
                    urlopen(rebound_post)
                self.assertEqual(error.exception.code, 403)

                unsupported_request = Request(
                    f"{base_url}/api/workbench/import",
                    data=b":00000001FF",
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-File-Name": "firmware.hex",
                        "Origin": base_url,
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as error:
                    urlopen(unsupported_request)
                self.assertEqual(error.exception.code, 400)

                with self.assertRaises(HTTPError) as error:
                    urlopen(f"{base_url}/api/workbench/analyses/not-a-real-id")
                self.assertEqual(error.exception.code, 404)

                first_request = Request(
                    f"{base_url}/api/workbench/import?ecu_type=ECU-A",
                    data=b"\x00\x01\x02\x03",
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-File-Name": "original.bin",
                        "Origin": base_url,
                    },
                    method="POST",
                )
                with urlopen(first_request) as response:
                    first = json.loads(response.read())
                    self.assertEqual(response.status, 201)
                    self.assertEqual(first["source"]["size_bytes"], 4)
                    self.assertEqual(
                        first["identifications"]["user_provided"]["ecu_type"],
                        "ECU-A",
                    )

                second_request = Request(
                    f"{base_url}/api/workbench/import",
                    data=b"\x00\x01\xff\x03\x04",
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-File-Name": "tuned.bin",
                        "Origin": base_url,
                    },
                    method="POST",
                )
                with urlopen(second_request) as response:
                    second = json.loads(response.read())

                compare_request = Request(
                    f"{base_url}/api/workbench/compare",
                    data=json.dumps({
                        "analysis_a_id": first["analysis_id"],
                        "analysis_b_id": second["analysis_id"],
                        "threshold_percent": 90,
                    }).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base_url,
                    },
                    method="POST",
                )
                with urlopen(compare_request) as response:
                    comparison = json.loads(response.read())
                    self.assertEqual(response.status, 201)
                    self.assertEqual(comparison["differing_bytes"], 2)
                    self.assertEqual(comparison["semantic_status"], "INCONCLUSIVE")
                    self.assertEqual(comparison["byte_similarity"]["status"], "NOT_COMPARABLE")
                    self.assertIsNone(comparison["byte_similarity"]["meets_threshold"])
                    self.assertEqual(comparison["byte_similarity"]["threshold_percent"], 90)

                invalid_threshold = Request(
                    f"{base_url}/api/workbench/compare",
                    data=json.dumps({
                        "analysis_a_id": first["analysis_id"],
                        "analysis_b_id": second["analysis_id"],
                        "threshold_percent": 101,
                    }).encode(),
                    headers={"Content-Type": "application/json", "Origin": base_url},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as error:
                    urlopen(invalid_threshold)
                self.assertEqual(error.exception.code, 400)

                rerun_request = Request(
                    f"{base_url}/api/workbench/analyses/{first['analysis_id']}/reanalyze",
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base_url,
                    },
                    method="POST",
                )
                with urlopen(rerun_request) as response:
                    rerun = json.loads(response.read())
                    self.assertEqual(rerun["parent_analysis_id"], first["analysis_id"])
                with urlopen(f"{base_url}/api/workbench/history") as response:
                    saved = json.loads(response.read())
                    self.assertEqual(len(saved["analyses"]), 3)
                    self.assertEqual(len(saved["comparisons"]), 1)
                with urlopen(
                    f"{base_url}/api/workbench/analyses/{first['analysis_id']}"
                ) as response:
                    self.assertEqual(
                        json.loads(response.read())["analysis_id"],
                        first["analysis_id"],
                    )
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)

    def test_local_read_only_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "index.sqlite3"
            database = IndexDatabase(database_path)
            database.close()
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), create_handler(database_path)
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(f"{base_url}/") as response:
                    self.assertEqual(response.status, 200)
                    dashboard = response.read()
                    self.assertIn(b"WinOLS bibliotheek", dashboard)
                    self.assertIn(b"ECU File Workbench", dashboard)
                    self.assertIn(
                        b'accept=".bin,.ori,application/octet-stream"', dashboard
                    )
                    self.assertEqual(
                        response.headers["Content-Security-Policy"],
                        "default-src 'self'; script-src 'self'; style-src 'self'; "
                        "connect-src 'self'; object-src 'none'; base-uri 'none'; "
                        "frame-ancestors 'none'",
                    )
                with urlopen(f"{base_url}/dashboard.css") as response:
                    self.assertIn(b".cards", response.read())
                with urlopen(f"{base_url}/dashboard.js") as response:
                    dashboard_script = response.read()
                    self.assertIn(b"loadProjects", dashboard_script)
                    self.assertIn(b"loadMultiSourceInventory", dashboard_script)
                    self.assertIn(b"/\\.(bin|ori)$/i.test(file.name)", dashboard_script)
                with urlopen(f"{base_url}/diagnostics") as response:
                    self.assertIn(b"Importdiagnose", response.read())
                with urlopen(f"{base_url}/diagnostics.js") as response:
                    self.assertIn(b"loadDiagnostics", response.read())
                with urlopen(f"{base_url}/api/status") as response:
                    status = json.loads(response.read())
                    self.assertEqual(status["projects"], 0)
                with urlopen(f"{base_url}/api/inventory") as response:
                    inventory = json.loads(response.read())
                    self.assertEqual(inventory["indexed"]["projects"], 0)
                    self.assertIsNone(inventory["latest_scan"])
                with urlopen(f"{base_url}/api/multi-source-inventory") as response:
                    self.assertIsNone(json.loads(response.read()))
                with urlopen(f"{base_url}/api/source-scan-progress") as response:
                    self.assertEqual(len(json.loads(response.read())), 5)
                with urlopen(f"{base_url}/api/hashing") as response:
                    hashing = json.loads(response.read())
                    self.assertEqual(hashing["totals"]["ols_locations"], 0)
                    self.assertEqual(hashing["worker"]["active_workers"],0)
                    self.assertEqual(hashing["worker"]["progress_percent"],0)
                with urlopen(f"{base_url}/api/inventory/locations?limit=10") as response:
                    self.assertEqual(json.loads(response.read()), [])
                with urlopen(f"{base_url}/api/sources") as response:
                    self.assertEqual(json.loads(response.read()), [])
                with urlopen(f"{base_url}/api/parser-coverage") as response:
                    self.assertEqual(json.loads(response.read())["project_count"], 0)
                with urlopen(f"{base_url}/api/project-search") as response:
                    search = json.loads(response.read())
                    self.assertEqual(search["results"], [])
                    self.assertEqual(search["offset"], 0)
                    self.assertFalse(search["has_more"])
                    self.assertGreaterEqual(search["query_seconds"], 0)
                with urlopen(f"{base_url}/api/workbench/references") as response:
                    self.assertEqual(json.loads(response.read()), [])
                with urlopen(f"{base_url}/api/projects") as response:
                    self.assertEqual(json.loads(response.read()), [])
                with urlopen(f"{base_url}/api/diagnostics") as response:
                    self.assertEqual(json.loads(response.read()), [])
                with urlopen(f"{base_url}/api/reference-summary") as response:
                    summary = json.loads(response.read())
                    self.assertEqual(summary["project_count"], 0)
                with urlopen(f"{base_url}/api/reference-package/1") as response:
                    package = json.loads(response.read())
                    self.assertEqual(package["package_status"], "NOT_TESTED")
                with urlopen(f"{base_url}/api/version-matches") as response:
                    self.assertEqual(json.loads(response.read()), [])
                with urlopen(
                    f"{base_url}/api/version-matches?status=unresolved"
                ) as response:
                    self.assertEqual(json.loads(response.read()), [])
                with urlopen(f"{base_url}/api/export-queue") as response:
                    self.assertEqual(json.loads(response.read()), [])
                with urlopen(f"{base_url}/api/export-task-history/1") as response:
                    self.assertEqual(json.loads(response.read()), [])
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{base_url}/api/projects/not-an-id")
                self.assertEqual(raised.exception.code, 400)
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{base_url}/api/project-matches/1")
                self.assertEqual(raised.exception.code, 400)
                source_path = Path(directory) / "not-yet-created"
                request = Request(
                    f"{base_url}/api/sources",
                    data=json.dumps({"path": str(source_path)}).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base_url,
                    },
                    method="POST",
                )
                with urlopen(request) as response:
                    registered = json.loads(response.read())
                    self.assertEqual(registered["status"], "unavailable")
                with urlopen(f"{base_url}/api/sources") as response:
                    self.assertEqual(len(json.loads(response.read())), 1)
                with urlopen(f"{base_url}/api/source-roots") as response:
                    roots = json.loads(response.read())
                    self.assertEqual(
                        [item["name"] for item in roots["roots"]],
                        ["Database 1", "Database 2", "Database 3", "Database 4", "default"],
                    )
                    self.assertEqual(roots["deduplication"]["unique_ols_contents"], 0)
                configure_request = Request(
                    f"{base_url}/api/source-roots",
                    data=json.dumps({
                        "slot_id": 2,
                        "path": str(source_path),
                    }).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base_url,
                    },
                    method="POST",
                )
                with urlopen(configure_request) as response:
                    configured = json.loads(response.read())["slot"]
                    self.assertEqual(configured["name"], "Database 2")
                    self.assertEqual(configured["source_status"], "unavailable")
                with urlopen(f"{base_url}/api/source-scans/1") as response:
                    self.assertEqual(json.loads(response.read()), [])
                request = Request(
                    f"{base_url}/api/sources",
                    data=json.dumps({"path": str(source_path)}).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "http://attacker.invalid",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request)
                self.assertEqual(raised.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)

    def test_project_search_validation_busy_database_and_timeout_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "index.sqlite3"
            database = IndexDatabase(database_path)
            database.close()
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), create_handler(database_path)
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{base_url}/api/project-search?file_hash=invalid")
                self.assertEqual(raised.exception.code, 400)

                with patch(
                    "ols_platform.dashboard.search_projects",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(f"{base_url}/api/project-search")
                    self.assertEqual(raised.exception.code, 503)
                    self.assertIn(
                        "retry",
                        json.loads(raised.exception.read())["error"].lower(),
                    )

                with patch(
                    "ols_platform.dashboard.search_projects",
                    side_effect=TimeoutError("Search timed out."),
                ):
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(f"{base_url}/api/project-search")
                    self.assertEqual(raised.exception.code, 504)
                    self.assertEqual(
                        json.loads(raised.exception.read())["error"],
                        "Search timed out.",
                    )
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
