from __future__ import annotations

import json
import ipaddress
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import parse_qs, urlsplit

from .database import IndexDatabase
from .sources import build_hashing_report
from .reference_package import build_package_report
from .project_matching import find_project_matches
from .project_search import parser_coverage, search_projects
from .winols_reference import build_project_report, build_reference_summary
from .sources import register_source, scan_registered_source, test_source


_PAGE = Path(__file__).with_name("dashboard.html")
_DIAGNOSTICS_PAGE = Path(__file__).with_name("diagnostics.html")
_STATIC = {
    "/dashboard.css": (Path(__file__).with_name("dashboard.css"), "text/css; charset=utf-8"),
    "/dashboard.js": (
        Path(__file__).with_name("dashboard.js"),
        "text/javascript; charset=utf-8",
    ),
    "/diagnostics.js": (
        Path(__file__).with_name("diagnostics.js"),
        "text/javascript; charset=utf-8",
    ),
}


def create_handler(database_path: Path):
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            peer = self.client_address[0]
            try:
                if not ipaddress.ip_address(peer).is_loopback:
                    self._send_json(403, {"error": "Loopback requests only."})
                    return
                host = self.headers.get("Host", "")
                if self.headers.get("Origin") != f"http://{host}":
                    self._send_json(403, {"error": "Same-origin requests only."})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._send_json(415, {"error": "Use application/json."})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 8192:
                    self._send_json(413, {"error": "Request body must be 1-8192 bytes."})
                    return
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be a JSON object.")
                database = IndexDatabase(database_path)
                try:
                    if self.path == "/api/sources":
                        path_value = payload.get("path")
                        if not isinstance(path_value, str) or not path_value.strip():
                            raise ValueError("A source directory path is required.")
                        result = register_source(database, Path(path_value))
                    elif self.path == "/api/source-roots":
                        path_value = payload.get("path")
                        slot_id = payload.get("slot_id")
                        if not isinstance(path_value, str) or not path_value.strip():
                            raise ValueError("A source directory path is required.")
                        if not isinstance(slot_id, int) or isinstance(slot_id, bool):
                            raise ValueError("A numeric source slot is required.")
                        if not 1 <= slot_id <= 5:
                            raise ValueError("Source slot must be between 1 and 5.")
                        result = register_source(database, Path(path_value))
                        database.configure_source_slot(slot_id, result["id"])
                        result = {
                            "slot": next(
                                slot for slot in database.list_source_slots()
                                if slot["slot_id"] == slot_id
                            )
                        }
                    elif self.path.startswith("/api/sources/"):
                        parts = self.path.strip("/").split("/")
                        if len(parts) != 4 or parts[0:2] != ["api", "sources"]:
                            self._send_json(404, {"error": "Not found."})
                            return
                        source_id = int(unquote(parts[2]))
                        if parts[3] == "test":
                            result = test_source(database, source_id)
                        elif parts[3] == "scan":
                            result = scan_registered_source(database, source_id)
                        else:
                            self._send_json(404, {"error": "Not found."})
                            return
                    else:
                        self._send_json(404, {"error": "Not found."})
                        return
                finally:
                    database.close()
                self._send_json(200, result)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
            except OSError as exc:
                self._send_json(409, {"error": str(exc)})
            except Exception as exc:
                self.log_error("Dashboard source operation failed: %s", exc)
                self._send_json(500, {"error": "Source operation failed; see local server log."})

        def do_GET(self) -> None:
            request = urlsplit(self.path)
            if request.path == "/":
                self._send(200, _PAGE.read_bytes(), "text/html; charset=utf-8")
                return
            if request.path == "/diagnostics":
                self._send(
                    200,
                    _DIAGNOSTICS_PAGE.read_bytes(),
                    "text/html; charset=utf-8",
                )
                return
            if request.path in _STATIC:
                path, content_type = _STATIC[request.path]
                self._send(200, path.read_bytes(), content_type)
                return

            try:
                database = IndexDatabase(database_path)
                try:
                    if request.path == "/api/status":
                        self._send_json(200, database.summary())
                        return
                    if request.path == "/api/inventory":
                        self._send_json(
                            200,
                            {
                                "indexed": database.summary(),
                                "latest_scan": database.get_latest_inventory_run(),
                            },
                        )
                        return
                    if request.path == "/api/multi-source-inventory":
                        latest = database.get_latest_inventory_run()
                        if latest is not None and latest["report"].get("workflow") == (
                            "multi_source_metadata_inventory"
                        ):
                            self._send_json(200, latest)
                        else:
                            self._send_json(200, None)
                        return
                    if request.path == "/api/source-scan-progress":
                        self._send_json(200, database.list_source_scan_progress())
                        return
                    if request.path == "/api/hashing":
                        self._send_json(200, build_hashing_report(database))
                        return
                    if request.path == "/api/inventory/locations":
                        query = parse_qs(request.query)
                        source_id = (
                            int(query["source_id"][0])
                            if query.get("source_id") else None
                        )
                        limit = int(query.get("limit", ["100"])[0])
                        offset = int(query.get("offset", ["0"])[0])
                        self._send_json(
                            200,
                            database.list_inventory_locations(
                                source_id=source_id,limit=limit,offset=offset
                            ),
                        )
                        return
                    if request.path == "/api/sources":
                        self._send_json(200, database.list_sources())
                        return
                    if request.path == "/api/source-roots":
                        self._send_json(
                            200,
                            {
                                "roots": database.list_source_slots(),
                                "deduplication": database.source_dedup_summary(),
                                "duplicate_locations": database.list_duplicate_content_locations(),
                            },
                        )
                        return
                    if request.path == "/api/parser-coverage":
                        query = parse_qs(request.query)
                        limit = int(query.get("limit", ["1000"])[0])
                        self._send_json(
                            200, parser_coverage(database, limit=limit)
                        )
                        return
                    if request.path == "/api/project-search":
                        query = parse_qs(request.query)
                        metadata = {
                            field: query[field][0]
                            for field in (
                                "ecu_type", "ecu_manufacturer", "ecu_family",
                                "sw_number", "hw_number", "hw_code",
                                "calibration_number", "calibration_id",
                                "engine", "make", "model", "year",
                                "fuel_type", "displacement", "power",
                                "gearbox", "memory_type",
                            )
                            if query.get(field, [""])[0]
                        }
                        self._send_json(
                            200,
                            search_projects(
                                database,
                                text=query.get("text", [""])[0][:200],
                                metadata=metadata,
                                map_name=query.get("map_name", [""])[0][:200],
                                columns=int(query["columns"][0])
                                if query.get("columns") else None,
                                rows=int(query["rows"][0]) if query.get("rows") else None,
                                version_name=query.get("version_name", [""])[0][:200],
                                binary_hash=query.get("binary_hash", [""])[0][:128],
                                file_hash=query.get("file_hash", [""])[0][:64],
                                format_version=int(query["format_version"][0])
                                if query.get("format_version") else None,
                                validation_status=query.get(
                                    "validation_status", [""]
                                )[0][:40],
                                parser_status=query.get(
                                    "parser_status", [""]
                                )[0][:40],
                                source_path=query.get(
                                    "source_path", [""]
                                )[0][:500],
                                file_size=int(query["file_size"][0])
                                if query.get("file_size") else None,
                                min_file_size=int(query["min_file_size"][0])
                                if query.get("min_file_size") else None,
                                max_file_size=int(query["max_file_size"][0])
                                if query.get("max_file_size") else None,
                                map_text=query.get("map_text", [""])[0][:200],
                                limit=int(query.get("limit", ["100"])[0]),
                                offset=int(query.get("offset", ["0"])[0]),
                            ),
                        )
                        return
                    if request.path.startswith("/api/index-queue/"):
                        source_id = int(request.path.rsplit("/", 1)[1])
                        if database.get_source(source_id) is None:
                            self._send_json(404, {"error": "Source not found."})
                        else:
                            self._send_json(
                                200,
                                database.list_index_queue(
                                    source_id, limit=1000
                                ),
                            )
                        return
                    if request.path.startswith("/api/source-scans/"):
                        source_id = int(request.path.rsplit("/", 1)[1])
                        if database.get_source(source_id) is None:
                            self._send_json(404, {"error": "Source not found."})
                        else:
                            self._send_json(
                                200,
                                database.list_source_scan_runs(source_id),
                            )
                        return
                    if request.path == "/api/projects":
                        query = parse_qs(request.query)
                        limit = int(query.get("limit", ["100"])[0])
                        search = query.get("query", [""])[0][:200]
                        self._send_json(
                            200,
                            database.list_projects(query=search, limit=limit),
                        )
                        return
                    if request.path == "/api/diagnostics":
                        query = parse_qs(request.query)
                        status = query.get("status", [""])[0]
                        parser_errors = query.get("parser_errors", ["0"])[0] == "1"
                        needs_reference = query.get("needs_reference", ["0"])[0] == "1"
                        limit = int(query.get("limit", ["100"])[0])
                        self._send_json(
                            200,
                            database.list_diagnostics(
                                processing_status=status,
                                parser_errors=parser_errors,
                                needs_reference=needs_reference,
                                limit=limit,
                            ),
                        )
                        return
                    if request.path == "/api/reference-summary":
                        self._send_json(200, build_reference_summary(database))
                        return
                    if request.path == "/api/version-matches":
                        query = parse_qs(request.query)
                        project_values = query.get("project_id", [])
                        project_id = (
                            int(project_values[0]) if project_values else None
                        )
                        limit = int(query.get("limit", ["500"])[0])
                        status = query.get("status", ["all"])[0]
                        self._send_json(
                            200,
                            database.list_binary_slot_candidates(
                                project_id,
                                status=status,
                                limit=limit,
                            ),
                        )
                        return
                    if request.path == "/api/export-queue":
                        query = parse_qs(request.query)
                        status_values = query.get("status", [])
                        status = status_values[0] if status_values else None
                        limit = int(query.get("limit", ["500"])[0])
                        self._send_json(
                            200,
                            database.list_export_tasks(status=status, limit=limit),
                        )
                        return
                    if request.path.startswith("/api/export-task-history/"):
                        task_id = int(request.path.rsplit("/", 1)[1])
                        self._send_json(
                            200,
                            database.list_export_task_history(task_id),
                        )
                        return
                    if request.path.startswith("/api/reference-report/"):
                        project_id = int(request.path.rsplit("/", 1)[1])
                        report = build_project_report(database, project_id)
                        self._send_json(200, report)
                        return
                    if request.path.startswith("/api/reference-package/"):
                        project_id = int(request.path.rsplit("/", 1)[1])
                        report = build_package_report(database, project_id)
                        self._send_json(200, report)
                        return
                    if request.path.startswith("/api/project-matches/"):
                        project_id = int(request.path.rsplit("/", 1)[1])
                        self._send_json(
                            200,
                            find_project_matches(database, project_id),
                        )
                        return
                    if request.path.startswith("/api/projects/"):
                        project_id = int(request.path.rsplit("/", 1)[1])
                        project = database.get_project(project_id)
                        if project is None:
                            self._send_json(404, {"error": "Project not found."})
                        else:
                            self._send_json(200, project)
                        return
                finally:
                    database.close()
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    self._send_json(
                        503,
                        {"error": "The index is busy; retry the read shortly."},
                    )
                else:
                    self._send_json(500, {"error": "Search failed; see local server log."})
                    self.log_error("Dashboard SQLite query failed: %s", exc)
                return
            except TimeoutError as exc:
                self._send_json(504, {"error": str(exc)})
                return
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._send_json(500, {"error": "Request failed; see local server log."})
                self.log_error("Dashboard request failed: %s", exc)
                return

            self._send_json(404, {"error": "Not found."})

        def _send_json(self, status: int, payload: object) -> None:
            self._send(
                status,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _send(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            print(f"dashboard: {self.address_string()} - {format % args}")

    return DashboardHandler


def serve(database_path: Path, port: int = 8765) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), create_handler(database_path))
    print(f"Local dashboard: http://127.0.0.1:{port}/ (loopback only; Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local dashboard.")
    finally:
        server.server_close()
