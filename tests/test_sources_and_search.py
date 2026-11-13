import hashlib
import json
import sqlite3
import tempfile
import unittest
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from ols_platform.database import IndexDatabase
from ols_platform.importer import import_sources
from ols_platform.project_search import parser_coverage, search_projects
from ols_platform.reader import (
    ParsedMap,
    ParsedVersion,
    ProjectSnapshot,
    ReadIssue,
)
from ols_platform.sources import (
    build_hashing_plan,
    build_hashing_report,
    move_source,
    register_source,
    run_multi_source_inventory,
    run_hashing,
    run_hash_worker,
    run_source_queue,
    scan_registered_source,
    test_source,
)
from deployment.database_integrity import snapshot_database


class FixtureReader:
    parser_name = "fixture-parser"
    parser_version = "1"

    def inspect(self, source):
        if not source.is_file():
            raise FileNotFoundError(source)
        return ProjectSnapshot(
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            format_version=503,
            metadata={
                "make": "Example",
                "model": "Roadster",
                "ecu_type": "ECU-A",
                "sw_number": "SW-123",
            },
            versions=(ParsedVersion("Original", "", 0, 1, 0, 8, "", 0),),
            maps=(ParsedMap(
                "Fuel map", "", "MAP", "UWORD", 8, 4, 100, "mg", 1.0, 0.0
            ),),
            issues=(ReadIssue("unresolved_fixture", "Fixture has unresolved data."),),
        )


class SourceAndSearchTests(unittest.TestCase):
    def _create_hash_fixture(self, root: Path, file_count: int):
        source_paths = [root / f"source-{index}" for index in range(1,6)]
        for path in source_paths:
            path.mkdir()
        for index in range(file_count):
            source_paths[index % len(source_paths)].joinpath(
                f"file-{index}.ols"
            ).write_bytes(f"project-{index}".encode() * 5000)
        database = IndexDatabase(root / "index.sqlite3")
        sources = [register_source(database,path) for path in source_paths]
        for slot_id,source in enumerate(sources,start=1):
            database.configure_source_slot(slot_id,source["id"])
            scan_registered_source(database,source["id"],benchmark_bytes=0)
        return database,sources

    def test_hash_worker_runs_successive_batches_and_resumes_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database,sources=self._create_hash_fixture(Path(directory),3)
            try:
                first=run_hash_worker(
                    database,workers=1,batch_size=1,batch_delay_seconds=0,
                    retry_delay_seconds=0,max_batches=1,
                )
                self.assertEqual(first["status"],"stopped")
                self.assertEqual(first["batches_completed"],1)
                self.assertEqual(first["files_completed"],1)
                self.assertEqual(first["queue"]["totals"]["hashed"],1)
                self.assertEqual(first["queue"]["worker"]["status"],"stopped")
                database.set_hash_control("paused")
                database.set_hash_control("running")

                resumed=run_hash_worker(
                    database,workers=2,batch_size=1,batch_delay_seconds=0,
                    retry_delay_seconds=0,max_batches=3,
                )
                self.assertEqual(resumed["status"],"complete")
                self.assertEqual(resumed["batches_completed"],2)
                self.assertEqual(resumed["files_completed"],2)
                self.assertEqual(resumed["queue"]["totals"]["hashed"],3)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM hash_attempts WHERE status='HASHED'"
                    ).fetchone()[0],
                    3,
                )
                self.assertEqual(
                    database.hash_worker_status()["files_completed"],2
                )
                self.assertEqual(
                    resumed["queue"]["worker"]["bytes_read"],
                    resumed["bytes_read"],
                )
                self.assertGreaterEqual(
                    resumed["last_batch"]["timings"]["read_call_seconds"],0
                )
                self.assertGreaterEqual(
                    resumed["last_batch"]["timings"]["sha256_update_seconds"],0
                )
                self.assertGreaterEqual(
                    resumed["last_batch"]["timings"]["database_write_seconds"],0
                )
                empty=run_hash_worker(
                    database,workers=1,batch_size=2,batch_delay_seconds=0,
                    retry_delay_seconds=0,
                )
                self.assertEqual(empty["status"],"complete")
                self.assertEqual(empty["files_completed"],0)
                self.assertEqual(empty["queue"]["totals"]["pending"],0)
                self.assertEqual(len(sources),5)
            finally:
                database.close()

    def test_interrupted_hash_run_recovers_after_database_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, sources = self._create_hash_fixture(root, 1)
            database_path = root / "index.sqlite3"
            try:
                database.sync_hash_jobs([sources[0]["id"]])
                run_id = database.begin_hash_run(
                    worker_count=1,
                    chunk_size=64 * 1024,
                )
                claimed = database.claim_hash_jobs(
                    run_id=run_id,
                    source_ids=[sources[0]["id"]],
                    limit=1,
                )
                self.assertEqual(len(claimed), 1)
                with database.connection:
                    database.connection.execute(
                        """
                        UPDATE hash_worker_state SET status='running',
                            owner_token='simulated-crash',process_id=?,
                            active_run_id=? WHERE id=1
                        """,
                        (2_147_483_647, run_id),
                    )
            finally:
                database.close()

            recovered = IndexDatabase(database_path)
            try:
                report = recovered.recover_hash_run()

                self.assertEqual(report["recovered_run_id"], run_id)
                self.assertEqual(report["jobs_requeued"], 1)
                run_status = recovered.connection.execute(
                    "SELECT status FROM hash_runs WHERE id=?",
                    (run_id,),
                ).fetchone()["status"]
                job = recovered.connection.execute(
                    "SELECT status,run_id,error FROM hash_jobs WHERE id=?",
                    (claimed[0]["id"],),
                ).fetchone()
                self.assertEqual(run_status, "INTERRUPTED")
                self.assertEqual(job["status"], "RETRY_PENDING")
                self.assertIsNone(job["run_id"])
                self.assertEqual(
                    job["error"],
                    "Recovered after interrupted hash process.",
                )
                self.assertEqual(recovered.hash_control_status()["status"], "idle")
                self.assertEqual(recovered.hash_worker_status()["status"], "stopped")

                resumed = run_hash_worker(
                    recovered,
                    workers=1,
                    batch_size=1,
                    batch_delay_seconds=0,
                    retry_delay_seconds=0,
                )
                self.assertEqual(resumed["status"], "complete")
                self.assertEqual(resumed["files_completed"], 1)
                self.assertEqual(resumed["queue"]["totals"]["hashed"], 1)
                self.assertEqual(
                    recovered.connection.execute(
                        "SELECT COUNT(*) FROM hash_attempts WHERE status='HASHED'"
                    ).fetchone()[0],
                    1,
                )

                repeated = recovered.recover_hash_run()
                self.assertIsNone(repeated["recovered_run_id"])
                self.assertEqual(repeated["jobs_requeued"], 0)
                self.assertEqual(
                    recovered.connection.execute(
                        "SELECT status FROM hash_jobs WHERE id=?",
                        (claimed[0]["id"],),
                    ).fetchone()["status"],
                    "HASHED",
                )
            finally:
                recovered.close()

    def test_hash_recovery_refuses_a_live_worker_process(self):
        with tempfile.TemporaryDirectory() as directory:
            database = IndexDatabase(Path(directory) / "index.sqlite3")
            try:
                run_id = database.begin_hash_run(
                    worker_count=1,
                    chunk_size=64 * 1024,
                )
                with database.connection:
                    database.connection.execute(
                        """
                        UPDATE hash_worker_state SET status='running',
                            owner_token='live-test-worker',process_id=?,
                            active_run_id=? WHERE id=1
                        """,
                        (os.getpid(), run_id),
                    )

                with self.assertRaisesRegex(
                    RuntimeError,
                    "appears active; refusing recovery",
                ):
                    database.recover_hash_run()

                self.assertEqual(
                    database.hash_control_status()["status"],
                    "running",
                )
                self.assertEqual(
                    database.connection.execute(
                        "SELECT status FROM hash_runs WHERE id=?",
                        (run_id,),
                    ).fetchone()["status"],
                    "RUNNING",
                )
                worker = database.connection.execute(
                    "SELECT status,process_id FROM hash_worker_state WHERE id=1"
                ).fetchone()
                self.assertEqual(worker["status"], "running")
                self.assertEqual(worker["process_id"], os.getpid())
            finally:
                database.close()

    def test_hashing_report_uses_wall_clock_batch_rates_and_remaining_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            database,sources=self._create_hash_fixture(Path(directory),1)
            try:
                database.sync_hash_jobs([sources[0]["id"]])
                source_key=str(sources[0]["id"])
                runs=(
                    (
                        "2026-01-01 00:00:00","2026-01-01 00:00:10",
                        {"bytes_read":4*1024*1024,"files_completed":2,
                         "outcomes":{"HASHED":2},"errors":0,
                         "by_source":{source_key:{"bytes_read":4*1024*1024}},
                         "timings":{"read_call_seconds":2.0,
                                    "sha256_update_seconds":0.1,
                                    "database_write_seconds":0.2,
                                    "worker_wall_seconds":10.0}},
                    ),
                    (
                        "2026-01-01 00:00:10","2026-01-01 00:00:30",
                        {"bytes_read":6*1024*1024,"files_completed":3,
                         "outcomes":{"HASHED":3},"errors":1,
                         "by_source":{source_key:{"bytes_read":6*1024*1024}},
                         "timings":{"read_call_seconds":3.0,
                                    "sha256_update_seconds":0.2,
                                    "database_write_seconds":0.3,
                                    "worker_wall_seconds":20.0}},
                    ),
                )
                with database.connection:
                    for started_at,finished_at,report in runs:
                        database.connection.execute(
                            """
                            INSERT INTO hash_runs(
                                status,worker_count,chunk_size,report_json,
                                started_at,finished_at
                            ) VALUES('BATCH_COMPLETE',2,1048576,?,?,?)
                            """,
                            (
                                json.dumps(report),started_at,finished_at,
                            ),
                        )

                report=build_hashing_report(database)
                performance=report["recent_hash_performance"]
                self.assertEqual(performance["runs"],2)
                self.assertEqual(performance["window_seconds"],30)
                self.assertEqual(performance["files_hashed"],5)
                self.assertEqual(performance["bytes_read"],10*1024*1024)
                self.assertAlmostEqual(performance["mib_per_second"],1/3,3)
                self.assertEqual(performance["files_per_hour"],600)
                self.assertEqual(performance["errors"],1)
                self.assertEqual(
                    report["totals"]["remaining_eta_basis"],
                    "Actual un-hashed OLS bytes divided by the wall-clock rate "
                    "across the ten most recent completed hash runs.",
                )
                self.assertAlmostEqual(
                    report["totals"]["remaining_seconds_at_recent_rate"],
                    report["totals"]["pending_physical_bytes"]/(1024*1024/3),
                    1,
                )
                timing=report["recent_hash_timing_breakdown"]
                self.assertEqual(timing["instrumented_runs"],2)
                self.assertEqual(timing["read_call_seconds"],5.0)
                self.assertEqual(timing["database_write_seconds"],0.5)
                source_report=next(
                    source for source in report["by_source"]
                    if source["source_id"]==sources[0]["id"]
                )
                self.assertEqual(
                    source_report["recent_bytes_read"],10*1024*1024
                )
                self.assertAlmostEqual(
                    source_report["mib_per_second"],1/3,3
                )
                self.assertEqual(len(sources),5)
            finally:
                database.close()

    def test_hash_worker_retries_transient_failure_and_changed_fingerprint(self):
        from ols_platform.sources import _FileChangedDuringHash, _hash_location

        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            database,sources=self._create_hash_fixture(root,1)
            try:
                original=_hash_location
                attempts={"count":0}
                def transient_error(job,chunk_size,progress_callback=None):
                    attempts["count"]+=1
                    if attempts["count"]==1:
                        raise OSError("temporary network read failure")
                    return original(job,chunk_size,progress_callback)
                with patch(
                    "ols_platform.sources._hash_location",
                    side_effect=transient_error,
                ):
                    reports=[]
                    for _ in range(5):
                        report=run_hash_worker(
                            database,workers=1,batch_size=1,
                            batch_delay_seconds=0,retry_delay_seconds=0,
                            retry_limit=2,max_batches=3,
                        )
                        reports.append(report)
                        if report["status"]=="complete":
                            break
                        self.assertEqual(report["status"],"stopped",report)
                self.assertEqual(report["status"],"complete",report)
                self.assertEqual(sum(item["errors"] for item in reports),1,reports)
                self.assertEqual(attempts["count"],2)
                statuses=[
                    row[0] for row in database.connection.execute(
                        "SELECT status FROM hash_attempts ORDER BY id"
                    )
                ]
                self.assertEqual(statuses,["RETRY_PENDING","HASHED"])

                source_id=sources[0]["id"]
                with database.connection:
                    database.connection.execute(
                        """
                        UPDATE file_locations SET sha256=NULL,content_id=NULL,
                            state='discovered' WHERE source_id=?
                        """,
                        (source_id,),
                    )
                    database.connection.execute(
                        """
                        UPDATE hash_jobs SET status='HASH_PENDING',sha256=NULL,
                            attempts=0 WHERE source_id=?
                        """,
                        (source_id,),
                    )
                changed={"count":0}
                def changes_once(job,chunk_size,progress_callback=None):
                    changed["count"]+=1
                    if changed["count"]==1:
                        raise _FileChangedDuringHash(
                            "file changed during read",
                            started_at="2026-11-10T00:00:00+00:00",
                            finished_at="2026-11-10T00:00:01+00:00",
                            bytes_read=3,
                        )
                    return original(job,chunk_size,progress_callback)
                with patch(
                    "ols_platform.sources._hash_location",
                    side_effect=changes_once,
                ):
                    changed_report=run_hash_worker(
                        database,workers=1,batch_size=1,batch_delay_seconds=0,
                        retry_delay_seconds=0,retry_limit=2,max_batches=3,
                    )
                self.assertEqual(changed_report["status"],"complete")
                self.assertEqual(changed["count"],2)
                self.assertEqual(
                    [
                        row[0] for row in database.connection.execute(
                            "SELECT status FROM hash_attempts ORDER BY id"
                        )
                    ][-2:],
                    ["FILE_CHANGED","HASHED"],
                )
            finally:
                database.close()

    def test_hash_worker_pause_resume_and_single_active_lease(self):
        from ols_platform.sources import _hash_location

        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            database,sources=self._create_hash_fixture(root,1)
            database_path=root/"index.sqlite3"
            database.close()
            started=threading.Event()
            release=threading.Event()
            result=[]
            failure=[]
            original=_hash_location

            def blocked_hash(job,chunk_size,progress_callback=None):
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test hash was not released")
                return original(job,chunk_size,progress_callback)

            def run():
                worker_database=IndexDatabase(database_path)
                try:
                    result.append(run_hash_worker(
                        worker_database,workers=1,batch_size=1,
                        batch_delay_seconds=0,retry_delay_seconds=0,
                    ))
                except Exception as exc:
                    failure.append(exc)
                finally:
                    worker_database.close()

            with patch(
                "ols_platform.sources._hash_location",side_effect=blocked_hash
            ):
                thread=threading.Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(started.wait(5))
                    control=IndexDatabase(database_path)
                    try:
                        with self.assertRaises(RuntimeError):
                            control.acquire_hash_worker(
                                owner_token="second",process_id=os.getpid(),workers=1,
                                batch_size=1,chunk_size=1024*1024,
                                batch_delay_seconds=0,retry_limit=2,
                                retry_delay_seconds=0,
                            )
                        control.set_hash_control("paused")
                        release.set()
                        deadline=time.monotonic()+5
                        while time.monotonic()<deadline:
                            if control.hash_worker_status()["status"]=="paused":
                                break
                            time.sleep(0.02)
                        self.assertEqual(
                            control.hash_worker_status()["status"],"paused"
                        )
                    finally:
                        control.set_hash_control("running")
                        control.close()
                finally:
                    release.set()
                    thread.join(10)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(failure,[])
            self.assertEqual(result[0]["status"],"complete")
            self.assertEqual(result[0]["batches_completed"],1)
            self.assertEqual(result[0]["queue"]["totals"]["hashed"],1)

    def test_source_root_relocation_rebases_queue_paths_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            database,sources=self._create_hash_fixture(root,2)
            try:
                hashed=run_hashing(
                    database,workers=1,chunk_size=64*1024,max_files=2
                )
                self.assertEqual(hashed["files_completed"],2)
                before=snapshot_database(database.connection)
                source_ids=[source["id"] for source in sources]
                database.sync_hash_jobs(source_ids)
                new_roots={}
                mappings={}
                for source in sources:
                    target=root/f"local-{source['id']}"
                    target.mkdir()
                    new_roots[str(source["id"])]=str(target)
                    mappings[source["id"]]={
                        "old_path":source["path"],"new_path":str(target)
                    }
                moved=database.relocate_source_roots(mappings)
                self.assertEqual(len(moved),5)
                for source in sources:
                    expected=Path(new_roots[str(source["id"])])/"file-0.ols"
                    if source["id"]==sources[0]["id"]:
                        for table in ("file_locations","index_queue","hash_jobs"):
                            path=database.connection.execute(
                                f"SELECT path FROM {table} WHERE source_id=?",
                                (source["id"],),
                            ).fetchone()[0]
                            self.assertEqual(Path(path),expected)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM hash_jobs WHERE status='HASHED'"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM hash_attempts WHERE status='HASHED'"
                    ).fetchone()[0],
                    2,
                )
                after=snapshot_database(database.connection)
                self.assertEqual(after["record_digests"],before["record_digests"])
            finally:
                database.close()

    def test_hash_queue_hashes_full_content_and_records_exact_duplicate_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_paths = [root / f"source-{index}" for index in range(1, 6)]
            for path in source_paths:
                path.mkdir()
            payload = (b"WinOLS full file content" * 5000)
            (source_paths[0] / "one.ols").write_bytes(payload)
            (source_paths[1] / "renamed.ols").write_bytes(payload)
            (source_paths[2] / "same-size.ols").write_bytes(b"x" * len(payload))
            database = IndexDatabase(root / "index.sqlite3")
            try:
                sources = [register_source(database, path) for path in source_paths]
                for slot_id, source in enumerate(sources, start=1):
                    database.configure_source_slot(slot_id, source["id"])
                    scan_registered_source(
                        database,source["id"],benchmark_bytes=0
                    )
                plan = build_hashing_plan(database)
                self.assertTrue(plan["all_sources_available"])
                self.assertEqual(plan["pending_locations"], 3)
                self.assertEqual(plan["unmaterialized_queue_jobs"], 3)
                result = run_hashing(
                    database,workers=2,chunk_size=64*1024,max_files=3
                )
                self.assertEqual(result["status"],"COMPLETE")
                self.assertEqual(result["files_completed"],3)
                self.assertEqual(result["bytes_read"],len(payload)*3)
                self.assertEqual(result["queue"]["totals"]["hashed"],3)
                self.assertEqual(
                    result["queue"]["deduplication"]["unique_ols_contents"],2
                )
                self.assertEqual(
                    result["queue"]["deduplication"]["exact_duplicate_locations"],1
                )
                group = database.list_duplicate_content_locations(limit=10)[0]
                self.assertEqual(group["locations_count"],2)
                self.assertEqual(
                    database.list_duplicate_content_locations(
                        limit=10,source_ids=[sources[0]["id"]]
                    ),
                    [],
                )
                self.assertEqual(group["physical_bytes"],len(payload)*2)
                self.assertEqual(
                    group["duplicate_bytes_saved_from_reprocessing"],len(payload)
                )
                attempts = database.connection.execute(
                    "SELECT COUNT(*),SUM(bytes_read) FROM hash_attempts WHERE status='HASHED'"
                ).fetchone()
                self.assertEqual(attempts[0],3)
                self.assertEqual(attempts[1],len(payload)*3)
                self.assertEqual(database.summary()["projects"],0)
                self.assertEqual(
                    build_hashing_report(database)["deduplication"]["unique_ols_bytes"],
                    len(payload)*2,
                )
                parsed=run_source_queue(
                    database,sources[0]["id"],reader=FixtureReader(),
                    max_file_bytes=1024*1024,batch_size=1,
                )
                self.assertEqual(parsed["cached_content_hashes_reused"],1)
                self.assertEqual(parsed["counts"],{"partial":1})
                duplicate=run_source_queue(
                    database,sources[1]["id"],reader=FixtureReader(),
                    max_file_bytes=1024*1024,batch_size=1,
                )
                self.assertEqual(
                    duplicate["counts"],{"duplicate_content_skipped":1}
                )
                again=run_hashing(
                    database,workers=1,chunk_size=64*1024,max_files=3
                )
                self.assertEqual(again["files_completed"],0)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM hash_attempts WHERE status='HASHED'"
                    ).fetchone()[0],3
                )
            finally:
                database.close()

    def test_hash_worker_never_commits_partial_or_changed_file_hash(self):
        from ols_platform.sources import _FileChangedDuringHash

        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            sources_paths=[root/f"source-{index}" for index in range(1,6)]
            for path in sources_paths:
                path.mkdir()
            (sources_paths[0]/"change.ols").write_bytes(b"before")
            database=IndexDatabase(root/"index.sqlite3")
            try:
                sources=[register_source(database,path) for path in sources_paths]
                for slot_id,source in enumerate(sources,start=1):
                    database.configure_source_slot(slot_id,source["id"])
                    scan_registered_source(database,source["id"],benchmark_bytes=0)
                with patch(
                    "ols_platform.sources._hash_location",
                    side_effect=_FileChangedDuringHash(
                        "changed mid-read",
                        started_at="2026-01-01T00:00:00+00:00",
                        finished_at="2026-01-01T00:00:01+00:00",
                        bytes_read=3,
                    ),
                ):
                    result=run_hashing(
                        database,workers=1,chunk_size=64*1024,max_files=1
                    )
                self.assertEqual(result["outcomes"],{"FILE_CHANGED":1})
                location=database.connection.execute(
                    "SELECT sha256 FROM file_locations WHERE filename='change.ols'"
                ).fetchone()
                self.assertIsNone(location["sha256"])
                attempt=database.connection.execute(
                    "SELECT status,bytes_read,started_at,finished_at FROM hash_attempts"
                ).fetchone()
                self.assertEqual(attempt["status"],"FILE_CHANGED")
                self.assertEqual(attempt["bytes_read"],3)
                self.assertNotEqual(attempt["started_at"],attempt["finished_at"])
            finally:
                database.close()

    def test_hashing_retries_after_source_disconnect_and_preserves_audit_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source_paths=[root/f"source-{index}" for index in range(1,6)]
            for path in source_paths:
                path.mkdir()
            target=source_paths[0]/"retry.ols"
            target.write_bytes(b"retry after restore")
            database=IndexDatabase(root/"index.sqlite3")
            try:
                sources=[register_source(database,path) for path in source_paths]
                for slot_id,source in enumerate(sources,start=1):
                    database.configure_source_slot(slot_id,source["id"])
                    scan_registered_source(database,source["id"],benchmark_bytes=0)
                before=database.connection.execute(
                    "SELECT COUNT(*) FROM reference_check_history"
                ).fetchone()[0]
                target.unlink()
                source_paths[0].rmdir()
                unavailable=run_hashing(
                    database,workers=1,max_files=1
                )
                self.assertEqual(
                    unavailable["queue"]["queue_statuses"]["SOURCE_UNAVAILABLE"],1
                )
                self.assertEqual(
                    unavailable["queue"]["totals"]["hashed"],0
                )
                source_paths[0].mkdir()
                target.write_bytes(b"retry after restore")
                scan_registered_source(
                    database,sources[0]["id"],benchmark_bytes=0
                )
                restored=run_hashing(
                    database,workers=1,max_files=1,retry_failed=True
                )
                self.assertEqual(restored["queue"]["totals"]["hashed"],1)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM hash_attempts"
                    ).fetchone()[0],1
                )
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM reference_check_history"
                    ).fetchone()[0],before
                )
            finally:
                database.close()

    def test_multi_source_inventory_preserves_locations_and_bounds_hashing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_paths = [root / f"source-{index}" for index in range(1, 6)]
            for path in source_paths:
                path.mkdir()
            (source_paths[0] / "shared.ols").write_bytes(b"same")
            (source_paths[1] / "shared.ols").write_bytes(b"same")
            (source_paths[2] / "shared.ols").write_bytes(b"diff")
            (source_paths[0] / "extra.bin").write_bytes(b"other")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                sources = [register_source(database, path) for path in source_paths]
                for slot_id, source in enumerate(sources, start=1):
                    database.configure_source_slot(slot_id, source["id"])
                report = run_multi_source_inventory(
                    database, progress_every=2, max_hash_files=2, max_hash_bytes=8
                )
                self.assertEqual(report["status"], "complete")
                self.assertEqual(len(report["sources"]), 5)
                self.assertEqual(report["totals"]["found_paths"], 4)
                self.assertEqual(report["totals"]["ols_paths"], 3)
                self.assertEqual(report["totals"]["physical_bytes"], 17)
                self.assertEqual(report["totals"]["ols_physical_bytes"], 12)
                self.assertEqual(report["hash_benchmark"]["files_hashed"], 2)
                self.assertLessEqual(report["hash_benchmark"]["bytes_read"], 8)
                self.assertEqual(report["totals"]["unhashed_ols_locations"], 1)
                self.assertEqual(len(report["possible_duplicate_groups"]), 1)
                self.assertEqual(
                    report["possible_duplicate_groups"][0]["locations_count"], 3
                )
                self.assertIn(
                    "not verified",
                    report["possible_duplicate_groups"][0]["basis"],
                )
                locations = database.list_inventory_locations(limit=100)
                self.assertEqual(len(locations), 4)
                self.assertEqual(
                    len({item["source_id"] for item in locations}), 3
                )
                exact = database.source_dedup_summary()
                self.assertEqual(exact["contents_in_multiple_locations"], 1)
                self.assertEqual(
                    report["extension_totals"]["ols"]["files"], 3
                )
            finally:
                database.close()

    def test_unavailable_source_does_not_remove_its_historical_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            available_path = root / "available"
            interrupted_path = root / "temporary-share"
            available_path.mkdir()
            interrupted_path.mkdir()
            (available_path / "active.ols").write_bytes(b"active")
            (interrupted_path / "remembered.ols").write_bytes(b"remembered")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                sources = [
                    register_source(database, available_path),
                    register_source(database, interrupted_path),
                ]
                for slot_id, source in enumerate(sources, start=1):
                    database.configure_source_slot(slot_id, source["id"])
                scan_registered_source(
                    database, sources[1]["id"], benchmark_bytes=0
                )
                interrupted_path.joinpath("remembered.ols").unlink()
                interrupted_path.rmdir()
                report = run_multi_source_inventory(
                    database, max_hash_files=1, max_hash_bytes=6
                )
                self.assertEqual(report["status"], "partial")
                unavailable = report["sources"][1]
                self.assertEqual(unavailable["status"], "unavailable")
                historical = database.connection.execute(
                    "SELECT is_present FROM file_locations WHERE source_id=?",
                    (sources[1]["id"],),
                ).fetchone()
                self.assertEqual(historical["is_present"], 1)
                self.assertEqual(report["totals"]["inaccessible_sources"], 4)
            finally:
                database.close()

    def test_metadata_scan_resumes_an_interrupted_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source"
            nested = source_path / "nested"
            nested.mkdir(parents=True)
            (nested / "resumed.ols").write_bytes(b"resume")
            database = IndexDatabase(root / "index.sqlite3")
            original_scandir = os.scandir
            calls = 0

            def interrupt_nested_scan(path):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise KeyboardInterrupt
                return original_scandir(path)

            try:
                source = register_source(database, source_path)
                with patch(
                    "ols_platform.sources.os.scandir",
                    side_effect=interrupt_nested_scan,
                ):
                    first = scan_registered_source(
                        database, source["id"], benchmark_bytes=0
                    )
                self.assertEqual(first["status"], "interrupted")
                resumed = scan_registered_source(
                    database, source["id"], benchmark_bytes=0
                )
                self.assertEqual(resumed["status"], "available")
                self.assertEqual(resumed["ols_paths"], 1)
                self.assertEqual(
                    database.list_inventory_locations(limit=10)[0]["filename"],
                    "resumed.ols",
                )
            finally:
                database.close()

    def test_partial_directory_inventory_is_reported_without_marking_source_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source"
            nested = source_path / "nested"
            nested.mkdir(parents=True)
            (source_path / "visible.ols").write_bytes(b"visible")
            (nested / "blocked.ols").write_bytes(b"not reached")
            database = IndexDatabase(root / "index.sqlite3")
            original_scandir = os.scandir
            calls = 0

            def deny_nested_scan(path):
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise PermissionError("fixture denies nested directory")
                return original_scandir(path)

            try:
                source = register_source(database, source_path)
                database.configure_source_slot(1, source["id"])
                with patch(
                    "ols_platform.sources.os.scandir",
                    side_effect=deny_nested_scan,
                ):
                    report = run_multi_source_inventory(
                        database, max_hash_files=1, max_hash_bytes=7
                    )
                self.assertEqual(report["status"], "partial")
                self.assertEqual(report["sources"][0]["status"], "scan_warnings")
                self.assertEqual(report["totals"]["found_paths"], 1)
                self.assertEqual(report["totals"]["sources_incomplete"], 1)
                self.assertEqual(report["totals"]["incomplete_directories"], 1)
                self.assertEqual(report["hash_benchmark"]["status"], "not_run")
                resumed = run_multi_source_inventory(
                    database, max_hash_files=1, max_hash_bytes=7
                )
                self.assertEqual(resumed["status"], "partial")
                self.assertEqual(resumed["sources"][0]["status"], "available")
                self.assertEqual(resumed["totals"]["found_paths"], 2)
            finally:
                database.close()

    def test_exact_content_dedup_keeps_every_location_and_distinguishes_same_size_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            roots = [root / f"root-{index}" for index in range(1, 4)]
            for path in roots:
                path.mkdir()
            (roots[0] / "same.ols").write_bytes(b"DATA")
            (roots[1] / "renamed.ols").write_bytes(b"DATA")
            (roots[2] / "same.ols").write_bytes(b"DIFF")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                sources = [register_source(database, path) for path in roots]
                for slot_id, source in enumerate(sources, start=1):
                    database.configure_source_slot(slot_id, source["id"])
                    scan = scan_registered_source(
                        database, source["id"], benchmark_bytes=0
                    )
                    self.assertEqual(scan["status"], "available")

                first = run_source_queue(
                    database, sources[0]["id"], reader=FixtureReader(),
                    max_file_bytes=1024, batch_size=1,
                )
                same_bytes = run_source_queue(
                    database, sources[1]["id"], reader=FixtureReader(),
                    max_file_bytes=1024, batch_size=1,
                )
                different_same_size = run_source_queue(
                    database, sources[2]["id"], reader=FixtureReader(),
                    max_file_bytes=1024, batch_size=1,
                )
                self.assertEqual(first["counts"], {"partial": 1})
                self.assertEqual(same_bytes["counts"], {"duplicate_content_skipped": 1})
                self.assertEqual(different_same_size["counts"], {"partial": 1})
                self.assertEqual(database.summary()["projects"], 2)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM file_contents WHERE file_kind='ols'"
                    ).fetchone()[0],
                    2,
                )
                locations = database.connection.execute(
                    """
                    SELECT path,sha256,content_id FROM file_locations
                    WHERE is_present=1 ORDER BY path
                    """
                ).fetchall()
                self.assertEqual(len(locations), 3)
                self.assertEqual(locations[0]["sha256"], locations[1]["sha256"])
                self.assertNotEqual(locations[0]["sha256"], locations[2]["sha256"])
                self.assertEqual(
                    len(database.list_duplicate_content_locations()), 1
                )
                self.assertEqual(
                    database.source_dedup_summary()["contents_in_multiple_locations"],
                    1,
                )
                second_scan = scan_registered_source(
                    database, sources[1]["id"], benchmark_bytes=0
                )
                self.assertEqual(second_scan["queue_pending"], 0)
                self.assertEqual(
                    database.list_index_queue(sources[1]["id"])[0]["status"],
                    "duplicate_content_skipped",
                )
            finally:
                database.close()

    def test_successful_scan_marks_only_its_missing_locations_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left"
            right = root / "right"
            left.mkdir()
            right.mkdir()
            (left / "left.ols").write_bytes(b"first")
            (right / "right.ols").write_bytes(b"second")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                sources = [register_source(database, path) for path in (left, right)]
                for source in sources:
                    scan_registered_source(database, source["id"], benchmark_bytes=0)
                (left / "left.ols").unlink()
                scan_registered_source(database, sources[0]["id"], benchmark_bytes=0)
                states = database.connection.execute(
                    "SELECT source_id,is_present FROM file_locations ORDER BY source_id"
                ).fetchall()
                self.assertEqual(
                    [(row["source_id"], row["is_present"]) for row in states],
                    [(sources[0]["id"], 0), (sources[1]["id"], 1)],
                )
                database.close()
                database = IndexDatabase(root / "index.sqlite3")
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM source_scan_runs"
                    ).fetchone()[0],
                    3,
                )
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM projects"
                    ).fetchone()[0],
                    0,
                )
            finally:
                database.close()

    def test_existing_queue_entries_are_backfilled_as_locations_without_hashing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "existing"
            source_path.mkdir()
            source_file = source_path / "queued.ols"
            source_file.write_bytes(b"not hashed during migration")
            database_path = root / "index.sqlite3"
            database = IndexDatabase(database_path)
            source = register_source(database, source_path)
            stats = source_file.stat()
            database.queue_source_file(
                source_id=source["id"],
                path=os.path.abspath(source_file),
                size_bytes=stats.st_size,
                modified_ns=stats.st_mtime_ns,
            )
            database.close()

            database = IndexDatabase(database_path)
            try:
                location = database.connection.execute(
                    "SELECT path,size_bytes,sha256,is_present FROM file_locations"
                ).fetchone()
                self.assertEqual(location["path"], os.path.abspath(source_file))
                self.assertEqual(location["size_bytes"], stats.st_size)
                self.assertIsNone(location["sha256"])
                self.assertEqual(location["is_present"], 1)
                self.assertEqual(
                    database.source_dedup_summary()["unhashed_ols_locations"], 1
                )
            finally:
                database.close()

    def test_failed_changed_content_is_not_linked_to_the_previous_project(self):
        class BrokenReader(FixtureReader):
            def inspect(self, source):
                raise ValueError("unreadable project")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "projects"
            source_path.mkdir()
            path = source_path / "changing.ols"
            path.write_bytes(b"first content")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                source = register_source(database, source_path)
                scan_registered_source(database, source["id"], benchmark_bytes=0)
                run_source_queue(
                    database, source["id"], reader=FixtureReader(),
                    max_file_bytes=1024, batch_size=1,
                )
                previous = database.connection.execute(
                    """
                    SELECT c.sha256,p.project_id FROM file_contents c
                    JOIN file_processing p ON p.content_id=c.id
                    WHERE p.project_id IS NOT NULL
                    """
                ).fetchone()
                path.write_bytes(b"different content")
                scan_registered_source(database, source["id"], benchmark_bytes=0)
                result = run_source_queue(
                    database, source["id"], reader=BrokenReader(),
                    max_file_bytes=1024, batch_size=1,
                )
                self.assertEqual(result["counts"], {"failed": 1})
                current = database.connection.execute(
                    """
                    SELECT c.sha256,p.project_id FROM file_contents c
                    JOIN file_processing p ON p.content_id=c.id
                    WHERE c.sha256!=?
                    """,
                    (previous["sha256"],),
                ).fetchone()
                self.assertIsNone(current["project_id"])
                self.assertEqual(database.summary()["projects"], 1)
            finally:
                database.close()

    def test_parser_fingerprint_change_queues_one_reprocessing_per_content(self):
        class UpdatedReader(FixtureReader):
            parser_version = "2"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "projects"
            source_path.mkdir()
            (source_path / "first.ols").write_bytes(b"identical project")
            (source_path / "copy.ols").write_bytes(b"identical project")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                source = register_source(database, source_path)
                scan_registered_source(database, source["id"], benchmark_bytes=0)
                first_run = run_source_queue(
                    database, source["id"], reader=FixtureReader(),
                    max_file_bytes=1024, batch_size=10,
                )
                self.assertEqual(first_run["counts"], {
                    "partial": 1,
                    "duplicate_content_skipped": 1,
                })
                updated = run_source_queue(
                    database, source["id"], reader=UpdatedReader(),
                    max_file_bytes=1024, batch_size=10,
                )
                self.assertEqual(updated["parser_fingerprint_reprocess_queued"], 1)
                self.assertEqual(updated["counts"], {"partial": 1})
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM file_processing"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(database.summary()["projects"], 1)
            finally:
                database.close()

    def test_source_registration_access_scan_and_resumable_index_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "projects"
            source.mkdir()
            ols_file = source / "one.ols"
            ols_file.write_bytes(b"fixture source")
            (source / "notes.txt").write_text("not parsed", encoding="utf-8")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                missing = register_source(database, root / "later")
                self.assertEqual(missing["status"], "unavailable")
                source_record = register_source(database, source)
                self.assertEqual(source_record["status"], "available")
                self.assertEqual(test_source(database, source_record["id"])["status"], "available")
                report = scan_registered_source(database, source_record["id"])
                self.assertEqual(report["status"], "available")
                self.assertEqual(report["files"], 2)
                self.assertEqual(report["ols_files"], 1)
                self.assertEqual(report["new_ols_files"], 1)
                self.assertEqual(report["queue_pending"], 1)
                self.assertEqual(
                    report["network_read_benchmark"]["bytes_read"],
                    len(b"fixture source"),
                )
                self.assertGreater(
                    report["network_read_benchmark"]["mib_per_second"], 0
                )

                first = run_source_queue(
                    database,
                    source_record["id"],
                    reader=FixtureReader(),
                    max_file_bytes=1024,
                    batch_size=1,
                )
                self.assertEqual(first["counts"], {"partial": 1})
                self.assertEqual(first["remaining"], 0)
                second = run_source_queue(
                    database,
                    source_record["id"],
                    reader=FixtureReader(),
                    max_file_bytes=1024,
                    batch_size=1,
                )
                self.assertEqual(second["processed"], 0)
                self.assertEqual(database.summary()["projects"], 1)
                self.assertEqual(database.list_sources()[1]["found_files"], 2)
                self.assertEqual(database.list_sources()[1]["indexed_files"], 1)

                unchanged_scan = scan_registered_source(
                    database, source_record["id"], benchmark_bytes=0
                )
                self.assertEqual(unchanged_scan["queue_pending"], 0)
                self.assertEqual(unchanged_scan["cached_fingerprint_files"], 1)
                self.assertIn(
                    "Stat matches are not treated as content identity",
                    unchanged_scan["note"],
                )
                ols_file.write_bytes(b"fixture source has changed size")
                changed_scan = scan_registered_source(
                    database, source_record["id"], benchmark_bytes=0
                )
                self.assertEqual(changed_scan["stat_changed_ols_files"], 1)
                self.assertEqual(changed_scan["queue_pending"], 1)
                changed_import = run_source_queue(
                    database,
                    source_record["id"],
                    reader=FixtureReader(),
                    max_file_bytes=1024,
                    batch_size=1,
                )
                self.assertEqual(changed_import["counts"], {"partial": 1})
                self.assertEqual(database.summary()["projects"], 1)
            finally:
                database.close()

    def test_moved_and_removed_sources_require_real_access_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old"
            new_path = root / "new"
            old_path.mkdir()
            new_path.mkdir()
            database = IndexDatabase(root / "index.sqlite3")
            try:
                source = register_source(database, old_path)
                moved = move_source(database, source["id"], new_path)
                self.assertEqual(
                    os.path.normcase(moved["path"]),
                    # Stored source roots are canonical (symlinks and Windows
                    # 8.3 short names expanded), matching Path.resolve().
                    os.path.normcase(str(new_path.resolve())),
                )
                self.assertEqual(test_source(database, source["id"])["status"], "available")
                new_path.rmdir()
                unavailable = test_source(database, source["id"])
                self.assertEqual(unavailable["status"], "unavailable")
                self.assertIn("does not currently exist", unavailable["last_error"])
                with self.assertRaisesRegex(OSError, "unavailable"):
                    scan_registered_source(database, source["id"])
            finally:
                database.close()

    def test_temporary_disconnect_is_retryable_and_resumes_after_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "network-share"
            source_path.mkdir()
            ols_file = source_path / "recover.ols"
            ols_file.write_bytes(b"recoverable project")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                source = register_source(database, source_path)
                scan = scan_registered_source(
                    database, source["id"], benchmark_bytes=0
                )
                self.assertEqual(scan["queue_pending"], 1)
                database.connection.execute(
                    "UPDATE index_queue SET status='in_progress' WHERE source_id=?",
                    (source["id"],),
                )
                database.connection.commit()
                ols_file.unlink()
                source_path.rmdir()
                unavailable = test_source(database, source["id"])
                self.assertEqual(unavailable["status"], "unavailable")
                self.assertEqual(database.summary()["projects"], 0)
                self.assertEqual(
                    database.list_index_queue(source["id"])[0]["status"], "in_progress"
                )
                with self.assertRaisesRegex(OSError, "unavailable"):
                    run_source_queue(
                        database,
                        source["id"],
                        reader=FixtureReader(),
                        max_file_bytes=1024,
                        batch_size=1,
                    )
                self.assertEqual(
                    database.list_index_queue(source["id"])[0]["status"],
                    "interrupted",
                )
                source_path.mkdir()
                ols_file.write_bytes(b"recoverable project")
                resumed = scan_registered_source(
                    database, source["id"], benchmark_bytes=0
                )
                self.assertEqual(resumed["queue_pending"], 1)
                result = run_source_queue(
                    database,
                    source["id"],
                    reader=FixtureReader(),
                    max_file_bytes=1024,
                    batch_size=1,
                )
                self.assertEqual(result["counts"], {"partial": 1})
                self.assertEqual(database.summary()["projects"], 1)
            finally:
                database.close()

    def test_permission_failure_is_reported_as_source_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "restricted"
            source_path.mkdir()
            database = IndexDatabase(root / "index.sqlite3")
            try:
                source = register_source(database, source_path)
                with patch(
                    "ols_platform.sources.os.scandir",
                    side_effect=PermissionError("read permission denied"),
                ):
                    checked = test_source(database, source["id"])
                self.assertEqual(checked["status"], "unavailable")
                self.assertIn("PermissionError", checked["last_error"])
            finally:
                database.close()

    def test_exact_filters_search_provenance_and_parser_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "one.ols"
            source.write_bytes(b"some project data")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                outcome = next(import_sources(
                    sources=[source],
                    reader=FixtureReader(),
                    database=database,
                    max_file_bytes=1024,
                    progress=False,
                ))
                self.assertEqual(outcome.status, "partial")
                result = search_projects(
                    database,
                    metadata={"ecu_type": "ECU-A", "sw_number": "SW-123"},
                    map_name="Fuel map",
                    columns=8,
                    rows=4,
                )
                self.assertEqual(result["result_count"], 1)
                project = result["results"][0]
                self.assertEqual(project["confidence"], "LIKELY")
                self.assertEqual(project["versions"][0]["name"], "Original")
                self.assertIn("not a technical tune", project["match_reason"])
                self.assertEqual(result["ranker"], "deterministic-evidence")
                self.assertGreater(project["rank_score"], 0)
                self.assertTrue(project["ranking_uncertainties"])

                coverage = parser_coverage(database)
                self.assertEqual(coverage["project_count"], 1)
                self.assertEqual(coverage["formats"][0]["format_version"], 503)
                self.assertEqual(
                    coverage["current_problem_groups"][0]["code"], "unresolved_fixture"
                )
                with self.assertRaisesRegex(ValueError, "Unsupported metadata"):
                    search_projects(database, metadata={"password": "no"})
                candidate_id = database.record_training_candidate(
                    project_id=1,
                    version_id=1,
                    provenance="unit test fixture",
                    validation_status="INCONCLUSIVE",
                )
                self.assertEqual(database.list_training_candidates()[0]["id"], candidate_id)
                with self.assertRaisesRegex(ValueError, "does not belong"):
                    database.record_training_candidate(
                        project_id=1,
                        version_id=999,
                        provenance="bad reference",
                        validation_status="NOT_VALIDATED",
                    )
            finally:
                database.close()

    def test_project_search_filters_pages_and_sees_new_complete_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = []
            for index in range(3):
                source = root / f"search-{index}.ols"
                source.write_bytes(bytes([65 + index]) * (128 * (index + 1)))
                files.append(source)
            database = IndexDatabase(root / "index.sqlite3")
            writer = None
            try:
                outcomes = list(import_sources(
                    sources=files,
                    reader=FixtureReader(),
                    database=database,
                    max_file_bytes=1024,
                    progress=False,
                ))
                self.assertEqual(len(outcomes), 3)
                source = register_source(database, root)
                scan_registered_source(
                    database, source["id"], benchmark_bytes=0
                )
                projects = {
                    row["path"]: row["id"]
                    for row in database.connection.execute(
                        "SELECT id,path FROM source_files"
                    )
                }
                for index, file_path in enumerate(files):
                    project_id = projects[str(file_path.resolve())]
                    with database.connection:
                        database.connection.execute(
                            """
                            UPDATE project_metadata SET value=?
                            WHERE project_id=? AND field='sw_number'
                            """,
                            (f"SW-SEARCH-{index}", project_id),
                        )
                target_path = str(files[1].resolve())
                target_project = projects[target_path]
                content_hash = hashlib.sha256(files[1].read_bytes()).hexdigest()
                with database.connection:
                    database.connection.execute(
                        "UPDATE source_files SET sha256=? WHERE id=?",
                        ("0" * 64, target_project),
                    )
                self.assertEqual(
                    search_projects(database, file_hash=content_hash)["results"],
                    [],
                )

                writer = IndexDatabase(root / "index.sqlite3")
                with writer.connection:
                    writer.connection.execute(
                        """
                        UPDATE file_locations SET sha256=?
                        WHERE path=? AND file_kind='ols'
                        """,
                        (content_hash, target_path),
                    )
                exact_hash = search_projects(
                    database, file_hash=content_hash
                )
                self.assertEqual(exact_hash["result_count"], 1)
                self.assertEqual(
                    exact_hash["results"][0]["project_id"], target_project
                )
                self.assertEqual(exact_hash["results"][0]["match_status"], "CONFIRMED")
                self.assertEqual(
                    exact_hash["results"][0]["source"]["hash_provenance"],
                    "live_file_location",
                )
                self.assertFalse(
                    exact_hash["results"][0]["source"][
                        "parser_snapshot_matches_current_content"
                    ]
                )
                self.assertIn(
                    "file_hash",
                    [item["field"] for item in exact_hash["results"][0]["matched_features"]],
                )

                with writer.connection:
                    writer.connection.execute(
                        "UPDATE file_locations SET is_present=0 WHERE path=? AND file_kind='ols'",
                        (target_path,),
                    )
                self.assertEqual(
                    search_projects(database, file_hash=content_hash)["results"],
                    [],
                )
                with writer.connection:
                    writer.connection.execute(
                        "UPDATE file_locations SET is_present=1 WHERE path=? AND file_kind='ols'",
                        (target_path,),
                    )

                first_page = search_projects(database, limit=1)
                second_page = search_projects(database, limit=1, offset=1)
                final_page = search_projects(database, limit=1, offset=2)
                self.assertEqual(first_page["result_count"], 1)
                self.assertTrue(first_page["has_more"])
                self.assertEqual(second_page["offset"], 1)
                self.assertTrue(second_page["has_more"])
                self.assertFalse(final_page["has_more"])
                self.assertEqual(
                    len({
                        first_page["results"][0]["project_id"],
                        second_page["results"][0]["project_id"],
                        final_page["results"][0]["project_id"],
                    }),
                    3,
                )

                combined = search_projects(
                    database,
                    metadata={"ecu_type": " ECU-A ", "sw_number": "SW-SEARCH-0"},
                    map_text="Fuel",
                    columns=8,
                    rows=4,
                )
                self.assertEqual(combined["result_count"], 1)
                result = combined["results"][0]
                self.assertEqual(result["metadata"]["sw_number"], "SW-SEARCH-0")
                self.assertEqual(result["maps"][0]["axes_count"], 0)
                self.assertFalse(result["values_reliably_decoded"])
                self.assertTrue(result["missing_information"])
                self.assertTrue(result["uncertainties"])
                self.assertGreaterEqual(combined["query_seconds"], 0)

                size_match = search_projects(
                    database, min_file_size=200, max_file_size=300
                )
                self.assertEqual(size_match["result_count"], 1)
                path_match = search_projects(
                    database, source_path="search-2.ols"
                )
                self.assertEqual(path_match["result_count"], 1)
                parser_status = result["parser"]["status"]
                self.assertEqual(
                    search_projects(database, parser_status=parser_status)[
                        "result_count"
                    ],
                    3,
                )
                with patch(
                    "ols_platform.project_search._SLOW_SEARCH_SECONDS",0
                ):
                    with self.assertLogs(
                        "ols_platform.project_search", level="WARNING"
                    ):
                        search_projects(database, limit=1)
                with self.assertRaisesRegex(ValueError, "64 hexadecimal"):
                    search_projects(database, file_hash="not-a-hash")
                with self.assertRaisesRegex(ValueError, "cannot exceed"):
                    search_projects(
                        database, min_file_size=400, max_file_size=200
                    )
            finally:
                if writer is not None:
                    writer.close()
                database.close()

    def test_any_metadata_search_ranks_before_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strong_match = root / "strong-match.ols"
            weak_match = root / "weak-match.ols"
            strong_match.write_bytes(b"strong project")
            weak_match.write_bytes(b"weak project")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                outcomes = list(import_sources(
                    sources=[strong_match, weak_match],
                    reader=FixtureReader(),
                    database=database,
                    max_file_bytes=1024,
                    progress=False,
                ))
                self.assertEqual(len(outcomes), 2)
                project_ids = {
                    Path(row["path"]).name: int(row["id"])
                    for row in database.connection.execute(
                        """
                        SELECT p.id,s.path
                        FROM projects p JOIN source_files s ON s.id=p.source_id
                        """
                    )
                }
                with database.connection:
                    database.connection.execute(
                        """
                        UPDATE project_metadata SET value='SW-OTHER'
                        WHERE project_id=? AND field='sw_number'
                        """,
                        (project_ids[weak_match.name],),
                    )

                filters = {
                    "metadata": {
                        "ecu_type": "ECU-A",
                        "sw_number": "SW-123",
                    },
                    "metadata_match_mode": "any",
                }
                first_page = search_projects(database, **filters, limit=1)
                second_page = search_projects(
                    database, **filters, limit=1, offset=1
                )

                self.assertEqual(first_page["result_count"], 1)
                self.assertTrue(first_page["has_more"])
                self.assertEqual(
                    first_page["results"][0]["project_id"],
                    project_ids[strong_match.name],
                )
                self.assertEqual(
                    second_page["results"][0]["project_id"],
                    project_ids[weak_match.name],
                )
                self.assertGreater(
                    first_page["results"][0]["rank_score"],
                    second_page["results"][0]["rank_score"],
                )
                self.assertEqual(
                    first_page["ranking_scope"],
                    "whole_filtered_result_set",
                )
                empty_filter = search_projects(
                    database,
                    metadata={"sw_number": " "},
                    limit=2,
                )
                self.assertEqual(empty_filter["result_count"], 2)
                self.assertTrue(
                    all(result["rank_score"] == 0 for result in empty_filter["results"])
                )
            finally:
                database.close()

    def test_project_search_surfaces_sqlite_read_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            database = IndexDatabase(Path(directory) / "index.sqlite3")
            database.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                search_projects(database)

    def test_file_hash_search_uses_latest_live_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "child"
            child.mkdir()
            source = child / "overlap.ols"
            source.write_bytes(b"synthetic project")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                next(import_sources(
                    sources=[source], reader=FixtureReader(), database=database,
                    max_file_bytes=1024, progress=False,
                ))
                registered = [register_source(database, path) for path in (root, child)]
                for item in registered:
                    scan_registered_source(database, item["id"], benchmark_bytes=0)
                with database.connection:
                    for item, digest in zip(registered, ("a" * 64, "b" * 64)):
                        database.connection.execute(
                            "UPDATE file_locations SET sha256=? "
                            "WHERE source_id=? AND path=?",
                            (digest, item["id"], str(source.resolve())),
                        )
                self.assertEqual(
                    search_projects(database, file_hash="a" * 64)["result_count"], 0
                )
                latest = search_projects(database, file_hash="b" * 64)
                self.assertEqual(latest["result_count"], 1)
                self.assertEqual(latest["results"][0]["match_status"], "CONFIRMED")
            finally:
                database.close()

    def test_partial_metadata_search_reports_only_actual_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "example.ols"
            source.write_bytes(b"synthetic project")
            database = IndexDatabase(root / "index.sqlite3")
            try:
                next(import_sources(
                    sources=[source], reader=FixtureReader(), database=database,
                    max_file_bytes=1024, progress=False,
                ))
                criteria = {
                    "ecu_type": "ECU-A",
                    "sw_number": "different",
                    "hw_number": "missing",
                }
                first = search_projects(
                    database, metadata=criteria, metadata_match_mode="any"
                )["results"][0]
                second = search_projects(
                    database, metadata=criteria, metadata_match_mode="any"
                )["results"][0]
                self.assertEqual(
                    first["matched_features"],
                    [{"field": "ecu_type", "matched_value": "ECU-A"}],
                )
                self.assertIn("At least one", first["match_reason"])
                self.assertIn("Differing metadata filters: sw_number.", first["match_reason"])
                self.assertIn("Missing metadata filters: hw_number.", first["match_reason"])
                self.assertIn("hw_number", first["missing_information"])
                self.assertTrue(first["parser_warnings"])
                self.assertEqual(first["rank_score"], second["rank_score"])
                self.assertEqual(first["rank_reasons"], second["rank_reasons"])
                all_matched = search_projects(
                    database,
                    metadata={"ecu_type": "ECU-A", "sw_number": "SW-123"},
                    metadata_match_mode="any",
                )["results"][0]
                self.assertIn("All supplied metadata filters matched.", all_matched["match_reason"])
                with database.connection:
                    database.connection.execute(
                        "INSERT OR REPLACE INTO project_metadata "
                        "(project_id,field,value,status) VALUES (1,'hw_number','HW-1','direct')"
                    )
                conflicting = search_projects(
                    database,
                    metadata={
                        "ecu_type": "ECU-A",
                        "sw_number": "SW-123",
                        "hw_number": "HW-OTHER",
                    },
                    metadata_match_mode="any",
                )["results"][0]
                self.assertEqual(conflicting["match_status"], "INCONCLUSIVE")
                self.assertIn("Differing metadata filters: hw_number.", conflicting["match_reason"])
                self.assertEqual(
                    search_projects(database, metadata=criteria)["result_count"], 0
                )
            finally:
                database.close()

    def test_partial_metadata_ranking_is_stable_across_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = IndexDatabase(root / "index.sqlite3")
            try:
                for name in ("best.ols", "weaker.ols", "tied.ols"):
                    source = root / name
                    source.write_bytes(name.encode())
                    next(import_sources(
                        sources=[source], reader=FixtureReader(), database=database,
                        max_file_bytes=1024, progress=False,
                    ))
                with database.connection:
                    database.connection.execute(
                        "UPDATE project_metadata SET value='other' "
                        "WHERE project_id=2 AND field='sw_number'"
                    )
                criteria = {"ecu_type": "ECU-A", "sw_number": "SW-123"}
                pages = [
                    search_projects(
                        database, metadata=criteria, metadata_match_mode="any",
                        limit=1, offset=offset,
                    )["results"][0]
                    for offset in range(3)
                ]
                self.assertEqual([item["project_id"] for item in pages], [1, 3, 2])
                self.assertGreater(pages[1]["rank_score"], pages[2]["rank_score"])
                self.assertEqual(pages[0]["rank_score"], pages[1]["rank_score"])
            finally:
                database.close()

    def test_project_search_aborts_interrupted_sqlite_statement(self):
        class InterruptedConnection:
            def __init__(self):
                self.progress_handler = None

            def set_progress_handler(self, handler, _interval):
                self.progress_handler = handler

            def execute(self, _query, _parameters):
                raise sqlite3.OperationalError("interrupted")

        connection = InterruptedConnection()
        with self.assertRaisesRegex(TimeoutError, "refine the search"):
            search_projects(SimpleNamespace(connection=connection))
        self.assertIsNone(connection.progress_handler)


if __name__ == "__main__":
    unittest.main()
