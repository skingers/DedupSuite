#!/usr/bin/env python3
"""Production runner: concurrent ingest, Ed25519 signing, and vault export.

Hashes source files with the 8-worker pipeline, persists rows and
``batch_signatures`` to the production database, verifies integrity, classifies
golden copies, and exports unique notes into the Obsidian vault.
"""

from __future__ import annotations

import argparse
import platform
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import List, Optional

import db_ingest
from dedup_suite import DatabaseManager
from integrity_check import IntegrityCheck
from pipeline import PRODUCER_WORKERS, run_pipeline
from production_config import (
    ProductionPaths,
    add_production_path_arguments,
    collect_ingest_paths,
    paths_from_namespace,
)


def _log(message: str) -> None:
    print(f"[PRODUCTION] {message}", flush=True)


def _progress(done: int, total: int, status: str) -> None:
    if total:
        _log(f"{status} ({done}/{total})")
    else:
        _log(status)


def _prepare_database(db_path: Path, device_id: str) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        db_ingest.configure_connection(conn)
        db_ingest.register_device(conn, device_id)
        conn.commit()
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_production",
        description=(
            "DedupSuite 2.0 production ingest: 8-way concurrent hashing, "
            "cryptographic batch signing, and Obsidian vault export."
        ),
    )
    add_production_path_arguments(parser)
    parser.add_argument(
        "--device-id",
        default=None,
        help="Device identifier for file_index FK (default: hostname).",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Audit session UUID (default: random).",
    )
    parser.add_argument(
        "--ignore-exts",
        default="",
        help="Comma-separated extensions to skip (e.g. .tmp,.bak).",
    )
    parser.add_argument(
        "--ignore-folders",
        default="",
        help="Comma-separated folder names to skip (case-insensitive).",
    )
    parser.add_argument(
        "--no-export",
        action="store_true",
        help="Skip golden classification and Obsidian vault export.",
    )
    parser.add_argument(
        "--no-integrity-check",
        action="store_true",
        help="Skip post-ingest Ed25519 batch verification.",
    )
    layout = parser.add_mutually_exclusive_group()
    layout.add_argument(
        "--tree-mode",
        "--hierarchical",
        dest="hierarchical",
        action="store_true",
        help="Export golden files under YYYY/YYYY-MM-DD/ with dated filenames (default).",
    )
    layout.add_argument(
        "--flat",
        dest="hierarchical",
        action="store_false",
        help="Write notes at the vault root without a dated folder tree.",
    )
    parser.set_defaults(hierarchical=True)
    parser.add_argument(
        "--export-mode",
        choices=db_ingest.EXPORT_MODES,
        default="standard",
        help=(
            "Vault export profile: 'standard' copies only physical assets (default); "
            "'plm' or 'obsidian' also writes Markdown sidecars for LLM/knowledge use."
        ),
    )
    return parser


def run_production(
    paths: ProductionPaths,
    *,
    device_id: Optional[str] = None,
    session_id: Optional[str] = None,
    ignore_exts: str = "",
    ignore_folders: str = "",
    export_vault: bool = True,
    integrity_check: bool = True,
    hierarchical: bool = True,
    export_mode: str = "standard",
) -> int:
    """Execute the production pipeline for resolved absolute paths."""
    device = device_id or platform.node() or "production-device"
    session = session_id or str(uuid.uuid4())

    if not paths.source.is_dir():
        _log(f"ERROR: source is not a directory: {paths.source}")
        return 2
    paths.destination.mkdir(parents=True, exist_ok=True)

    ignore_ext_list = [e.strip() for e in ignore_exts.split(",") if e.strip()]
    ignore_folder_list = [f.strip() for f in ignore_folders.split(",") if f.strip()]

    file_paths: List[Path] = collect_ingest_paths(
        paths.source,
        ignore_exts=ignore_ext_list,
        ignore_folders=ignore_folder_list,
    )
    if not file_paths:
        _log(f"No files found under source: {paths.source}")
        return 2

    _log(f"Source: {paths.source}")
    _log(f"Destination vault: {paths.destination}")
    _log(f"Database: {paths.database}")
    _log(f"Session: {session} | Device: {device}")
    _log(f"Files to ingest: {len(file_paths)} | Hash workers: {PRODUCER_WORKERS}")
    _log(f"Vault layout: {'hierarchical (YYYY/YYYY-MM-DD)' if hierarchical else 'flat'}")
    _log(f"Export mode: {export_mode}")

    _prepare_database(paths.database, device)

    duration, inserted, collected = run_pipeline(
        file_paths,
        paths.database,
        device_id=device,
        session_id=session,
        progress_callback=_progress,
    )
    success_rows = sum(1 for _, rec in collected if rec.get("status") == "success")
    read_errors = len(collected) - success_rows
    _log(
        f"Ingest complete in {duration:.2f}s — "
        f"inserted/updated: {inserted}, hashed OK: {success_rows}, read errors: {read_errors}"
    )

    if integrity_check:
        checker = IntegrityCheck(paths.database)
        valid, total, failed = checker.verify_all()
        _log(f"Integrity: {valid}/{total} batch signatures valid")
        if failed:
            _log(f"ERROR: failed batch indices: {failed}")
            return 1
        if total and not checker.scan():
            _log("ERROR: integrity scan failed")
            return 1

    if export_vault:
        db_manager = DatabaseManager(db_path=paths.database)
        try:
            stats = db_manager.identify_golden_versions(session_id=session)
            _log(f"Golden classification: {stats}")
            export_result = db_ingest.export_golden_vault(
                paths.database,
                session,
                paths.destination,
                hierarchical=hierarchical,
                export_mode=export_mode,
                log=_log,
            )
            _log(
                f"Vault export complete ({export_result.export_mode}): "
                f"{export_result.assets_copied} assets, "
                f"{export_result.notes_written} sidecars, "
                f"{export_result.proofs_persisted} OTS proofs in DB, "
                f"{export_result.anchored} anchored, {export_result.pending} pending"
            )
        finally:
            db_manager.close()

    _log("Production run finished successfully.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = paths_from_namespace(args)
    return run_production(
        config,
        device_id=args.device_id,
        session_id=args.session_id,
        ignore_exts=args.ignore_exts,
        ignore_folders=args.ignore_folders,
        export_vault=not args.no_export,
        integrity_check=not args.no_integrity_check,
        hierarchical=args.hierarchical,
        export_mode=args.export_mode,
    )


if __name__ == "__main__":
    sys.exit(main())
