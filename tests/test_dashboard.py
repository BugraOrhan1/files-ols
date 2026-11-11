import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ols_platform.dashboard import create_handler
from ols_platform.database import IndexDatabase


class DashboardTests(unittest.TestCase):
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
                    self.assertIn(b"WinOLS bibliotheek", response.read())
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
                    self.assertEqual(json.loads(response.read())["results"], [])
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


if __name__ == "__main__":
    unittest.main()
