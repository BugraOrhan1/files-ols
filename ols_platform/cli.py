from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from .compare import (
    byte_similarity,
    compare_binary_files,
    store_comparison,
    validate_byte_threshold,
)
from .database import IndexDatabase
from .dashboard import serve
from .ecu_workbench import (
    _hash_file,
    export_candidates,
    export_preview,
    export_selected_binary,
    import_binary_stream,
)
from .importer import import_sources
from .inventory import scan_inventory
from .export_queue import plan_export_tasks, write_manual_export_batch
from .project_matching import compare_projects, find_project_matches
from .project_search import parser_coverage, search_projects
from .sources import (
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
from .reader import PyOlsReader
from .binary_reference_analysis import (
    analyze_project,
    store_analysis_report,
    write_analysis_outputs,
)
from .reference_package import (
    audit_reference_package,
    build_package_report,
    compare_map_metadata,
    compare_project_metadata,
    import_reference_package,
    inspect_package,
    match_package_versions,
    write_package_template,
)
from .winols_reference import (
    build_project_report,
    build_reference_summary,
    import_manifest,
    validate_project,
    write_templates,
)


def _default_database() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "ols-index.sqlite3"


def _write_report(
    path: Path,
    contents: str,
    *,
    protected_paths: list[Path],
) -> None:
    output_path = path.resolve()
    protected = {item.resolve() for item in protected_paths}
    if output_path in protected:
        raise ValueError(f"Refusing to overwrite protected input: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8", newline="\n") as report_file:
        report_file.write(contents + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ols-platform",
        description="Local-first WinOLS project indexer and binary comparison tools.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=_default_database(),
        help="SQLite index path (default: ./data/ols-index.sqlite3).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init-db", help="Create or validate the local index.")
    initialize.set_defaults(action="init")

    ecu_export = commands.add_parser(
        "ecu-bin-export",
        help="Interactively select and copy an indexed BIN research candidate.",
    )
    ecu_export.add_argument("original", type=Path, help="Original BIN to import read-only.")
    ecu_export.add_argument("--output-dir", type=Path, required=True)
    ecu_export.add_argument("--output-name", default="")
    ecu_export.add_argument("--label", default="", help="Optional user-supplied label.")
    for field in ("ecu_type", "ecu_manufacturer", "hw_number", "hw_code", "sw_number", "calibration_id"):
        ecu_export.add_argument("--" + field.replace("_", "-"), default="")
    ecu_export.set_defaults(action="ecu-bin-export")

    importing = commands.add_parser(
        "import", help="Incrementally index .ols files or source directories."
    )
    importing.add_argument("sources", nargs="+", type=Path)
    importing.add_argument(
        "--py-ols-path",
        type=Path,
        help="Path to a separate py-ols checkout or its ols_reader.py file.",
    )
    importing.add_argument(
        "--max-file-bytes",
        type=int,
        default=256 * 1024 * 1024,
        help="Reject larger inputs before invoking py-ols (default: 256 MiB).",
    )
    importing.set_defaults(action="import")

    inventory = commands.add_parser(
        "inventory",
        help="Dry-run filesystem inventory and compare OLS paths with the index.",
    )
    inventory.add_argument("roots", nargs="+", type=Path)
    inventory.add_argument(
        "--hash-duplicate-candidates",
        action="store_true",
        help="Hash same-size candidates to confirm exact duplicates.",
    )
    inventory.add_argument(
        "--verify-indexed-hashes",
        action="store_true",
        help="Re-hash indexed OLS files to verify content (may read terabytes).",
    )
    inventory.add_argument(
        "--file-list",
        type=Path,
        help="Optional JSONL file containing per-file inventory observations.",
    )
    inventory.add_argument("--report", type=Path, help="Optional JSON summary path.")
    inventory.set_defaults(action="inventory")

    export_queue = commands.add_parser(
        "export-queue",
        help="Generate minimal reference-export tasks or list the existing queue.",
    )
    export_queue.add_argument(
        "--list-only",
        action="store_true",
        help="Show the persisted queue without generating new tasks.",
    )
    export_queue.add_argument("--status")
    export_queue.add_argument("--limit", type=int, default=500)
    export_queue.set_defaults(action="export-queue")

    export_batch = commands.add_parser(
        "export-batch",
        help="Write a manual WinOLS export batch and checklist from queued tasks.",
    )
    export_batch.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data") / "export-batches",
    )
    export_batch.add_argument("--status", default="pending")
    export_batch.set_defaults(action="export-batch")

    export_task_update = commands.add_parser(
        "export-task-update",
        help="Record a valid export task transition and append an attempt result.",
    )
    export_task_update.add_argument("task_id", type=int)
    export_task_update.add_argument(
        "status",
        choices=("in_progress", "pending", "completed", "blocked", "cancelled"),
    )
    export_task_update.add_argument("result")
    export_task_update.set_defaults(action="export-task-update")

    export_task_history = commands.add_parser(
        "export-task-history",
        help="Show the append-only status and attempt history for an export task.",
    )
    export_task_history.add_argument("task_id", type=int)
    export_task_history.add_argument("--limit", type=int, default=100)
    export_task_history.set_defaults(action="export-task-history")

    status = commands.add_parser("status", help="Show index totals.")
    status.set_defaults(action="status")

    dashboard = commands.add_parser(
        "serve", help="Open the read-only local dashboard on loopback."
    )
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.set_defaults(action="serve")

    projects = commands.add_parser(
        "projects", help="List indexed projects or search metadata."
    )
    projects.add_argument("--query", default="", help="Search path, ECU or vehicle metadata.")
    projects.add_argument("--limit", type=int, default=100)
    projects.set_defaults(action="projects")

    find_matches = commands.add_parser(
        "find-projects",
        help="Find evidence-ranked candidates for an indexed project.",
    )
    find_matches.add_argument("--project-id", type=int, required=True)
    find_matches.add_argument("--limit", type=int, default=100)
    find_matches.set_defaults(action="find-projects")

    compare_projects_command = commands.add_parser(
        "compare-projects",
        help="Compare project metadata and parser-derived slot fingerprints.",
    )
    compare_projects_command.add_argument("left_project_id", type=int)
    compare_projects_command.add_argument("right_project_id", type=int)
    compare_projects_command.set_defaults(action="compare-projects")

    show_project = commands.add_parser(
        "show-project", help="Show metadata, versions, maps and axes for one project."
    )
    show_project.add_argument("project_id", type=int)
    show_project.set_defaults(action="show-project")

    diagnose = commands.add_parser(
        "diagnose", help="Show the stored import-quality report for one project."
    )
    diagnose.add_argument("project_id", type=int)
    diagnose.set_defaults(action="diagnose")

    reference_template = commands.add_parser(
        "reference-template",
        help="Generate editable manifests listing required WinOLS references.",
    )
    reference_template.add_argument("--project-id", type=int)
    reference_template.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reference_data") / "projects",
    )
    reference_template.set_defaults(action="reference-template")

    reference_import = commands.add_parser(
        "reference-import",
        help="Import a manifest and hash its local WinOLS export artifacts.",
    )
    reference_import.add_argument("manifest", type=Path)
    reference_import.set_defaults(action="reference-import")

    reference_validate = commands.add_parser(
        "reference-validate",
        help="Compare mapped references and report binary slot candidates.",
    )
    reference_validate.add_argument("--project-id", type=int)
    reference_validate.set_defaults(action="reference-validate")

    reference_report = commands.add_parser(
        "reference-report",
        help="Print one project's WinOLS reference validation report.",
    )
    reference_report.add_argument("project_id", type=int)
    reference_report.add_argument("--output", type=Path)
    reference_report.set_defaults(action="reference-report")

    reference_summary = commands.add_parser(
        "reference-summary",
        help="Summarize WinOLS reference coverage across indexed projects.",
    )
    reference_summary.add_argument("--output", type=Path)
    reference_summary.set_defaults(action="reference-summary")

    reference_analyze = commands.add_parser(
        "reference-analyze",
        help="Run an auditable, chunked binary export-to-slot analysis.",
    )
    reference_analyze.add_argument("--project-id", type=int)
    reference_analyze.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data") / "reference-reports" / "analysis",
    )
    reference_analyze.add_argument("--chunk-bytes", type=int, default=1024 * 1024)
    reference_analyze.add_argument("--max-ranges", type=int, default=100_000)
    reference_analyze.set_defaults(action="reference-analyze")

    match_candidates = commands.add_parser(
        "match-candidates",
        help="List version-slot candidates and their latest manual reviews.",
    )
    match_candidates.add_argument("--project-id", type=int)
    match_candidates.add_argument("--limit", type=int, default=500)
    match_candidates.set_defaults(action="match-candidates")

    match_review = commands.add_parser(
        "match-review",
        help="Append an evidence-backed manual version-match review.",
    )
    match_review.add_argument("candidate_id", type=int)
    match_review.add_argument(
        "decision", choices=("confirm_version", "reject", "defer")
    )
    match_review.add_argument("evidence")
    match_review.add_argument("--reviewer", default="local-operator")
    match_review.set_defaults(action="match-review")

    package_template = commands.add_parser(
        "reference-package-template",
        help="Create a WinOLS export packet template and checklist for a project.",
    )
    package_template.add_argument("project_id", type=int)
    package_template.add_argument(
        "--output-dir",
        type=Path,
        help="Packet directory (default: data/reference-packets/<project_id>).",
    )
    package_template.set_defaults(action="reference-package-template")

    package_inspect = commands.add_parser(
        "reference-package-inspect",
        help="Inspect a reference packet and verify its source, files, sizes and hashes.",
    )
    package_inspect.add_argument("manifest", type=Path)
    package_inspect.add_argument("--output", type=Path)
    package_inspect.set_defaults(action="reference-package-inspect")

    package_audit = commands.add_parser(
        "reference-package-audit",
        help="Run canonical reference checks without writing to the SQLite index.",
    )
    package_audit.add_argument("manifest", type=Path)
    package_audit.add_argument("--output", type=Path)
    package_audit.set_defaults(action="reference-package-audit")

    package_import = commands.add_parser(
        "reference-package-import",
        help="Import a complete, hash-verified reference packet.",
    )
    package_import.add_argument("manifest", type=Path)
    package_import.set_defaults(action="reference-package-import")

    version_match = commands.add_parser(
        "reference-version-match",
        help="Compare reviewed WinOLS version exports with each OLS binary slot.",
    )
    version_match.add_argument("manifest", type=Path)
    version_match.add_argument("--chunk-bytes", type=int, default=1024 * 1024)
    version_match.add_argument("--max-ranges", type=int, default=100_000)
    version_match.add_argument("--output", type=Path)
    version_match.set_defaults(action="reference-version-match")

    map_compare = commands.add_parser(
        "reference-map-compare",
        help="Compare manually reviewed WinOLS map fields without assuming address semantics.",
    )
    map_compare.add_argument("manifest", type=Path)
    map_compare.add_argument("--output", type=Path)
    map_compare.set_defaults(action="reference-map-compare")

    metadata_compare = commands.add_parser(
        "reference-metadata-compare",
        help="Compare reviewed WinOLS project metadata with parser metadata.",
    )
    metadata_compare.add_argument("manifest", type=Path)
    metadata_compare.add_argument("--output", type=Path)
    metadata_compare.set_defaults(action="reference-metadata-compare")

    package_report = commands.add_parser(
        "reference-package-report",
        help="Show parser, external packet data, missing references and audited runs.",
    )
    package_report.add_argument("project_id", type=int)
    package_report.add_argument("--manifest", type=Path)
    package_report.add_argument("--output", type=Path)
    package_report.set_defaults(action="reference-package-report")

    compare = commands.add_parser(
        "compare",
        help="Compare two binary files without modifying either input.",
    )
    compare.add_argument("original", type=Path)
    compare.add_argument("tuned", type=Path)
    compare.add_argument("--chunk-bytes", type=int, default=1024 * 1024)
    compare.add_argument("--max-ranges", type=int, default=100_000)
    compare.add_argument(
        "--byte-threshold", type=float, default=95.0,
        help="Byte similarity threshold for the selected pair, 0-100 (default: 95).",
    )
    compare.add_argument(
        "--no-store",
        action="store_true",
        help="Do not create or write to the SQLite index.",
    )
    compare.add_argument("--output", type=Path)
    compare.set_defaults(action="compare")

    sources = commands.add_parser(
        "sources", help="Register and inspect filesystem data sources."
    )
    source_commands = sources.add_subparsers(dest="source_action", required=True)
    source_add = source_commands.add_parser("add", help="Register a directory; check access now.")
    source_add.add_argument("path", type=Path)
    source_add.set_defaults(action="source-add")
    source_list = source_commands.add_parser("list", help="List registered sources and status.")
    source_list.set_defaults(action="source-list")
    source_slots = source_commands.add_parser(
        "slots", help="Show the five named source roots and content-deduplication totals."
    )
    source_slots.set_defaults(action="source-slots")
    source_configure = source_commands.add_parser(
        "configure", help="Assign a real directory path to one of the five named roots."
    )
    source_configure.add_argument(
        "slot", choices=("Database 1", "Database 2", "Database 3", "Database 4", "default")
    )
    source_configure.add_argument("path", type=Path)
    source_configure.set_defaults(action="source-configure")
    source_test = source_commands.add_parser("test", help="Recheck actual directory access.")
    source_test.add_argument("source_id", type=int)
    source_test.set_defaults(action="source-test")
    source_move = source_commands.add_parser("move", help="Update a source after it is moved.")
    source_move.add_argument("source_id", type=int)
    source_move.add_argument("path", type=Path)
    source_move.set_defaults(action="source-move")
    source_relocate = source_commands.add_parser(
        "relocate-roots",
        help="Rebase indexed paths to verified local roots on the database host.",
    )
    source_relocate.add_argument("root_config", type=Path)
    source_relocate.set_defaults(action="source-relocate-roots")
    source_scan = source_commands.add_parser(
        "scan", help="Dry-run a registered source and persist discovered OLS work."
    )
    source_scan.add_argument("source_id", type=int)
    source_scan.add_argument("--progress-every", type=int, default=10000)
    source_scan.add_argument(
        "--recheck",
        action="store_true",
        help="Queue all OLS files, including metadata-fingerprint matches, for hash verification.",
    )
    source_scan.add_argument(
        "--benchmark-bytes",
        type=int,
        default=16 * 1024 * 1024,
        help="Read at most this many bytes from the first OLS file for throughput measurement.",
    )
    source_scan.set_defaults(action="source-scan")
    multi_source_inventory = commands.add_parser(
        "source-inventory",
        help="Recursively inventory all five configured roots and run a bounded OLS hash sample.",
    )
    multi_source_inventory.add_argument("--progress-every", type=int, default=10000)
    multi_source_inventory.add_argument("--max-hash-files", type=int, default=25)
    multi_source_inventory.add_argument(
        "--max-hash-bytes",
        type=int,
        default=2 * 1024 * 1024 * 1024,
        help="Maximum total bytes read by the full-file hash benchmark (at most 2 GiB).",
    )
    multi_source_inventory.add_argument(
        "--report",
        type=Path,
        default=Path("data") / "inventory-reports" / "multi-source-inventory.json",
    )
    multi_source_inventory.set_defaults(action="source-inventory")
    hashing = commands.add_parser(
        "hashing", help="Inspect and process the persistent full-file SHA-256 queue."
    )
    hashing_commands = hashing.add_subparsers(dest="hashing_action",required=True)
    hashing_plan = hashing_commands.add_parser(
        "plan",help="Dry-run queue size and verify the five source roots."
    )
    hashing_plan.set_defaults(action="hashing-plan")
    hashing_status = hashing_commands.add_parser(
        "status",help="Show hash queue, source progress, and recent throughput."
    )
    hashing_status.set_defaults(action="hashing-status")
    hashing_run = hashing_commands.add_parser(
        "run",help="Hash a bounded number of OLS locations and persist each result."
    )
    hashing_run.add_argument("--workers",type=int,default=2)
    hashing_run.add_argument("--chunk-size",type=int,default=1024*1024)
    hashing_run.add_argument("--max-files",type=int,default=1000,
                             help="Maximum files for this run; 0 drains the queue.")
    hashing_run.add_argument("--retry-failed",action="store_true")
    hashing_run.add_argument("--progress-every",type=int,default=25)
    hashing_run.add_argument("--report",type=Path)
    hashing_run.set_defaults(action="hashing-run")
    hash_worker = commands.add_parser(
        "hash-worker",help="Continuously hash the five-source persistent queue."
    )
    hash_worker.add_argument(
        "--continuous",action="store_true",required=True,
        help="Keep processing batches until empty, paused, cancelled, or blocked.",
    )
    hash_worker.add_argument("--workers",type=int,default=2)
    hash_worker.add_argument("--batch-size",type=int,default=10)
    hash_worker.add_argument("--chunk-size",type=int,default=1024*1024)
    hash_worker.add_argument("--batch-delay",type=float,default=1.0)
    hash_worker.add_argument("--retry-limit",type=int,default=5)
    hash_worker.add_argument("--retry-delay",type=float,default=60.0)
    hash_worker.add_argument("--progress-every",type=int,default=25)
    hash_worker.add_argument("--report",type=Path)
    hash_worker.set_defaults(action="hash-worker")
    for action_name, label in (
        ("pause","Pause the active hashing run after active file reads finish."),
        ("resume","Resume a paused hashing process or clear a stale pause."),
        ("cancel","Stop the active hashing worker after active file reads finish."),
        ("stop","Safely stop the continuous worker after active file reads finish."),
        ("retry","Mark failed and changed files retryable."),
    ):
        command = hashing_commands.add_parser(action_name,help=label)
        command.set_defaults(action=f"hashing-{action_name}")
    hashing_recover = hashing_commands.add_parser(
        "recover",
        help="Requeue interrupted work; use only after confirming no hash process is active.",
    )
    hashing_recover.set_defaults(action="hashing-recover")
    hashing_duplicates = hashing_commands.add_parser(
        "duplicates",help="List full-SHA-256 exact duplicate groups."
    )
    hashing_duplicates.add_argument("--limit",type=int,default=100)
    hashing_duplicates.add_argument("--output",type=Path)
    hashing_duplicates.set_defaults(action="hashing-duplicates")
    hashing_report = hashing_commands.add_parser(
        "report",help="Export the current queue, hashes, duplicates and throughput."
    )
    hashing_report.add_argument("--limit",type=int,default=100)
    hashing_report.add_argument("--output",type=Path)
    hashing_report.set_defaults(action="hashing-report")
    source_report = source_commands.add_parser(
        "report", help="Show persisted scan history for one registered source."
    )
    source_report.add_argument("source_id", type=int)
    source_report.add_argument("--output", type=Path)
    source_report.set_defaults(action="source-report")

    queue_run = commands.add_parser(
        "index-queue-run", help="Process a bounded serial batch from a source queue."
    )
    queue_run.add_argument("source_id", type=int)
    queue_run.add_argument("--batch-size", type=int, default=1)
    queue_run.add_argument("--workers", type=int, default=1)
    queue_run.add_argument("--max-file-bytes", type=int, default=256 * 1024 * 1024)
    queue_run.add_argument("--py-ols-path", type=Path)
    queue_run.set_defaults(action="index-queue-run")

    search = commands.add_parser(
        "search-projects",
        help="Search indexed projects by ECU metadata, source, versions and maps.",
    )
    for argument in (
        "ecu_type", "ecu_manufacturer", "ecu_family", "sw_number",
        "hw_number", "hw_code", "calibration_number", "calibration_id",
        "engine", "make", "model", "year", "fuel_type", "displacement",
        "power", "gearbox", "memory_type",
    ):
        search.add_argument(f"--{argument.replace('_', '-')}")
    search.add_argument("--text", default="")
    search.add_argument("--map-name", default="")
    search.add_argument("--columns", type=int)
    search.add_argument("--rows", type=int)
    search.add_argument("--version-name", default="")
    search.add_argument("--binary-hash", default="")
    search.add_argument("--file-hash", default="")
    search.add_argument("--source-path", default="")
    search.add_argument("--file-size", type=int)
    search.add_argument("--min-file-size", type=int)
    search.add_argument("--max-file-size", type=int)
    search.add_argument("--map-text", default="")
    search.add_argument("--format-version", type=int)
    search.add_argument("--validation-status", default="")
    search.add_argument("--parser-status", default="")
    search.add_argument("--limit", type=int, default=100)
    search.add_argument("--offset", type=int, default=0)
    search.set_defaults(action="search-projects")

    coverage = commands.add_parser(
        "parser-coverage", help="Report parser coverage by format and issue."
    )
    coverage.add_argument("--limit", type=int, default=1000)
    coverage.set_defaults(action="parser-coverage")

    training_candidate = commands.add_parser(
        "training-candidate",
        help="Register a provenance-tracked candidate; this does not train a model.",
    )
    training_candidate.add_argument("project_id", type=int)
    training_candidate.add_argument("provenance")
    training_candidate.add_argument(
        "--version-id", type=int, help="Must be a version record belonging to the project."
    )
    training_candidate.add_argument(
        "--validation-status",
        choices=("NOT_VALIDATED", "INCONCLUSIVE", "INDEPENDENTLY_VALIDATED"),
        default="NOT_VALIDATED",
    )
    training_candidate.add_argument("--notes", default="")
    training_candidate.set_defaults(action="training-candidate")
    return parser


def _interactive_ecu_export(args: argparse.Namespace) -> int:
    original = args.original.resolve(strict=True)
    if not original.is_file() or original.suffix.lower() != ".bin":
        raise ValueError("Select an existing .bin file as the original.")
    output_directory = args.output_dir.resolve(strict=True)
    if not output_directory.is_dir():
        raise ValueError("The chosen output directory must already exist.")
    index = IndexDatabase(args.database, read_only=True)
    storage = args.database.resolve().parent / "ecu-workbench"
    try:
        identifiers = {
            field: getattr(args, field)
            for field in ("ecu_type", "ecu_manufacturer", "hw_number", "hw_code", "sw_number", "calibration_id")
            if getattr(args, field)
        }
        with original.open("rb") as stream:
            analysis = import_binary_stream(
                stream, filename=original.name, expected_size=original.stat().st_size,
                storage_root=storage, index_database=index, identifications=identifiers,
            )
        if _hash_file(original) != (
            analysis["source"]["sha256"], analysis["source"]["size_bytes"]
        ):
            raise ValueError("Het originele bestand is tijdens het importeren gewijzigd; export geannuleerd.")
        print(json.dumps({
            "status": "ORIGINAL_IMPORTED",
            "input_path": str(original),
            "original": analysis["source"],
            "identifications": analysis["identifications"],
            "limitations": analysis["report"]["limitations"],
        }, ensure_ascii=False, indent=2))
        include_others = False
        offset = 0
        while True:
            report = export_candidates(
                storage, analysis["analysis_id"], index,
                include_other_projects=include_others, offset=offset,
            )
            candidates = report["candidates"]
            if not candidates:
                print("Geen geschikte geregistreerde BIN-kandidaat vastgesteld.")
            else:
                print(
                    "Nr | Bestand | ECU/hardware/software | Type | Bytes/grootteverschil | SHA-256 | "
                    "Overeenkomsten | Verschillen | Ontbrekend | Waarschuwingen | "
                    "Validatie | Opzoekscore | Bytegelijkenis"
                )
                for number, item in enumerate(candidates, 1):
                    print(
                        f"{number} | {item['filename']} | "
                        f"{json.dumps(item['identifications'], ensure_ascii=False)} | "
                        f"{item['original_or_tuned']}/{item['tuning_type']} | "
                        f"{item['size_bytes']}/{item['size_comparison']} | {item['sha256']} | "
                        f"{item['matched_features']} | {item['differing_features']} | "
                        f"{item['missing_features']} | "
                        f"{item['parser_warnings']} / {item['parser_errors']} | {item['compatibility']} / "
                        f"{item['reference_validation']} | {item['retrieval_score']} "
                        "(opzoekpunten, geen kans) | NOT_COMPUTED"
                        + ("" if item["available"] else " | BESTAND ONTBREEKT")
                    )
            if report["truncated"]:
                print("Meer resultaten beschikbaar: N voor de volgende pagina.")
            if offset:
                print("V voor de vorige pagina.")
            choice = input("Kies nummer, A voor andere kandidaten, N/V voor pagina, of Q om te stoppen: ").strip()
            if choice.lower() == "q":
                print("Geen bestand geëxporteerd.")
                return 0
            if choice.lower() == "a":
                include_others = True
                offset = 0
                continue
            if choice.lower() == "n" and report["truncated"]:
                offset += 500
                continue
            if choice.lower() == "v" and offset:
                offset = max(0, offset - 500)
                continue
            try:
                number = int(choice)
            except ValueError as exc:
                raise ValueError("Kies een weergegeven kandidaatnummer, A of Q.") from exc
            if not 1 <= number <= len(candidates):
                raise ValueError("Het gekozen kandidaatnummer staat niet in de lijst.")
            selected = candidates[number - 1]
            preview = export_preview(storage, analysis["analysis_id"], selected["artifact_id"], index)
            output_name = args.output_name or Path(selected["filename"]).stem + ".bin"
            while (output_directory / output_name).exists() or (
                output_directory / (output_name + ".manifest.json")
            ).exists():
                output_name = input(
                    "BIN of manifest bestaat al. Kies een andere bestandsnaam of Q om te stoppen: "
                ).strip()
                if output_name.lower() == "q":
                    print("Geen bestand geëxporteerd.")
                    return 0
            print("Controleer de exacte kandidaat en uitvoerlocatie:")
            print(json.dumps({
                **preview, "output_directory": str(output_directory),
                "output_name": output_name, "user_label": args.label or "UNKNOWN",
            }, ensure_ascii=False, indent=2))
            if input("Typ EXPORT om deze specifieke kopie te bevestigen: ").strip() != "EXPORT":
                print("Geen bestand geëxporteerd.")
                return 0
            result = export_selected_binary(
                storage, analysis["analysis_id"], selected["artifact_id"], index,
                output_directory=output_directory, output_name=output_name,
                confirmation_token=preview["confirmation_token"], confirmed=True,
                user_label=args.label,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    finally:
        index.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "ecu-bin-export":
        return _interactive_ecu_export(args)
    if args.action == "serve":
        if not 1 <= args.port <= 65535:
            raise ValueError("--port must be between 1 and 65535.")
        serve(args.database, args.port)
        return 0
    if args.action == "compare":
        validate_byte_threshold(args.byte_threshold)
        result = compare_binary_files(
            args.original,
            args.tuned,
            chunk_size=args.chunk_bytes,
            max_ranges=args.max_ranges,
        )
        if not args.no_store:
            database = IndexDatabase(args.database)
            try:
                store_comparison(args.database, result)
            finally:
                database.close()
        output = json.dumps({
            "original": result.original_path,
            "tuned": result.tuned_path,
            "original_sha256": result.original_sha256,
            "tuned_sha256": result.tuned_sha256,
            "original_size": result.original_size,
            "tuned_size": result.tuned_size,
            "differing_bytes": result.differing_bytes,
            "exact_match": result.exact_match,
            "byte_similarity": byte_similarity(result, args.byte_threshold),
            "changed_ranges": [
                {"start": item.start, "end_exclusive": item.end_exclusive}
                for item in result.changed_ranges
            ],
        }, indent=2)
        if args.output:
            _write_report(
                args.output,
                output,
                protected_paths=[
                    args.original,
                    args.tuned,
                    args.database,
                    args.database.with_name(args.database.name + "-wal"),
                    args.database.with_name(args.database.name + "-shm"),
                    args.database.with_name(args.database.name + "-journal"),
                ],
            )
            print(f"Binary comparison written to {args.output.resolve()}")
        else:
            print(output)
        return 0

    if args.action == "reference-package-audit":
        database = IndexDatabase(args.database, read_only=True)
        try:
            report = audit_reference_package(database, args.manifest)
            output = json.dumps(report, indent=2, ensure_ascii=False)
            if args.output:
                protected_paths = [
                    args.database,
                    args.database.with_name(args.database.name + "-wal"),
                    args.database.with_name(args.database.name + "-shm"),
                    args.database.with_name(args.database.name + "-journal"),
                    args.manifest,
                    args.manifest.resolve().parent / "CHECKLIST.txt",
                ]
                project_id = report.get("project_id")
                if isinstance(project_id, int):
                    diagnostic = database.get_diagnostic(project_id)
                    if diagnostic is not None:
                        protected_paths.append(Path(diagnostic["source"]["path"]))
                for artifact in report["package_inspection"]["artifacts"]:
                    raw_path = artifact.get("path")
                    if not isinstance(raw_path, str) or not raw_path:
                        continue
                    artifact_path = Path(raw_path)
                    if not artifact_path.is_absolute():
                        artifact_path = args.manifest.resolve().parent / artifact_path
                    protected_paths.append(artifact_path)
                _write_report(
                    args.output,
                    output,
                    protected_paths=protected_paths,
                )
                print(f"Offline reference audit written to {args.output.resolve()}")
            else:
                print(output)
        finally:
            database.close()
        return 0

    database = IndexDatabase(args.database)
    try:
        if args.action == "init":
            print(f"Index ready: {args.database.resolve()}")
        elif args.action == "status":
            print(json.dumps(database.summary(), indent=2))
        elif args.action == "hashing-plan":
            plan = build_hashing_plan(database)
            print(json.dumps(plan,indent=2,ensure_ascii=False))
            return 0 if plan["all_sources_available"] else 2
        elif args.action == "hashing-status":
            print(json.dumps(build_hashing_report(database),indent=2,ensure_ascii=False))
        elif args.action == "hashing-run":
            report = run_hashing(
                database,
                workers=args.workers,
                chunk_size=args.chunk_size,
                max_files=args.max_files,
                retry_failed=args.retry_failed,
                progress_every=args.progress_every,
            )
            output = json.dumps(report,indent=2,ensure_ascii=False,sort_keys=True)
            if args.report:
                args.report.parent.mkdir(parents=True,exist_ok=True)
                args.report.write_text(output+"\n",encoding="utf-8")
                print(f"Hash run report: {args.report.resolve()}")
            print(output)
            return 130 if report["status"] in {"INTERRUPTED","CANCELLED"} else 0
        elif args.action == "hash-worker":
            report = run_hash_worker(
                database,workers=args.workers,batch_size=args.batch_size,
                chunk_size=args.chunk_size,
                batch_delay_seconds=args.batch_delay,
                retry_limit=args.retry_limit,
                retry_delay_seconds=args.retry_delay,
                progress_every=args.progress_every,
            )
            output = json.dumps(report,indent=2,ensure_ascii=False,sort_keys=True)
            if args.report:
                args.report.parent.mkdir(parents=True,exist_ok=True)
                args.report.write_text(output+"\n",encoding="utf-8")
                print(f"Hash worker report: {args.report.resolve()}")
            print(output)
            return 0 if report["status"] in {"complete","stopped"} else 2
        elif args.action == "hashing-pause":
            print(json.dumps(database.set_hash_control("paused"),indent=2))
        elif args.action == "hashing-resume":
            print(json.dumps(database.set_hash_control("running"),indent=2))
        elif args.action == "hashing-cancel":
            print(json.dumps(database.set_hash_control("cancelled"),indent=2))
        elif args.action == "hashing-stop":
            print(json.dumps(database.set_hash_control("cancelled"),indent=2))
        elif args.action == "hashing-retry":
            source_ids = [
                int(slot["source_id"]) for slot in database.list_source_slots()
                if slot["source_id"] is not None
            ]
            database.sync_hash_jobs(source_ids)
            retried = database.retry_failed_hash_jobs(source_ids)
            print(json.dumps({"retry_pending":retried},indent=2))
        elif args.action == "hashing-recover":
            print(json.dumps(database.recover_hash_run(),indent=2))
        elif args.action == "hashing-duplicates":
            groups = database.list_duplicate_content_locations(limit=args.limit)
            result = {
                "groups":groups,
                "group_count":len(groups),
                "basis":"Full SHA-256 and equal file size; no filename inference.",
            }
            output = json.dumps(result,indent=2,ensure_ascii=False)
            if args.output:
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(output+"\n",encoding="utf-8")
                print(f"Duplicate report: {args.output.resolve()}")
            print(output)
        elif args.action == "hashing-report":
            report = build_hashing_report(database,limit=args.limit)
            output = json.dumps(report,indent=2,ensure_ascii=False,sort_keys=True)
            if args.output:
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(output+"\n",encoding="utf-8")
                print(f"Hashing report: {args.output.resolve()}")
            print(output)
        elif args.action == "source-add":
            print(json.dumps(
                register_source(database, args.path), indent=2, ensure_ascii=False
            ))
        elif args.action == "source-list":
            print(json.dumps(database.list_sources(), indent=2, ensure_ascii=False))
        elif args.action == "source-slots":
            print(json.dumps({
                "roots": database.list_source_slots(),
                "deduplication": database.source_dedup_summary(),
            }, indent=2, ensure_ascii=False))
        elif args.action == "source-configure":
            source = register_source(database, args.path)
            slot_id = next(
                item["slot_id"] for item in database.list_source_slots()
                if item["name"] == args.slot
            )
            database.configure_source_slot(slot_id, source["id"])
            print(json.dumps({
                "slot": args.slot,
                "source": database.get_source(source["id"]),
            }, indent=2, ensure_ascii=False))
            return 0 if source["status"] == "available" else 2
        elif args.action == "source-test":
            print(json.dumps(
                test_source(database, args.source_id), indent=2, ensure_ascii=False
            ))
        elif args.action == "source-move":
            print(json.dumps(
                move_source(database, args.source_id, args.path),
                indent=2,
                ensure_ascii=False,
            ))
        elif args.action == "source-relocate-roots":
            roots = json.loads(args.root_config.read_text(encoding="utf-8"))
            slots = database.list_source_slots()
            if len(slots) != 5 or any(slot["source_id"] is None for slot in slots):
                raise RuntimeError("All five source slots must be configured before relocation.")
            if set(roots) != {str(slot["source_id"]) for slot in slots}:
                raise ValueError("Root configuration must contain exactly the five source IDs.")
            proposed = {}
            for slot in slots:
                new_path = Path(roots[str(slot["source_id"])]).resolve()
                if not new_path.is_dir():
                    raise OSError(f"Verified local source directory is missing: {new_path}")
                next(new_path.iterdir(),None)
                proposed[int(slot["source_id"])] = {
                    "old_path":slot["path"],"new_path":str(new_path)
                }
            results = database.relocate_source_roots(proposed)
            relocated = []
            for slot in slots:
                source_id = int(slot["source_id"])
                relocated.append({
                    "slot":slot["name"],"source_id":slot["source_id"],
                    "old_path":slot["path"],
                    "new_path":proposed[source_id]["new_path"],
                    "updated_paths":results[source_id],
                })
            print(json.dumps({"relocated_sources":relocated},indent=2,ensure_ascii=False))
        elif args.action == "source-scan":
            report = scan_registered_source(
                database,
                args.source_id,
                progress_every=args.progress_every,
                force_recheck=args.recheck,
                benchmark_bytes=args.benchmark_bytes,
            )
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 130 if report["status"] == "interrupted" else 0
        elif args.action == "source-inventory":
            report = run_multi_source_inventory(
                database,
                progress_every=args.progress_every,
                max_hash_files=args.max_hash_files,
                max_hash_bytes=args.max_hash_bytes,
            )
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report,indent=2,ensure_ascii=False,sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"Multi-source inventory report: {args.report.resolve()}")
            print(json.dumps({
                "status":report["status"],
                "totals":report["totals"],
                "hash_benchmark":{
                    "status":report["hash_benchmark"]["status"],
                    "files_hashed":report["hash_benchmark"]["files_hashed"],
                    "bytes_read":report["hash_benchmark"]["bytes_read"],
                    "average_mib_per_second":
                        report["hash_benchmark"].get("average_mib_per_second"),
                },
            },indent=2,ensure_ascii=False))
            return 0 if report["all_configured_sources_complete"] else 2
        elif args.action == "source-report":
            source = database.get_source(args.source_id)
            if source is None:
                raise ValueError(f"Unknown registered source ID {args.source_id}.")
            report = {
                "source": source,
                "scan_history": database.list_source_scan_runs(args.source_id),
            }
            output = json.dumps(report, indent=2, ensure_ascii=False)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output + "\n", encoding="utf-8")
                print(f"Source report written to {args.output.resolve()}")
            else:
                print(output)
        elif args.action == "index-queue-run":
            if args.workers != 1:
                raise ValueError(
                    "Only --workers 1 is supported: py-ols parses whole files and "
                    "parallel memory use has not been validated."
                )
            reader = PyOlsReader(args.py_ols_path)
            report = run_source_queue(
                database,
                args.source_id,
                reader=reader,
                max_file_bytes=args.max_file_bytes,
                batch_size=args.batch_size,
            )
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 1 if report["counts"].get("failed") or report["counts"].get("blocked") else 0
        elif args.action == "search-projects":
            metadata = {
                name: getattr(args, name)
                for name in (
                    "ecu_type", "ecu_manufacturer", "ecu_family", "sw_number",
                    "hw_number", "hw_code", "calibration_number",
                    "calibration_id", "engine", "make", "model", "year",
                    "fuel_type", "displacement", "power", "gearbox",
                    "memory_type",
                )
                if getattr(args, name)
            }
            print(json.dumps(
                search_projects(
                    database,
                    text=args.text,
                    metadata=metadata,
                    map_name=args.map_name,
                    columns=args.columns,
                    rows=args.rows,
                    version_name=args.version_name,
                    binary_hash=args.binary_hash,
                    file_hash=args.file_hash,
                    format_version=args.format_version,
                    validation_status=args.validation_status,
                    parser_status=args.parser_status,
                    source_path=args.source_path,
                    file_size=args.file_size,
                    min_file_size=args.min_file_size,
                    max_file_size=args.max_file_size,
                    map_text=args.map_text,
                    limit=args.limit,
                    offset=args.offset,
                ),
                indent=2,
                ensure_ascii=False,
            ))
        elif args.action == "parser-coverage":
            print(json.dumps(
                parser_coverage(database, limit=args.limit),
                indent=2,
                ensure_ascii=False,
            ))
        elif args.action == "training-candidate":
            candidate_id = database.record_training_candidate(
                project_id=args.project_id,
                version_id=args.version_id,
                provenance=args.provenance,
                validation_status=args.validation_status,
                notes=args.notes,
            )
            print(json.dumps({
                "candidate_id": candidate_id,
                "status": args.validation_status,
                "training_started": False,
            }, indent=2))
        elif args.action == "inventory":
            report = scan_inventory(
                roots=args.roots,
                database=database,
                hash_duplicate_candidates=args.hash_duplicate_candidates,
                verify_indexed_hashes=args.verify_indexed_hashes,
                file_list_path=args.file_list,
                report_path=args.report,
            )
            output = json.dumps(report, indent=2, ensure_ascii=False)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(output + "\n", encoding="utf-8")
                report["report_path"] = str(args.report.resolve())
            print(json.dumps(report, indent=2, ensure_ascii=False))
        elif args.action == "export-queue":
            if args.list_only:
                result = {
                    "tasks": database.list_export_tasks(
                        status=args.status,
                        limit=args.limit,
                    )
                }
            else:
                result = plan_export_tasks(database)
                if args.status is not None:
                    result["queue"] = database.list_export_tasks(
                        status=args.status,
                        limit=args.limit,
                    )
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.action == "export-batch":
            print(json.dumps(
                write_manual_export_batch(
                    database,
                    output_directory=args.output_dir,
                    status=args.status,
                ),
                indent=2,
                ensure_ascii=False,
            ))
        elif args.action == "export-task-update":
            database.update_export_task(
                args.task_id,
                status=args.status,
                result=args.result,
            )
            print(json.dumps(
                {
                    "task_id": args.task_id,
                    "status": args.status,
                    "recorded": True,
                },
                indent=2,
            ))
        elif args.action == "export-task-history":
            print(json.dumps(
                database.list_export_task_history(
                    args.task_id,
                    limit=args.limit,
                ),
                indent=2,
                ensure_ascii=False,
            ))
        elif args.action == "find-projects":
            print(json.dumps(
                find_project_matches(
                    database,
                    args.project_id,
                    limit=args.limit,
                ),
                indent=2,
                ensure_ascii=False,
            ))
        elif args.action == "compare-projects":
            print(json.dumps(
                compare_projects(
                    database,
                    args.left_project_id,
                    args.right_project_id,
                ),
                indent=2,
                ensure_ascii=False,
            ))
        elif args.action == "projects":
            print(json.dumps(
                database.list_projects(query=args.query, limit=args.limit),
                indent=2,
            ))
        elif args.action == "show-project":
            project = database.get_project(args.project_id)
            if project is None:
                print(f"No project found with id {args.project_id}.")
                return 1
            print(json.dumps(project, indent=2))
        elif args.action == "diagnose":
            report = database.get_diagnostic(args.project_id)
            if report is None:
                print(f"No diagnostic report found for project {args.project_id}.")
                return 1
            print(json.dumps(report, indent=2, ensure_ascii=False))
        elif args.action == "reference-template":
            paths = write_templates(
                database,
                args.output_dir,
                project_id=args.project_id,
            )
            print(json.dumps({
                "generated": [str(path.resolve()) for path in paths],
                "count": len(paths),
            }, indent=2, ensure_ascii=False))
        elif args.action == "reference-import":
            result = import_manifest(database, args.manifest)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.action == "reference-validate":
            if args.project_id is None:
                project_ids = [
                    row["id"]
                    for row in database.connection.execute(
                        "SELECT id FROM projects ORDER BY id"
                    )
                ]
            else:
                project_ids = [args.project_id]
            reports = [validate_project(database, item) for item in project_ids]
            print(json.dumps({
                "validated_projects": len(reports),
                "reports": reports,
            }, indent=2, ensure_ascii=False))
        elif args.action == "reference-report":
            report = build_project_report(database, args.project_id)
            output = json.dumps(report, indent=2, ensure_ascii=False)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output + "\n", encoding="utf-8")
                print(f"Reference report written to {args.output.resolve()}")
            else:
                print(output)
        elif args.action == "reference-summary":
            report = build_reference_summary(database)
            output = json.dumps(report, indent=2, ensure_ascii=False)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output + "\n", encoding="utf-8")
                print(f"Reference summary written to {args.output.resolve()}")
            else:
                print(output)
        elif args.action == "reference-analyze":
            if args.chunk_bytes <= 0 or args.max_ranges <= 0:
                raise ValueError("--chunk-bytes and --max-ranges must be positive.")
            if args.project_id is None:
                project_ids = [
                    row["project_id"]
                    for row in database.connection.execute(
                        """
                        SELECT DISTINCT project_id
                        FROM reference_artifacts
                        WHERE kind='binary_version'
                        ORDER BY project_id
                        """
                    )
                ]
            else:
                project_ids = [args.project_id]
            outcomes = []
            for project_id in project_ids:
                report = analyze_project(
                    database,
                    project_id,
                    chunk_size=args.chunk_bytes,
                    max_ranges=args.max_ranges,
                )
                run_id = store_analysis_report(database, report)
                report["analysis_run_id"] = run_id
                json_path = (
                    args.output_dir / f"project-{project_id}-run-{run_id}.json"
                )
                text_path = (
                    args.output_dir / f"project-{project_id}-run-{run_id}.txt"
                )
                write_analysis_outputs(
                    report,
                    json_path=json_path,
                    text_path=text_path,
                )
                outcomes.append(
                    {
                        "project_id": project_id,
                        "analysis_run_id": run_id,
                        "overall_status": report["overall_status"],
                        "counts": report["counts"],
                        "json_report": str(json_path.resolve()),
                        "text_report": str(text_path.resolve()),
                    }
                )
            print(json.dumps({"analyses": outcomes}, indent=2, ensure_ascii=False))
        elif args.action == "match-candidates":
            print(json.dumps(
                database.list_binary_slot_candidates(
                    args.project_id,
                    limit=args.limit,
                ),
                indent=2,
                ensure_ascii=False,
            ))
        elif args.action == "match-review":
            review_id = database.review_version_match(
                args.candidate_id,
                args.decision,
                args.evidence,
                reviewer=args.reviewer,
            )
            print(json.dumps(
                {
                    "review_id": review_id,
                    "candidate_id": args.candidate_id,
                    "decision": args.decision,
                    "reviewer": args.reviewer,
                },
                indent=2,
                ensure_ascii=False,
            ))
        elif args.action == "reference-package-template":
            output_dir = args.output_dir or (
                Path(__file__).resolve().parents[1]
                / "data"
                / "reference-packets"
                / str(args.project_id)
            )
            result = write_package_template(
                database,
                args.project_id,
                output_dir,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.action == "reference-package-inspect":
            report = inspect_package(database, args.manifest)
            diagnostic = database.get_diagnostic(report["project_id"])
            if diagnostic is not None:
                report["run_id"] = database.store_reference_package_run(
                    project_id=report["project_id"],
                    source_sha256=diagnostic["source"]["sha256"],
                    manifest_path=str(args.manifest.resolve()),
                    manifest_sha256=report["manifest_sha256"],
                    run_type="inspection",
                    status=report["status"],
                    report=report,
                )
            output = json.dumps(report, indent=2, ensure_ascii=False)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output + "\n", encoding="utf-8")
                print(f"Package inspection written to {args.output.resolve()}")
            else:
                print(output)
        elif args.action == "reference-package-import":
            result = import_reference_package(database, args.manifest)
            report = result["inspection"]
            diagnostic = database.get_diagnostic(report["project_id"])
            result["run_id"] = database.store_reference_package_run(
                project_id=report["project_id"],
                source_sha256=diagnostic["source"]["sha256"],
                manifest_path=str(args.manifest.resolve()),
                manifest_sha256=report["manifest_sha256"],
                run_type="import",
                status=report["status"],
                report=result,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.action == "reference-version-match":
            if args.chunk_bytes <= 0 or args.max_ranges <= 0:
                raise ValueError("--chunk-bytes and --max-ranges must be positive.")
            report = match_package_versions(
                database,
                args.manifest,
                chunk_size=args.chunk_bytes,
                max_ranges=args.max_ranges,
            )
            diagnostic = database.get_diagnostic(report["project_id"])
            report["run_id"] = database.store_reference_package_run(
                project_id=report["project_id"],
                source_sha256=diagnostic["source"]["sha256"],
                manifest_path=str(args.manifest.resolve()),
                manifest_sha256=report["manifest_sha256"],
                run_type="version_match",
                status=report["status"],
                report=report,
            )
            output = json.dumps(report, indent=2, ensure_ascii=False)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output + "\n", encoding="utf-8")
                print(f"Version match report written to {args.output.resolve()}")
            else:
                print(output)
        elif args.action == "reference-map-compare":
            report = compare_map_metadata(database, args.manifest)
            diagnostic = database.get_diagnostic(report["project_id"])
            report["run_id"] = database.store_reference_package_run(
                project_id=report["project_id"],
                source_sha256=diagnostic["source"]["sha256"],
                manifest_path=str(args.manifest.resolve()),
                manifest_sha256=report["manifest_sha256"],
                run_type="map_metadata",
                status=report["status"],
                report=report,
            )
            output = json.dumps(report, indent=2, ensure_ascii=False)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output + "\n", encoding="utf-8")
                print(f"Map comparison written to {args.output.resolve()}")
            else:
                print(output)
        elif args.action == "reference-metadata-compare":
            report = compare_project_metadata(database, args.manifest)
            diagnostic = database.get_diagnostic(report["project_id"])
            report["run_id"] = database.store_reference_package_run(
                project_id=report["project_id"],
                source_sha256=diagnostic["source"]["sha256"],
                manifest_path=str(args.manifest.resolve()),
                manifest_sha256=report["manifest_sha256"],
                run_type="project_metadata",
                status=report["status"],
                report=report,
            )
            output = json.dumps(report, indent=2, ensure_ascii=False)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output + "\n", encoding="utf-8")
                print(f"Metadata comparison written to {args.output.resolve()}")
            else:
                print(output)
        elif args.action == "reference-package-report":
            report = build_package_report(
                database,
                args.project_id,
                manifest_path=args.manifest,
            )
            output = json.dumps(report, indent=2, ensure_ascii=False)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output + "\n", encoding="utf-8")
                print(f"Reference package report written to {args.output.resolve()}")
            else:
                print(output)
        elif args.action == "import":
            if args.max_file_bytes <= 0:
                raise ValueError("--max-file-bytes must be positive.")
            reader = PyOlsReader(args.py_ols_path)
            counts: Counter[str] = Counter()
            for outcome in import_sources(
                    sources=args.sources,
                    reader=reader,
                    database=database,
                    max_file_bytes=args.max_file_bytes,
                ):
                counts[outcome.status] += 1
            print(json.dumps({
                "processed": sum(counts.values()),
                "imported": counts["imported"],
                "partial": counts["partial"],
                "skipped_unchanged": counts["skipped_unchanged"],
                "blocked": counts["blocked"],
                "failed": counts["failed"],
                "index": database.summary(),
            }, indent=2))
            return 1 if counts["blocked"] or counts["failed"] else 0
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
