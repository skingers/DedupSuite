from __future__ import annotations

import argparse
import datetime
from datetime import datetime
import re
import os
import sys
import sqlite3
import shutil
import hashlib
import time
import threading
import json
import csv
import queue
import tempfile
import traceback
import uuid
import platform
import subprocess
import webbrowser
import tkinter as tk
from tkinter import messagebox, filedialog
import concurrent.futures
import abc
from enum import Enum, auto
from dataclasses import dataclass, asdict
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union
from check_db_v2 import ensure_blockchain_schema
from core.notary import DedupNotary
from core.markdown_translator import MarkdownTranslator, UniqueFileRecord, _sanitise
from network.notary_bridge import CloudNotaryBridge
import ingest_kernel
from vector_engine import SovereignVectorEngine
from chat_engine import generate_rag_response
from pipeline import run_pipeline
from config_manager import AppConfig
from core.license_gate import (
    FreemiumLimitExceeded,
    assert_processing_allowed,
    record_processed_file,
    resolve_state_path,
)

config = AppConfig()

# --- Boot Assertion Constants ---
KNOWN_GOOD_HASH: str = "717f416bb33de1f1b30f3c5683b6677121eaa55f8f54ab7fb2277e5c1b0cc463"
CANONICAL_EXECUTABLE: Optional[str] = None


def verify_environment() -> None:
    """Validate environment, interpreter, and source script integrity at startup."""
    # 1. Environmental Forensics
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        script_dir = os.getcwd()

    log_path = os.path.join(script_dir, "sovraan.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    log_entry = (
        f"--- Environmental Forensics ({timestamp}) ---\n"
        f"sys.executable: {sys.executable}\n"
        f"sys.version: {sys.version}\n"
        f"sys.path: {sys.path}\n"
        f"os.getcwd(): {os.getcwd()}\n"
        f"------------------------------------------------------\n"
    )

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception as e:
        print(f"Warning: Failed to write to forensics log: {e}", file=sys.stderr)

    # 2. Script Identity (Integrity Check)
    try:
        current_file = os.path.abspath(__file__)
    except NameError:
        current_file = None

    if current_file and os.path.isfile(current_file):
        sha256 = hashlib.sha256()
        try:
            with open(current_file, "rb") as f:
                for line in f:
                    # Skip the line containing the KNOWN_GOOD_HASH declaration to avoid self-reference paradox.
                    if line.strip().startswith(b"KNOWN_GOOD_HASH"):
                        continue
                    sha256.update(line)
            computed_hash = sha256.hexdigest()
        except Exception as e:
            raise RuntimeError(f"Failed to read current script for integrity check: {e}")

        if KNOWN_GOOD_HASH and KNOWN_GOOD_HASH != "PLACEHOLDER":
            if computed_hash != KNOWN_GOOD_HASH:
                msg = (
                    f"Script integrity verification failed!\n"
                    f"A stale or 'shadow' version of the script is being executed, or the source file "
                    f"has been modified without updating the KNOWN_GOOD_HASH constant.\n"
                    f"Expected (KNOWN_GOOD_HASH): {KNOWN_GOOD_HASH}\n"
                    f"Actual (Computed): {computed_hash}\n"
                    f"If this is a conscious deployment, please update KNOWN_GOOD_HASH = '{computed_hash}' in sovraan_core.py."
                )
                raise RuntimeError(msg)
        elif KNOWN_GOOD_HASH == "PLACEHOLDER":
            print(f"[BOOT WARNING] KNOWN_GOOD_HASH is set to PLACEHOLDER. Calculated hash: {computed_hash}", file=sys.stderr)

    # 3. Interpreter Validation
    if CANONICAL_EXECUTABLE:
        norm_current = os.path.normcase(os.path.normpath(sys.executable))
        norm_canonical = os.path.normcase(os.path.normpath(CANONICAL_EXECUTABLE))
        if norm_current != norm_canonical:
            msg = (
                f"Interpreter validation failed!\n"
                f"The current Python executable does not match the canonical interpreter path.\n"
                f"Current executable: {sys.executable}\n"
                f"Expected canonical path: {CANONICAL_EXECUTABLE}"
            )
            raise RuntimeError(msg)


class StoragePort(abc.ABC):
    """Abstract port defining storage operations for vault elevation."""

    @abc.abstractmethod
    def exists(self, path: str) -> bool:
        """Check if path exists."""
        pass

    @abc.abstractmethod
    def put(self, src: str, dest: str) -> None:
        """Copy file from src to dest."""
        pass

    @abc.abstractmethod
    def makedirs(self, path: str) -> None:
        """Create directory and any missing parent directories."""
        pass

    @abc.abstractmethod
    def write_text(self, path: str, content: str) -> None:
        """Write content to file at path using UTF-8 encoding."""
        pass

    @abc.abstractmethod
    def get_basename(self, path: str) -> str:
        """Get the basename (filename) of a path."""
        pass

    @abc.abstractmethod
    def get_mtime(self, path: str) -> float:
        """Get the modification time of a file."""
        pass

    @abc.abstractmethod
    def get_dirname(self, path: str) -> str:
        """Get the parent directory path of a file."""
        pass

    @abc.abstractmethod
    def join_paths(self, *parts: str) -> str:
        """Join multiple path components."""
        pass

    @abc.abstractmethod
    def with_suffix(self, path: str, suffix: str) -> str:
        """Replace the suffix of a path."""
        pass

    @abc.abstractmethod
    def get_relative_posix_path(self, path: str, start: str) -> str:
        """Get relative path from start, formatted as posix."""
        pass

    @abc.abstractmethod
    def get_free_space(self, path: str) -> int:
        """Get the free space in bytes of the volume containing path."""
        pass

    @abc.abstractmethod
    def delete(self, path: str) -> None:
        """Delete file at path if it exists."""
        pass


class LocalDiskStorage(StoragePort):
    """Concrete adapter encapsulating local filesystem interactions."""

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def put(self, src: str, dest: str) -> None:
        shutil.copy2(src, dest)

    def makedirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def write_text(self, path: str, content: str) -> None:
        Path(path).write_text(content, encoding="utf-8")

    def get_basename(self, path: str) -> str:
        return os.path.basename(path)

    def get_mtime(self, path: str) -> float:
        return os.path.getmtime(path)

    def get_dirname(self, path: str) -> str:
        return os.path.dirname(path)

    def join_paths(self, *parts: str) -> str:
        return str(Path(os.path.join(*parts)).resolve())

    def with_suffix(self, path: str, suffix: str) -> str:
        return str(Path(path).with_suffix(suffix))

    def get_relative_posix_path(self, path: str, start: str) -> str:
        return Path(path).relative_to(start).as_posix()

    def get_free_space(self, path: str) -> int:
        return shutil.disk_usage(path).free

    def delete(self, path: str) -> None:
        print(f"[STORAGE] delete() called for path: {path}", flush=True)
        if os.path.exists(path):
            os.remove(path)


class ElevationStage(Enum):
    PENDING = auto()
    VALIDATED = auto()
    COPIED = auto()
    VERIFIED = auto()
    INDEXED = auto()
    COMPLETE = auto()
    FAILED = auto()


@dataclass
class ElevationRecord:
    src_path: str
    target_path: str
    stage: ElevationStage
    failure_stage: Optional[ElevationStage] = None
    failure_reason: Optional[str] = None


def rollback(record: ElevationRecord, storage: StoragePort) -> None:
    """Check the record's state/failure stage and clean up orphaned files if necessary."""
    print(f"[ROLLBACK] Invoked for record: {record.src_path} (Failed at stage: {record.failure_stage.name if record.failure_stage else 'UNKNOWN'})", flush=True)
    if record.target_path:
        sidecar_path = storage.with_suffix(record.target_path, ".md")
        
        # Delete sidecar file if exists
        try:
            if storage.exists(sidecar_path):
                print(f"[ROLLBACK] Removing orphaned sidecar: {sidecar_path}", flush=True)
                storage.delete(sidecar_path)
        except Exception as e:
            print(f"[ROLLBACK] Failed to remove sidecar: {e}", flush=True)

        # Delete main copied file if exists
        try:
            if storage.exists(record.target_path):
                print(f"[ROLLBACK] Removing orphaned target file: {record.target_path}", flush=True)
                storage.delete(record.target_path)
        except Exception as e:
            print(f"[ROLLBACK] Failed to remove target file: {e}", flush=True)

        # Post-Cleanup Check
        try:
            target_exists = storage.exists(record.target_path)
            sidecar_exists = storage.exists(sidecar_path)
            print(f"[ROLLBACK] Post-Cleanup Check: target_exists={target_exists}, sidecar_exists={sidecar_exists}", flush=True)
        except Exception:
            pass


def transition_elevation_file(
    record: ElevationRecord,
    storage: StoragePort,
    target_path: str,
    sidecar_content: str,
) -> None:
    """Attempts to transition a file through each elevation stage sequentially.

    If any stage fails, raises the exception which is handled by transitioning
    the record to FAILED, recording the failure stage, and invoking rollback.
    """
    try:
        # 1. PENDING -> VALIDATED
        record.stage = ElevationStage.VALIDATED
        if not storage.exists(record.src_path):
            raise FileNotFoundError(f"Source file not found: {record.src_path}")

        # 2. VALIDATED -> COPIED
        record.stage = ElevationStage.COPIED
        target_parent = storage.get_dirname(target_path)
        if not target_parent:
            raise ValueError(f"target_path has no parent directory: {target_path!r}")
        storage.makedirs(target_parent)
        storage.put(record.src_path, target_path)

        # 3. COPIED -> VERIFIED
        record.stage = ElevationStage.VERIFIED
        if not storage.exists(target_path):
            raise RuntimeError(
                f"storage.put completed without error but target file "
                f"does not exist: {target_path}"
            )

        # 4. VERIFIED -> INDEXED
        record.stage = ElevationStage.INDEXED
        sidecar_path = storage.with_suffix(target_path, ".md")
        storage.write_text(sidecar_path, sidecar_content)

        # 5. INDEXED -> COMPLETE
        record.stage = ElevationStage.COMPLETE

    except Exception as e:
        record.failure_stage = record.stage
        record.stage = ElevationStage.FAILED
        record.failure_reason = f"{type(e).__name__}: {str(e)}"
        rollback(record, storage)
        raise e


def serialize_records(records: List[ElevationRecord]) -> str:
    """Serialize ElevationRecord objects to JSON, handling Enum serialization."""
    serialized = []
    for r in records:
        serialized.append({
            "src_path": r.src_path,
            "target_path": r.target_path,
            "stage": r.stage.name,
            "failure_stage": r.failure_stage.name if r.failure_stage else None,
            "failure_reason": r.failure_reason
        })
    return json.dumps(serialized, indent=4)


try:
    import customtkinter as ctk
except ImportError:
    print("Missing dependency. Run: pip install customtkinter")
    sys.exit(1)

# --- External Dependencies ---
try:
    from PIL import Image, ImageDraw
    import cv2
    import imagehash
    import cairosvg
except (ImportError, OSError):
    print("Warning: Missing or incomplete optional dependencies (pillow, opencv, imagehash, cairosvg).")

try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

# ==========================================
#               HELPER CLASSES
# ==========================================

def get_drive_id(path: Union[str, Path]) -> str:
    """Return a stable device identifier for the volume containing ``path``."""
    try:
        p = Path(path).resolve()
        if platform.system() == 'Windows':
            drive = os.path.splitdrive(str(p))[0]
            if drive:
                # AUDIT-REVIEW: Use argument list instead of shell=True to prevent command injection.
                vol_args = ['vol', drive]
                creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                output = subprocess.check_output(
                    vol_args,
                    text=True,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
                for line in output.splitlines():
                    if 'serial number' in line.lower():
                        return line.split()[-1].strip()
        return str(os.stat(p.anchor).st_dev)
    except (OSError, subprocess.SubprocessError, ValueError):
        # AUDIT-REVIEW: Catch specific failures instead of bare except when resolving drive id.
        fallback_str = str(Path(path).resolve().anchor)
        return hashlib.sha256(fallback_str.encode()).hexdigest()[:16]

class DatabaseManager:
    """SQLite ledger for indexed files, golden/legacy status, and session metadata."""

    def __init__(
        self,
        db_name: str = "data_mine.db",
        *,
        db_path: Optional[Union[str, Path]] = None,
    ) -> None:
        if db_path is not None:
            resolved = Path(db_path).expanduser().resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(resolved)
        elif getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
            self.db_path = os.path.join(base_path, db_name)
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))
            self.db_path = os.path.join(base_path, db_name)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON;")
        # busy_timeout first so any residual contention waits politely instead
        # of raising "database is locked".
        self.conn.execute("PRAGMA busy_timeout = 30000;")
        # WAL lets the GUI thread keep reading (stats, etc.) while a worker
        # thread on its own connection writes — eliminating the lock contention
        # that froze the UI during Rationalise. Switching journal mode needs an
        # uncontended lock; degrade gracefully if another connection holds it.
        try:
            self.conn.execute("PRAGMA journal_mode = WAL;")
        except sqlite3.OperationalError:
            pass
        self._create_tables()
        ensure_blockchain_schema(self.db_path)

    def _create_tables(self) -> None:
        with self.conn:
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    device_name TEXT,
                    last_seen DATETIME
                )
            ''')
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS file_index (
                    sha256_hash TEXT,
                    phash TEXT,
                    file_name TEXT,
                    file_size INTEGER,
                    modified_time REAL,
                    full_path TEXT,
                    is_golden_version INTEGER DEFAULT 0,
                    device_id TEXT,
                    FOREIGN KEY(device_id) REFERENCES devices(device_id)
                )
            ''')
            try:
                self.conn.execute("ALTER TABLE file_index ADD COLUMN last_session_id TEXT")
            except sqlite3.OperationalError:
                pass
            for col, ddl in (
                ("is_golden", "INTEGER DEFAULT 0"),
                ("ots_proof", "BLOB"),
            ):
                cur = self.conn.execute("PRAGMA table_info(file_index)")
                columns = {info[1] for info in cur.fetchall()}
                if col not in columns:
                    self.conn.execute(f"ALTER TABLE file_index ADD COLUMN {col} {ddl}")

    def register_device(self, device_id: str, device_name: Optional[str] = None) -> None:
        """Ensure a row exists in ``devices`` for ``device_id``.

        ``file_index.device_id`` carries an enforced foreign key to
        ``devices(device_id)``; indexing a file therefore requires its device
        to be registered first. Uses ``INSERT OR IGNORE`` so repeated calls are
        idempotent.

        Args:
            device_id: Stable identifier for the volume being scanned.
            device_name: Human-readable label (defaults to the identifier).
        """
        if not device_id:
            return
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO devices (device_id, device_name, last_seen) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (device_id, device_name or device_id),
            )

    def index_file(self, data: Dict[str, Any]) -> None:
        """Insert or replace a file row using bound named parameters (SQL-injection safe)."""
        with self.conn:
            self.conn.execute('''
                INSERT OR REPLACE INTO file_index 
                (sha256_hash, phash, file_name, file_size, modified_time, full_path, device_id, last_session_id)
                VALUES (:sha256_hash, :phash, :file_name, :file_size, :modified_time, :full_path, :device_id, :last_session_id)
            ''', data)

    def mark_duplicates(self, duplicates: List[Union[str, Path]], session_id: Optional[str] = None) -> None:
        with self.conn:
            for dupe in duplicates:
                if session_id:
                    self.conn.execute(
                        "UPDATE file_index SET is_golden = 0, last_session_id = ? WHERE full_path = ?", 
                        (session_id, str(dupe))
                    )
                else:
                    self.conn.execute(
                        "UPDATE file_index SET is_golden = 0 WHERE full_path = ?", 
                        (str(dupe),)
                    )

    def identify_golden_versions(self, session_id: Optional[str] = None) -> Dict[str, int]:
        with self.conn:
            cur = self.conn.cursor()
            cur.execute("PRAGMA table_info(file_index)")
            columns = [info[1] for info in cur.fetchall()]
            if 'is_golden' not in columns:
                self.conn.execute("ALTER TABLE file_index ADD COLUMN is_golden INTEGER DEFAULT 0")

            if session_id:
                # Assume files in the current session are golden initially
                self.conn.execute("UPDATE file_index SET is_golden = 1 WHERE last_session_id = ?", (session_id,))
                
                # Mark as 0 ONLY if the hash already exists in older database records
                # OR if it's a duplicate within the same session (not the MIN rowid)
                self.conn.execute('''
                    UPDATE file_index 
                    SET is_golden = 0 
                    WHERE last_session_id = ? 
                      AND sha256_hash IS NOT NULL 
                      AND EXISTS (
                          SELECT 1 FROM file_index f2 
                          WHERE f2.sha256_hash = file_index.sha256_hash 
                            AND f2.rowid < file_index.rowid
                      )
                ''', (session_id,))
            else:
                self.conn.execute("UPDATE file_index SET is_golden = 1")
                self.conn.execute('''
                    UPDATE file_index 
                    SET is_golden = 0 
                    WHERE sha256_hash IS NOT NULL 
                      AND EXISTS (
                          SELECT 1 FROM file_index f2 
                          WHERE f2.sha256_hash = file_index.sha256_hash 
                            AND f2.rowid < file_index.rowid
                      )
                ''')
            
            if session_id:
                cur.execute("SELECT COUNT(*) FROM file_index WHERE is_golden = 1 AND last_session_id = ?", (session_id,))
                golden_count = cur.fetchone()[0]
                
                cur.execute("SELECT COUNT(*) FROM file_index WHERE is_golden = 0 AND sha256_hash IS NOT NULL AND last_session_id = ?", (session_id,))
                legacy_count = cur.fetchone()[0]
            else:
                cur.execute("SELECT COUNT(*) FROM file_index WHERE is_golden = 1")
                golden_count = cur.fetchone()[0]
                
                cur.execute("SELECT COUNT(*) FROM file_index WHERE is_golden = 0 AND sha256_hash IS NOT NULL")
                legacy_count = cur.fetchone()[0]
            
            return {"golden": golden_count, "legacy": legacy_count}

    @staticmethod
    def classify_golden_versions(
        conn: sqlite3.Connection,
        progress: Optional[Callable[[int, int], None]] = None,
        batch_size: int = 1000,
    ) -> Dict[str, int]:
        """Classify golden vs. legacy files on a *caller-supplied* connection.

        Intended to be run from a worker thread that owns its own dedicated
        ``sqlite3`` connection (never the GUI thread's shared one). The work is
        chunked per duplicate-hash group and committed in batches so the caller
        can yield control between batches via ``progress``.

        Semantics are identical to :meth:`identify_golden_versions` with no
        ``session_id``: for each ``sha256_hash`` the lowest-``rowid`` row is the
        golden master and every other row sharing that hash is marked legacy.

        Args:
            conn: A dedicated SQLite connection owned by the calling thread.
            progress: Optional ``progress(done, total)`` callback invoked after
                each committed batch (used for buffered logging / yielding).
            batch_size: Number of duplicate groups per committed batch.

        Returns:
            Mapping with ``"golden"`` and ``"legacy"`` counts.
        """
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(file_index)")
        columns = [info[1] for info in cur.fetchall()]
        if 'is_golden' not in columns:
            conn.execute("ALTER TABLE file_index ADD COLUMN is_golden INTEGER DEFAULT 0")
            conn.commit()

        # Everything is golden by default; duplicates are demoted below.
        conn.execute("UPDATE file_index SET is_golden = 1")
        conn.commit()

        cur.execute('''
            SELECT sha256_hash, MIN(rowid)
            FROM file_index
            WHERE sha256_hash IS NOT NULL
            GROUP BY sha256_hash
            HAVING COUNT(*) > 1
        ''')
        dup_groups = cur.fetchall()
        total = len(dup_groups)

        pending = 0
        for index, (file_hash, keep_rowid) in enumerate(dup_groups):
            conn.execute(
                "UPDATE file_index SET is_golden = 0 "
                "WHERE sha256_hash = ? AND rowid != ?",
                (file_hash, keep_rowid),
            )
            pending += 1
            if pending >= batch_size:
                conn.commit()
                pending = 0
                if progress is not None:
                    progress(index + 1, total)
                # Yield the GIL so the main thread stays responsive.
                time.sleep(0.01)
        conn.commit()
        if progress is not None and total:
            progress(total, total)

        cur.execute("SELECT COUNT(*) FROM file_index WHERE is_golden = 1")
        golden_count = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM file_index "
            "WHERE is_golden = 0 AND sha256_hash IS NOT NULL"
        )
        legacy_count = cur.fetchone()[0]
        return {"golden": golden_count, "legacy": legacy_count}

    def get_mine_stats(self) -> Dict[str, int]:
        with self.conn:
            cur = self.conn.cursor()
            try:
                cur.execute("PRAGMA table_info(file_index)")
                columns = [info[1] for info in cur.fetchall()]
                if 'is_golden' not in columns:
                    golden_files = 0
                else:
                    cur.execute("SELECT COUNT(*) FROM file_index WHERE is_golden = 1")
                    golden_files = cur.fetchone()[0]
                
                cur.execute("SELECT COUNT(*) FROM file_index")
                total_files = cur.fetchone()[0]
                
                cur.execute("SELECT SUM(file_size) FROM file_index")
                total_storage = cur.fetchone()[0] or 0
            except Exception:
                total_files = 0
                golden_files = 0
                total_storage = 0
                
            return {
                "total_files": total_files,
                "golden_files": golden_files,
                "total_storage": total_storage
            }

    def get_recent_golden_files(self, limit: int = 50) -> List[str]:
        with self.conn:
            cur = self.conn.cursor()
            try:
                cur.execute("PRAGMA table_info(file_index)")
                columns = [info[1] for info in cur.fetchall()]
                if 'is_golden' not in columns:
                    return []
                
                cur.execute('''
                    SELECT full_path FROM file_index 
                    WHERE is_golden = 1 
                    ORDER BY modified_time DESC 
                    LIMIT ?
                ''', (limit,))
                return [row[0] for row in cur.fetchall()]
            except Exception:
                return []

    def get_duplicate_groups(
        self,
        scan_mode: str = 'Exact',
        threshold: int = 0,
        limit: int = 100,
        offset: int = 0,
        session_id: Optional[str] = None,
    ) -> Tuple[List[List[Path]], int]:
        groups = []
        total = 0
        with self.conn:
            cur = self.conn.cursor()
            if "Exact" in scan_mode:
                if session_id:
                    cur.execute("SELECT COUNT(DISTINCT sha256_hash) FROM file_index WHERE sha256_hash IS NOT NULL AND is_golden = 0 AND last_session_id = ?", (session_id,))
                    row = cur.fetchone()
                    total = row[0] if row else 0
                    
                    cur.execute("SELECT sha256_hash FROM file_index WHERE sha256_hash IS NOT NULL AND is_golden = 0 AND last_session_id = ? GROUP BY sha256_hash LIMIT ? OFFSET ?", (session_id, limit, offset))
                else:
                    cur.execute("SELECT COUNT(*) FROM (SELECT sha256_hash FROM file_index WHERE sha256_hash IS NOT NULL GROUP BY sha256_hash HAVING COUNT(rowid) > 1)")
                    row = cur.fetchone()
                    total = row[0] if row else 0
                    
                    cur.execute("SELECT sha256_hash FROM file_index WHERE sha256_hash IS NOT NULL GROUP BY sha256_hash HAVING COUNT(rowid) > 1 LIMIT ? OFFSET ?", (limit, offset))
                    
                for (h,) in cur.fetchall():
                    cur.execute("SELECT full_path FROM file_index WHERE sha256_hash = ? ORDER BY is_golden DESC, modified_time ASC", (h,))
                    groups.append([Path(row[0]) for row in cur.fetchall()])
            else:
                if session_id:
                    cur.execute("SELECT COUNT(*) FROM file_index WHERE phash IS NOT NULL AND last_session_id = ?", (session_id,))
                    row = cur.fetchone()
                    total = row[0] if row else 0
                    
                    cur.execute("SELECT phash, full_path FROM file_index WHERE phash IS NOT NULL AND last_session_id = ? LIMIT ? OFFSET ?", (session_id, limit, offset))
                else:
                    cur.execute("SELECT COUNT(*) FROM file_index WHERE phash IS NOT NULL")
                    row = cur.fetchone()
                    total = row[0] if row else 0
                    
                    cur.execute("SELECT phash, full_path FROM file_index WHERE phash IS NOT NULL LIMIT ? OFFSET ?", (limit, offset))
                records = cur.fetchall()
                
                fingerprints = []
                for phash_str, path_str in records:
                    try:
                        # Clean up the string representation of a tuple of hashes
                        clean_str = phash_str.strip('()').replace("'", "").replace('"', "")
                        hexes = clean_str.split(",")
                        hashes = tuple(imagehash.hex_to_hash(x.strip()) for x in hexes if x.strip())
                        if hashes:
                            fingerprints.append((hashes, Path(path_str)))
                    except (ValueError, TypeError, AttributeError):
                        # AUDIT-REVIEW: Narrow phash parse failures instead of swallowing all exceptions.
                        pass
                
                visited = set()
                for i in range(len(fingerprints)):
                    p_fp, p_path = fingerprints[i]
                    if p_path in visited: continue
                    group = [p_path]
                    visited.add(p_path)
                    for j in range(i+1, len(fingerprints)):
                        c_fp, c_path = fingerprints[j]
                        if c_path in visited: continue
                        if len(p_fp) == len(c_fp):
                            dist = sum(p_fp[k] - c_fp[k] for k in range(len(p_fp)))
                            if dist <= threshold:
                                group.append(c_path)
                                visited.add(c_path)
                    if len(group) > 1:
                        groups.append(group)
        return groups, total

    def close(self) -> None:
        """Close the SQLite connection if still open."""
        if self.conn:
            self.conn.close()
            self.conn = None  # type: ignore[assignment]

class IconFactory:
    @staticmethod
    def create_icons(color="#ffffff"):
        icons = {}
        def new_img(): return Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        
        def make_ctk(img):
            return ctk.CTkImage(light_image=img, dark_image=img, size=(20, 20))
        
        # Folder (Browse)
        img = new_img(); d = ImageDraw.Draw(img)
        d.polygon([(2, 4), (6, 4), (8, 6), (14, 6), (14, 12), (2, 12)], outline=color, fill=None)
        d.rectangle([3, 7, 13, 11], fill=color)
        icons['folder'] = make_ctk(img)

        # Play (Start)
        img = new_img(); d = ImageDraw.Draw(img)
        d.polygon([(5, 3), (5, 13), (13, 8)], fill=color)
        icons['play'] = make_ctk(img)

        # Pause
        img = new_img(); d = ImageDraw.Draw(img)
        d.rectangle([4, 3, 6, 13], fill=color); d.rectangle([10, 3, 12, 13], fill=color)
        icons['pause'] = make_ctk(img)

        # Stop
        img = new_img(); d = ImageDraw.Draw(img)
        d.rectangle([4, 4, 12, 12], fill=color)
        icons['stop'] = make_ctk(img)
        
        # Save
        img = new_img(); d = ImageDraw.Draw(img)
        d.rectangle([3, 3, 13, 13], outline=color); d.rectangle([5, 3, 11, 5], fill=color); d.rectangle([5, 9, 11, 11], fill=color)
        icons['save'] = make_ctk(img)
        
        # Trash
        img = new_img(); d = ImageDraw.Draw(img)
        d.rectangle([5, 5, 11, 13], outline=color); d.line([(4, 3), (12, 3)], fill=color); d.line([(7, 2), (9, 2)], fill=color)
        icons['trash'] = make_ctk(img)
        
        # Refresh/Reset
        img = new_img(); d = ImageDraw.Draw(img)
        d.arc([3, 3, 13, 13], 0, 270, fill=color, width=2); d.polygon([(13, 3), (13, 7), (9, 3)], fill=color)
        icons['refresh'] = make_ctk(img)
        
        # Search
        img = new_img(); d = ImageDraw.Draw(img)
        d.ellipse([3, 3, 10, 10], outline=color, width=2); d.line([(9, 9), (13, 13)], fill=color, width=2)
        icons['search'] = make_ctk(img)
        
        # Arrow Right
        img = new_img(); d = ImageDraw.Draw(img)
        d.line([(3, 8), (11, 8)], fill=color, width=2); d.polygon([(11, 5), (11, 11), (14, 8)], fill=color)
        icons['arrow'] = make_ctk(img)
        
        # Check
        img = new_img(); d = ImageDraw.Draw(img)
        d.line([(3, 8), (6, 11), (13, 4)], fill=color, width=2)
        icons['check'] = make_ctk(img)

        # Close
        img = new_img(); d = ImageDraw.Draw(img)
        d.line([(4, 4), (12, 12)], fill=color, width=2); d.line([(4, 12), (12, 4)], fill=color, width=2)
        icons['close'] = make_ctk(img)
        return icons

APP_VERSION = "2.0.0"

# ==========================================
#               LOGIC CLASSES
# ==========================================

def _path_within_root(child: Path, root: Path) -> bool:
    """Return True if ``child`` resolves under ``root`` (blocks path traversal via ..)."""
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class FileAuditor:
    """Scan a directory tree, hash files, detect duplicates, and optionally index them in SQLite."""

    def __init__(
        self,
        root_path: Union[str, Path],
        move_to: Optional[Union[str, Path]] = None,
        delete: bool = False,
        dry_run: bool = True,
        threads: int = 4,
        report_file: Optional[str] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        stop_event: Optional[threading.Event] = None,
        ignore_exts: Optional[List[str]] = None,
        ignore_folders: Optional[List[str]] = None,
        threshold: int = 0,
        review_mode: bool = False,
        pause_event: Optional[threading.Event] = None,
        db_manager: Optional[DatabaseManager] = None,
        session_id: Optional[str] = None,
        license_key: Optional[str] = None,
    ) -> None:
        self.root_path = Path(root_path).resolve()
        self.move_to = Path(move_to).resolve() if move_to else None
        self.delete = delete
        self.dry_run = dry_run
        self.threads = threads
        self.report_file = report_file
        self.log = log_callback if log_callback else print
        self.update_progress = progress_callback if progress_callback else lambda x, y, z: None
        self.stop_event = stop_event if stop_event else threading.Event()
        self.pause_event = pause_event if pause_event else threading.Event(); self.pause_event.set()
        self.ignore_exts = tuple(e.lower() for e in ignore_exts) if ignore_exts else ()
        self.ignore_folders = set(f.lower() for f in ignore_folders) if ignore_folders else set()
        self.threshold = threshold
        self.review_mode = review_mode
        self.db_manager = db_manager
        self.session_id = session_id
        self.license_key = (license_key or config.get("proLicenseKey") or "").strip()
        self._state_path = resolve_state_path(
            Path(db_manager.db_path) if db_manager and db_manager.db_path else None
        )
        self.device_id = get_drive_id(self.root_path)
        # AUDIT-REVIEW: Register the device up front so the enforced
        # file_index.device_id -> devices FK resolves during indexing.
        if self.db_manager and self.device_id:
            try:
                self.db_manager.register_device(self.device_id, platform.node())
            except Exception as exc:
                self.log(f"Device registration failed: {exc}")
        self.files_scanned = 0
        self.duplicates_found = 0
        self.bytes_saved = 0

    def get_partial_hash(self, filepath: Path) -> Optional[str]:
        """Return the SHA-256 hex digest of the first 4 KiB of ``filepath``.

        Used as a fast pre-filter before full hashing.

        Args:
            filepath: Path to the file to read.

        Returns:
            The hex digest string, or ``None`` if the file cannot be read.
        """
        try:
            with open(filepath, 'rb') as f:
                return hashlib.sha256(f.read(4096)).hexdigest()
        except OSError:
            # AUDIT-REVIEW: Handle unreadable files explicitly instead of bare except.
            return None

    def get_file_hash(self, filepath: Path, chunk_size: int = 1048576) -> Optional[str]:
        """Return the full SHA-256 hex digest of ``filepath``.

        Delegates to :func:`ingest_kernel.parse_metadata_and_hash` (streaming
        readinto with a per-thread buffer). ``chunk_size`` is accepted for API
        compatibility but does not change the optimized read strategy.

        Args:
            filepath: Path to the file to hash.
            chunk_size: Retained for backward compatibility with callers/tests.

        Returns:
            The 64-character SHA-256 hex digest, or ``None`` if the file cannot
            be read.
        """
        del chunk_size
        self.pause_event.wait()
        record = ingest_kernel.parse_metadata_and_hash(filepath)
        if record.get("status") == "success":
            return record["hash"]
        return None

    def run(self) -> None:
        try:
            assert_processing_allowed(self.license_key, state_path=self._state_path)
        except FreemiumLimitExceeded as exc:
            self.log(str(exc))
            raise
        self.log(f"--- Starting Exact Audit on: {self.root_path} ---")
        size_map = defaultdict(list)
        self.update_progress(0, 0, "Scanning file sizes...")
        
        for dirpath, dirnames, filenames in os.walk(self.root_path):
            self.pause_event.wait()
            if self.stop_event.is_set(): break
            dirnames[:] = [d for d in dirnames if d.lower() not in self.ignore_folders]
            if self.move_to and self.move_to in Path(dirpath).parents: continue
            for filename in filenames:
                self.pause_event.wait()
                if self.stop_event.is_set(): break
                if filename.lower().endswith(self.ignore_exts): continue
                filepath = Path(dirpath) / filename
                try:
                    try:
                        record_processed_file(
                            self.license_key, state_path=self._state_path
                        )
                    except FreemiumLimitExceeded as exc:
                        self.log(str(exc))
                        raise
                    size = filepath.stat().st_size
                    size_map[size].append(filepath)
                    self.files_scanned += 1
                    if self.files_scanned % 100 == 0: self.update_progress(0, 1, f"Scanning: {self.files_scanned} files") # Use 0/1 for indeterminate
                except OSError: continue

        full_tasks = [(fp, s) for s, paths in size_map.items() for fp in paths]
        processed_groups = defaultdict(lambda: defaultdict(list))

        # Phase 2: concurrent hash + DB ingest via producer/consumer pipeline
        if full_tasks and not self.stop_event.is_set():
            paths_only = [fp for fp, _ in full_tasks]
            total = len(paths_only)
            self.update_progress(0, total, "Hashing content...")
            db_path = (
                Path(self.db_manager.db_path) if self.db_manager is not None else None
            )
            _, _, collected = run_pipeline(
                paths_only,
                db_path,
                device_id=self.device_id,
                session_id=self.session_id,
                stop_event=self.stop_event,
                pause_event=self.pause_event,
            )
            completed = 0
            for path, record in collected:
                self.pause_event.wait()
                if self.stop_event.is_set():
                    break
                if record.get("status") == "success":
                    meta = record["metadata"]
                    processed_groups[meta["size"]][record["hash"]].append(path)
                completed += 1
                self.update_progress(
                    completed, total, f"Hashing: {completed}/{total}"
                )

        for size, hash_group in processed_groups.items():
            for h, file_list in hash_group.items():
                if len(file_list) > 1: self.handle_duplicates(file_list)
        
        self.log("Audit Complete.")

    def handle_duplicates(self, file_list: List[Path]) -> None:
        if self.review_mode:
            return
        # Basic auto-resolve logic (Keep Oldest)
        try:
            file_list.sort(key=lambda x: x.stat().st_ctime)
        except OSError:
            # AUDIT-REVIEW: Sort failures should not abort duplicate handling.
            pass
        original = file_list[0]
        duplicates = file_list[1:]
        self.duplicates_found += len(duplicates)
        self.bytes_saved += sum(f.stat().st_size for f in duplicates)
        self.log(f"Keeping: {original.name}")
        for dupe in duplicates:
            if not self.dry_run:
                try:
                    if self.delete:
                        os.remove(dupe)
                    elif self.move_to:
                        # AUDIT-REVIEW: Reject paths that escape the scan root before building move targets.
                        if not _path_within_root(dupe, self.root_path):
                            self.log(f"Skipped unsafe path (outside root): {dupe.name}")
                            continue
                        target = self.move_to / dupe.relative_to(self.root_path)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(dupe), str(target))
                except (OSError, shutil.Error) as e:
                    self.log(f"Error processing {dupe.name}: {e}")
            self.log(f"  {'Deleted' if self.delete else 'Copied'}: {dupe.name}")

class VideoFileAuditor(FileAuditor):
    """Perceptual-hash audit for images and video files."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.valid_extensions = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.jpg', '.jpeg', '.png', '.bmp'}
        self.hash_cache: Dict[Path, Any] = {}

    def get_fingerprint(self, filepath: Path) -> Optional[Tuple[Any, ...]]:
        cap = None
        try:
            if filepath.suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp'}:
                self.pause_event.wait()
                with Image.open(filepath) as img:
                    return (imagehash.phash(img),)

            cap = cv2.VideoCapture(str(filepath))
            if not cap.isOpened():
                return None
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if count < 10:
                return None
            hashes = []
            for p in [0.1, 0.5, 0.9]:
                self.pause_event.wait()
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(count * p))
                ret, frame = cap.read()
                if ret:
                    hashes.append(imagehash.phash(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))))
            return tuple(hashes) if len(hashes) == 3 else None
        except (OSError, cv2.error, ValueError, TypeError):
            # AUDIT-REVIEW: Narrow media decode failures; release capture in finally below.
            return None
        finally:
            # AUDIT-REVIEW: Ensure VideoCapture is released to avoid native resource leaks.
            if cap is not None:
                cap.release()

    def run(self) -> None:
        self.log(f"--- Starting Visual/Video Audit ---")
        files = []
        for dirpath, dirnames, filenames in os.walk(self.root_path):
            self.pause_event.wait()
            if self.stop_event.is_set(): break
            dirnames[:] = [d for d in dirnames if d.lower() not in self.ignore_folders]
            if self.move_to and self.move_to in Path(dirpath).parents: continue
            for f in filenames:
                fp = Path(dirpath) / f
                if fp.suffix.lower() in self.valid_extensions: files.append(fp)
        
        self.log(f"Found {len(files)} media files.")
        fingerprints = []
        
        if files:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.threads) as executor:
                future_to_file = {executor.submit(self.get_fingerprint, fp): fp for fp in files}
                completed = 0
                for future in concurrent.futures.as_completed(future_to_file):
                    self.pause_event.wait()
                    if self.stop_event.is_set(): break
                    fp = future_to_file[future]
                    res = future.result()
                    if res: 
                        fingerprints.append((res, fp))
                        # Cache first frame hash for UI search
                        self.hash_cache[fp] = res[0]
                        if self.db_manager:
                            try:
                                mtime = fp.stat().st_mtime
                                size = fp.stat().st_size
                            except Exception:
                                mtime = 0.0
                                size = 0
                            
                            with self.db_manager.conn:
                                cur = self.db_manager.conn.cursor()
                                cur.execute('''
                                    UPDATE file_index 
                                    SET phash = ?, file_size = ?, modified_time = ?, last_session_id = ? 
                                    WHERE full_path = ?
                                ''', (str(res), size, mtime, self.session_id, str(fp)))
                                
                                if cur.rowcount == 0:
                                    cur.execute('''
                                        INSERT INTO file_index (sha256_hash, phash, file_name, file_size, modified_time, full_path, device_id, last_session_id)
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    ''', (None, str(res), fp.name, size, mtime, str(fp), self.device_id, self.session_id))
                    completed += 1
                    self.update_progress(completed, len(files), f"Analyzing: {completed}/{len(files)}")

        # Clustering (Simplified O(N^2) for brevity, BK-Tree preferred for production)
        self.log("Clustering...")
        fingerprints.sort(key=lambda x: str(x[1]))
        visited = set()
        threshold = self.threshold
        
        for i in range(len(fingerprints)):
            self.pause_event.wait()
            if self.stop_event.is_set(): break
            p_fp, p_path = fingerprints[i]
            if p_path in visited: continue
            group = [p_path]
            visited.add(p_path)
            
            for j in range(i+1, len(fingerprints)):
                c_fp, c_path = fingerprints[j]
                if c_path in visited: continue
                dist = sum(p_fp[k] - c_fp[k] for k in range(len(p_fp)))
                if dist <= threshold:
                    group.append(c_path)
                    visited.add(c_path)
            
            if len(group) > 1: self.handle_duplicates(group)
        self.log("Audit Complete.")

class FolderMerger:
    """Merge an incoming folder tree into a master folder with duplicate detection."""

    def __init__(
        self,
        master_root: Union[str, Path],
        incoming_root: Union[str, Path],
        mode: str = "copy",
        dupe_action: str = "ignore",
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        stop_event: Optional[threading.Event] = None,
        threads: int = 4,
        dry_run: bool = False,
    ) -> None:
        self.master_root = Path(master_root).resolve()
        self.incoming_root = Path(incoming_root).resolve()
        self.mode = mode; self.dupe_action = dupe_action; self.dry_run = dry_run
        self.log = log_callback if log_callback else print
        self.update_progress = progress_callback if progress_callback else lambda x, y, z: None
        self.stop_event = stop_event if stop_event else threading.Event()
        self.quarantine_path = self.incoming_root.parent / f"{self.incoming_root.name}_duplicates"
        self.stats = {"merged": 0, "duplicates": 0, "renamed": 0, "errors": 0}
        self.simulated_paths = set()

    def run(self):
        self.log(f"--- Merge Started ({'DRY' if self.dry_run else 'LIVE'}) ---")
        master_index = defaultdict(list)
        for r, _, fs in os.walk(self.master_root):
            for f in fs:
                try: master_index[Path(r).joinpath(f).stat().st_size].append(Path(r).joinpath(f))
                except: pass
        
        incoming = []
        total_bytes = 0
        for r, _, fs in os.walk(self.incoming_root):
            if self.quarantine_path in Path(r).parents: continue
            for f in fs:
                p = Path(r) / f
                incoming.append(p)
                try: total_bytes += p.stat().st_size
                except: pass
        
        processed_bytes = 0
        for i, inc in enumerate(incoming):
            if self.stop_event.is_set(): break
            try: sz = inc.stat().st_size
            except: sz = 0
            
            is_dupe = False
            if sz in master_index:
                h1 = self._hash(inc)
                for cand in master_index[sz]:
                    if h1 == self._hash(cand): is_dupe = True; break
            
            if is_dupe: self._handle_dupe(inc)
            else: self._merge(inc)
            
            processed_bytes += sz
            if i % 5 == 0: self.update_progress(processed_bytes, total_bytes, f"Processing: {i}/{len(incoming)}")
        self.log("Merge Complete.")

    def _hash(self, p: Path) -> Optional[str]:
        try:
            h = hashlib.sha256()
            with open(p, 'rb') as f:
                while c := f.read(65536):
                    if self.stop_event.is_set():
                        return None
                    h.update(c)
            return h.hexdigest()
        except OSError:
            # AUDIT-REVIEW: Handle unreadable merge candidates explicitly.
            return None

    def _handle_dupe(self, p: Path) -> None:
        self.stats['duplicates'] += 1
        if self.dupe_action == "delete" and not self.dry_run:
            os.remove(p)
        elif self.dupe_action == "quarantine" and not self.dry_run:
            # AUDIT-REVIEW: Only quarantine files that remain under the incoming root.
            if not _path_within_root(p, self.incoming_root):
                self.log(f"Skipped unsafe quarantine path: {p.name}")
                return
            dest = self.quarantine_path / p.relative_to(self.incoming_root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(p), str(dest))
        self.log(f"Duplicate: {p.name}")

    def _merge(self, p: Path) -> None:
        # AUDIT-REVIEW: Block merge targets built from paths outside the incoming tree.
        if not _path_within_root(p, self.incoming_root):
            self.stats['errors'] += 1
            self.log(f"Skipped unsafe merge path: {p.name}")
            return
        dest = self.master_root / p.relative_to(self.incoming_root)
        if dest.exists() or (self.dry_run and str(dest) in self.simulated_paths):
            dest = dest.with_name(f"{dest.stem}_{int(time.time())}{dest.suffix}")
            self.stats['renamed'] += 1
        
        if not self.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if self.mode == "move": shutil.copy2(str(p), str(dest))
            else: shutil.copy2(str(p), str(dest))
        else: self.simulated_paths.add(str(dest))
        self.stats['merged'] += 1
        self.log(f"Merged: {p.name}")

# ==========================================
#               GUI CLASSES
# ==========================================

class ReviewDialog:
    def __init__(self, parent, duplicate_groups, total_groups=0, db_manager=None, scan_mode="Exact", move_to_path=None, precomputed_hashes=None, threshold=5):
        self.top = ctk.CTkToplevel(parent)
        self.top.title("Review Duplicates")
        self.top.configure(fg_color="#181818")
        self._center_window(1100, 650)

        # Ensure the dialog opens on top and is modal
        self.top.transient(parent)
        self.top.grab_set()

        self.groups = duplicate_groups
        self.total_groups = total_groups
        self.db_manager = db_manager
        self.scan_mode = scan_mode
        self.move_to_path = move_to_path
        self.hash_cache = precomputed_hashes if precomputed_hashes else {}
        self.threshold = threshold
        self.undo_stack = []
        self.temp_dir = Path(tempfile.mkdtemp(prefix="dedup_staging_"))
        self.preview_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.thumbnail_cache = {}
        self.active_futures = {}
        self.latest_requests = {}
        self.icons = IconFactory.create_icons()
        
        if getattr(sys, 'frozen', False):
            base_path = Path(sys.executable).parent
        else:
            base_path = Path(__file__).parent
        self.targets_file = base_path / "move_targets.json"
        self.move_targets = self._load_targets()
        self.target_var = tk.StringVar(value='Select Folder...')
        
        self.all_pairs = []
        self.extensions = set()
        for group in self.groups:
            try:
                # Trust the DB: index 0 is explicitly the Golden version
                pair = (group[0], group[1])
                self.all_pairs.append(pair)
                self.extensions.add(pair[1].suffix.lower())
            except: continue
        self.pairs = self.all_pairs[:]
        self.current_index = 0
        self._init_ui()
        self._load_pair()

    def _center_window(self, width, height):
        screen_width = self.top.winfo_screenwidth()
        screen_height = self.top.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        self.top.geometry(f'{width}x{height}+{x}+{y}')

    def _load_targets(self):
        if self.targets_file.exists():
            try: return json.loads(self.targets_file.read_text())
            except: return []
        return []

    def _save_target(self):
        target = self.target_var.get()
        if target and target != 'Select Folder...':
            if target in self.move_targets:
                self.move_targets.remove(target)
            self.move_targets.insert(0, target)
            self.move_targets = self.move_targets[:5]
            self.cb_targets.configure(values=self.move_targets)
            try: self.targets_file.write_text(json.dumps(self.move_targets))
            except: pass

    def _init_ui(self):
        # Top Filter & Status
        f_top = ctk.CTkFrame(self.top, fg_color="#2A2A2A")
        f_top.pack(fill="x", padx=20, pady=(20, 10))
        
        ctk.CTkLabel(f_top, text="Filter by Type:").pack(side="left", padx=10, pady=10)
        self.filter_var = tk.StringVar()
        ext_list = sorted(list(self.extensions))
        self.cb_filter = ctk.CTkComboBox(f_top, variable=self.filter_var, values=ext_list, width=100, command=lambda e: self.apply_filter())
        self.cb_filter.pack(side="left", padx=5, pady=10)
        
        ctk.CTkButton(f_top, text="Clear", image=self.icons['close'], compound="left", fg_color="gray", command=self.clear_filter, width=80).pack(side="left", padx=5, pady=10)
        ctk.CTkButton(f_top, text="Delete All Shown", image=self.icons['trash'], compound="left", fg_color=COLOR_DANGER, hover_color=COLOR_DANGER_HOVER, command=self.delete_all_shown).pack(side="left", padx=5, pady=10)
        ctk.CTkButton(f_top, text="Copy All Shown", image=self.icons['arrow'], compound="left", fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, command=self.move_all_shown).pack(side="left", padx=5, pady=10)
        
        self.lbl_stats = ctk.CTkLabel(f_top, text=f"Total Duplicates: {len(self.pairs)}", text_color="#9E9E9E")
        self.lbl_stats.pack(side="right", padx=20, pady=10)
        
        # Images
        self.f_content = ctk.CTkFrame(self.top, fg_color="transparent")
        self.f_content.pack(fill="both", expand=True, padx=20, pady=10)
        
        self.f_img = ctk.CTkFrame(self.f_content, fg_color="transparent")
        
        # Left (Original)
        f_left = ctk.CTkFrame(self.f_img, fg_color="#2A2A2A")
        f_left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        ctk.CTkLabel(f_left, text="Original (Keep)", font=("Segoe UI", 14, "bold")).pack(pady=5)
        self.lbl_orig = ctk.CTkLabel(f_left, text="Loading Preview...")
        self.lbl_orig.pack(expand=True, pady=10)
        self.lbl_orig_path = ctk.CTkLabel(f_left, text="", wraplength=450, justify="center", font=("Consolas", 12), text_color="#9E9E9E")
        self.lbl_orig_path.pack(fill="x", pady=10, padx=10)

        # Right (Duplicate)
        f_right = ctk.CTkFrame(self.f_img, fg_color="#2A2A2A")
        f_right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        ctk.CTkLabel(f_right, text="Duplicate (Delete)", font=("Segoe UI", 14, "bold"), text_color="#A34B4B").pack(pady=5)
        self.lbl_dupe = ctk.CTkLabel(f_right, text="Loading Preview...")
        self.lbl_dupe.pack(expand=True, pady=10)
        self.lbl_dupe_path = ctk.CTkLabel(f_right, text="", wraplength=450, justify="center", font=("Consolas", 12), text_color="#9E9E9E")
        self.lbl_dupe_path.pack(fill="x", pady=10, padx=10)
        
        self.f_img.columnconfigure(0, weight=1); self.f_img.columnconfigure(1, weight=1)
        self.f_img.rowconfigure(0, weight=1)
        
        # Metadata View
        self.f_metadata = ctk.CTkScrollableFrame(self.f_content, fg_color="transparent")
        
        self._bind_context_menu(self.lbl_orig, True)
        self._bind_context_menu(self.lbl_orig_path, True)
        self._bind_context_menu(self.lbl_dupe, False)
        self._bind_context_menu(self.lbl_dupe_path, False)
        
        # Controls
        f_ctrl = ctk.CTkFrame(self.top, fg_color="transparent")
        f_ctrl.pack(fill="x", side="bottom", padx=20, pady=20)
        
        # Left controls
        f_c_left = ctk.CTkFrame(f_ctrl, fg_color="transparent")
        f_c_left.pack(side="left")
        ctk.CTkButton(f_c_left, text="Smart Select", image=self.icons['check'], compound="left", fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, command=self.smart_select, width=120).pack(side="left", padx=5, pady=10, anchor="center")
        ctk.CTkButton(f_c_left, text="Find Similar", image=self.icons['search'], compound="left", fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, command=self.find_similar, width=120).pack(side="left", padx=5, pady=10, anchor="center")
        if HAS_REPORTLAB: ctk.CTkButton(f_c_left, text="PDF", image=self.icons['save'], compound="left", fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, command=self.export_pdf, width=80).pack(side="left", padx=5, pady=10, anchor="center")
        ctk.CTkButton(f_c_left, text="CSV", image=self.icons['save'], compound="left", fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, command=self.export_csv, width=80).pack(side="left", padx=5, pady=10, anchor="center")
        
        # Center controls (Move)
        f_c_center = ctk.CTkFrame(f_ctrl, fg_color="transparent")
        f_c_center.pack(side="left", padx=20)
        
        target_frame = ctk.CTkFrame(f_c_center, fg_color="transparent")
        target_frame.pack(side="left", padx=10, pady=0, anchor="center")
        ctk.CTkLabel(target_frame, text="Copy destination (Vault folder)", font=("Segoe UI", 12)).grid(row=0, column=0, sticky="w")
        self.cb_targets = ctk.CTkComboBox(target_frame, variable=self.target_var, values=self.move_targets, width=150, state="readonly")
        self.cb_targets.grid(row=1, column=0, sticky="ew")
        
        def browse_target():
            d = filedialog.askdirectory()
            if d: 
                self.target_var.set(d)
                self._save_target()
            
        ctk.CTkButton(f_c_center, text="Browse", image=self.icons['folder'], compound="left", fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, command=browse_target, width=80).pack(side="left", padx=5, pady=10, anchor="center")
        ctk.CTkButton(f_c_center, text="Copy", image=self.icons['arrow'], compound="left", fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, command=self.move_dupe, width=80).pack(side="left", pady=10, anchor="center")
        
        # Right controls
        f_c_right = ctk.CTkFrame(f_ctrl, fg_color="transparent")
        f_c_right.pack(side="right")
        ctk.CTkButton(f_c_right, text="Undo", image=self.icons['refresh'], compound="left", fg_color="gray", command=self.undo_last, width=80).pack(side="right", padx=5, pady=10, anchor="center")
        ctk.CTkButton(f_c_right, text="Skip >", image=self.icons['arrow'], compound="right", fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, command=self.next_pair, width=80).pack(side="right", padx=5, pady=10, anchor="center")
        ctk.CTkButton(f_c_right, text="DELETE", image=self.icons['trash'], compound="left", fg_color=COLOR_DANGER, hover_color=COLOR_DANGER_HOVER, command=self.delete_dupe, width=100).pack(side="right", padx=10, pady=10, anchor="center")
        
        self.lbl_prog = ctk.CTkLabel(f_ctrl, text="0/0", font=("Segoe UI", 12, "bold"), text_color="#9E9E9E")
        self.lbl_prog.pack(side="right", padx=20)
        
        # Shortcuts
        self.top.bind("<Delete>", lambda e: self.delete_dupe())
        self.top.bind("<d>", lambda e: self.delete_dupe())
        self.top.bind("<s>", lambda e: self.next_pair())
        self.top.bind("<u>", lambda e: self.undo_last())

    def apply_filter(self):
        ext = self.filter_var.get().strip()
        if not ext: return
        self.pairs = [p for p in self.all_pairs if p[1].suffix.lower() == ext.lower()]
        self.current_index = 0
        self._load_pair()

    def clear_filter(self):
        self.filter_var.set("")
        self.cb_filter.set("")
        self.pairs = self.all_pairs[:]
        self.current_index = 0
        self._load_pair()

    def delete_all_shown(self):
        if not self.pairs: return
        if not messagebox.askyesno("Delete All", f"Are you sure you want to delete all {len(self.pairs)} duplicates currently listed?"): return
        
        operations = []
        restore_index = self.current_index
        count = 0
        for i, (orig, dupe) in enumerate(self.pairs):
            if not dupe.exists(): continue
            try:
                tmp = self.temp_dir / f"{uuid.uuid4()}_{dupe.name}"
                shutil.copy2(str(dupe), str(tmp))
                operations.append((tmp, dupe))
                count += 1
            except Exception as e: print(f"Error deleting {dupe}: {e}")
        
        if operations:
            self.undo_stack.append((operations, restore_index))
        self.current_index = len(self.pairs)
        self._load_pair()
        messagebox.showinfo("Success", f"Deleted {count} files.")

    def move_all_shown(self):
        if not self.pairs: return
        
        target_dir = self.target_var.get()
        if not target_dir or target_dir == 'Select Folder...' or not os.path.isdir(target_dir):
            messagebox.showwarning("No Destination", "Please select a valid destination folder from the copy destination dropdown first.")
            return

        if not messagebox.askyesno("Copy All", f"Are you sure you want to copy all {len(self.pairs)} duplicates currently listed to:\n\n{target_dir}?\n\nOriginal files will remain in place."): return

        target_path = Path(target_dir)
        self._save_target()
        operations = []
        restore_index = self.current_index
        count = 0
        for i, (orig, dupe) in enumerate(self.pairs):
            if not dupe.exists(): continue
            try:
                mtime = os.path.getmtime(dupe)
                date_str = time.strftime('%Y-%m-%d', time.localtime(mtime))
                folder_name = f"{date_str}_Archive"
                
                stem = dupe.stem
                if sum(c.isalpha() for c in stem) > 5 and not stem.upper().startswith(('IMG_', 'DSC', 'SCAN_')):
                    folder_name += f"_{stem}"
                    
                sub_folder = target_path / folder_name
                sub_folder.mkdir(parents=True, exist_ok=True)
                
                dest_file = sub_folder / dupe.name
                if dest_file.exists(): dest_file = sub_folder / f"{dupe.stem}_{int(time.time())}_{i}{dupe.suffix}"
                shutil.copy2(str(dupe), str(dest_file))
                operations.append((dest_file, dupe))
                count += 1
            except Exception as e: print(f"Error moving {dupe}: {e}")
        
        if operations:
            self.undo_stack.append((operations, restore_index))
        self.current_index = len(self.pairs)
        self._load_pair()
        messagebox.showinfo("Success", f"Copied {count} files to {target_dir}. Originals unchanged.")

    def _load_pair(self):
        if self.current_index >= len(self.pairs):
            self.f_metadata.pack_forget()
            self.f_img.pack(fill="both", expand=True)
            self.lbl_orig.configure(image=None, text="No more duplicates.")
            self.lbl_dupe.configure(image=None, text="")
            self.lbl_orig_path.configure(text="")
            self.lbl_dupe_path.configure(text="")
            return

        self.orig, self.dupe = self.pairs[self.current_index]
        self.lbl_prog.configure(text=f"Pair {self.current_index+1} of {len(self.pairs)}")
        self.lbl_stats.configure(text=f"Showing {len(self.pairs)} pairs")
        
        ext = self.orig.suffix.lower()
        visual_exts = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp'}
        
        if ext in visual_exts:
            self.f_metadata.pack_forget()
            self.f_img.pack(fill="both", expand=True)
            # Update Paths
            self.lbl_orig_path.configure(text=f"{self.orig}\nSize: {self._fmt_size(self.orig)}")
            self.lbl_dupe_path.configure(text=f"{self.dupe}\nSize: {self._fmt_size(self.dupe)}")
            
            self._show_img(self.lbl_orig, self.orig)
            self._show_img(self.lbl_dupe, self.dupe)
        else:
            self.f_img.pack_forget()
            self.f_metadata.pack(fill="both", expand=True)
            self.show_metadata_view((self.orig, self.dupe))

    def _fmt_size(self, path):
        try:
            s = path.stat().st_size
            for u in ['B','KB','MB','GB']:
                if s < 1024: return f"{s:.2f} {u}"
                s /= 1024
            return f"{s:.2f} TB"
        except: return "Unknown"

    def _show_img(self, lbl, path):
        # 1. Check Cache
        if path in self.thumbnail_cache:
            ctk_img = self.thumbnail_cache[path]
            lbl.configure(image=ctk_img, text="")
            lbl.image = ctk_img
            return

        # Update the latest requested path for this label
        self.latest_requests[lbl] = path

        # 2. Cancel pending future for this label to prevent queue flooding
        if lbl in self.active_futures:
            self.active_futures[lbl].cancel()
            del self.active_futures[lbl]

        lbl.configure(image=None, text="Loading...")
        
        def load_task():
            # EARLY EXIT: If UI has moved on to a different image, abort immediately
            if self.latest_requests.get(lbl) != path: return None

            try:
                ext = path.suffix.lower()
                video_exts = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v'}
                image_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp'}
                
                if ext in video_exts:
                    cap = cv2.VideoCapture(str(path))
                    if not cap.isOpened(): return {"type": "error", "message": "No Preview Available"}
                    cap.set(cv2.CAP_PROP_POS_FRAMES, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) * 0.5))
                    ret, frame = cap.read()
                    cap.release()
                    if not ret: return {"type": "error", "message": "No Preview Available"}
                    img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                elif ext in image_exts:
                    try:
                        img = Image.open(str(path))
                        img.load()
                    except Exception:
                        return {"type": "error", "message": "No Preview Available"}
                else:
                    return {"type": "text", "message": f"{ext.upper() if ext else 'Unknown'} File\n{path.name}"}
                
                # SECOND EXIT: Check again before heavy processing (resizing)
                if self.latest_requests.get(lbl) != path: return None

                # Convert to RGB to ensure compatibility with ImageTk
                if img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGB')
                img.thumbnail((400, 400))
                # Return raw data to prevent cross-thread object issues
                return {"type": "image", "data": img.tobytes(), "size": img.size, "mode": img.mode}
            except Exception as e:
                print(f"Error loading preview for {path}: {e}")
                return {"type": "error", "message": "No Preview Available"}

        def on_loaded(future):
            # Clean up future reference
            if lbl in self.active_futures:
                if self.active_futures[lbl] == future:
                    del self.active_futures[lbl]
            
            # Only update UI if this is still the requested image
            if self.latest_requests.get(lbl) != path: return

            try:
                result = future.result()
                if result and isinstance(result, dict):
                    if result["type"] == "image":
                        raw_data, size, mode = result["data"], result["size"], result["mode"]
                        # Recreate image in main thread
                        img = Image.frombytes(mode, size, raw_data)

                        # Create CTkImage. Size is required for display.
                        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=size)
                        
                        # Cache the result (limit size to avoid memory issues)
                        if len(self.thumbnail_cache) > 200: self.thumbnail_cache.clear()
                        self.thumbnail_cache[path] = ctk_img
                        
                        lbl.configure(image=ctk_img, text="")
                        lbl.image = ctk_img
                    else:
                        lbl.configure(image=None, text=result["message"])
                        lbl.image = None
                elif result and isinstance(result, tuple):
                    raw_data, size, mode = result
                    img = Image.frombytes(mode, size, raw_data)
                    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=size)
                    if len(self.thumbnail_cache) > 200: self.thumbnail_cache.clear()
                    self.thumbnail_cache[path] = ctk_img
                    lbl.configure(image=ctk_img, text="")
                    lbl.image = ctk_img
                else:
                    lbl.configure(image=None, text="[Preview Error]")
                    lbl.image = None
            except Exception as e:
                print(f"Error displaying preview: {e}")
                lbl.configure(image=None, text="[Display Error]")
                lbl.image = None

        future = self.preview_executor.submit(load_task)
        self.active_futures[lbl] = future
        future.add_done_callback(lambda f: self.top.after(0, on_loaded, f))

    def show_metadata_view(self, pair):
        if not hasattr(self, '_metadata_rows'):
            self._metadata_rows = []
            
        orig, dupe = pair
        duplicates = [dupe]
        try: duplicates.sort(key=lambda d: d.stat().st_mtime)
        except: pass

        total_needed = 1 + len(duplicates)
        
        while len(self._metadata_rows) < total_needed:
            f_row = ctk.CTkFrame(self.f_metadata)
            f_row.columnconfigure(0, weight=1)
            
            lbl_title = ctk.CTkLabel(f_row, text="", font=("Segoe UI", 14, "bold"))
            lbl_title.grid(row=0, column=0, sticky="w", padx=10, pady=(10,0))
            
            lbl_details = ctk.CTkLabel(f_row, text="", justify="left", font=("Consolas", 12), text_color="#9E9E9E")
            lbl_details.grid(row=1, column=0, sticky="w", padx=10, pady=(5,10))
            
            btn_open = ctk.CTkButton(f_row, text="Open File Location", fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, width=130)
            btn_open.grid(row=0, column=1, rowspan=2, sticky="e", padx=20, pady=10)
            
            self._metadata_rows.append((f_row, lbl_title, lbl_details, btn_open))
            
        for r in self._metadata_rows:
            r[0].pack_forget()
            
        def update_row(idx, title, path, is_golden):
            f_row, lbl_title, lbl_details, btn_open = self._metadata_rows[idx]
            f_row.pack(fill="x", pady=5, padx=10)
            
            try:
                mtime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(path.stat().st_mtime))
                size = self._fmt_size(path)
            except:
                mtime = "Unknown"
                size = "Unknown"
                
            color = COLOR_INFO if is_golden else "#A34B4B"
            lbl_title.configure(text=title, text_color=color)
            lbl_details.configure(text=f"Path: {path}\nSize: {size}  |  Modified: {mtime}")
            btn_open.configure(command=lambda p=path: self._ctx_action_path(p, 'folder'))

        update_row(0, "Golden Version", orig, True)
        for i, d in enumerate(duplicates):
            update_row(i+1, f"Duplicate Version {i+1}" if len(duplicates)>1 else "Duplicate Version", d, False)

    def _ctx_action_path(self, path: Path, action: str) -> None:
        if action == 'folder' and path.exists():
            try:
                if platform.system() == 'Windows':
                    # AUDIT-REVIEW: Pass explorer arguments as a list to avoid shell/path injection.
                    subprocess.Popen(['explorer', '/select,', str(path.resolve())])
                elif platform.system() == 'Darwin':
                    subprocess.call(['open', '-R', str(path)])
                else:
                    subprocess.call(['xdg-open', str(path.parent)])
            except OSError:
                pass

    def delete_dupe(self):
        try:
            tmp = self.temp_dir / f"{uuid.uuid4()}_{self.dupe.name}"
            shutil.copy2(str(self.dupe), str(tmp))
            operations = [(tmp, self.dupe)]
            self.undo_stack.append((operations, self.current_index))
            self.next_pair()
        except Exception as e: messagebox.showerror("Error", str(e))

    def undo_last(self):
        if self.undo_stack:
            operations, idx = self.undo_stack.pop()
            for src, dest in operations:
                shutil.copy2(str(src), str(dest))
            self.current_index = idx
            self._load_pair()

    def next_pair(self):
        self.current_index += 1
        self._load_pair()

    def move_dupe(self):
        target_dir = self.target_var.get()
        if not target_dir or target_dir == 'Select Folder...' or not os.path.isdir(target_dir):
            messagebox.showwarning("No Destination", "Please select a valid destination folder.")
            return
        try:
            target_path = Path(target_dir)
            mtime = os.path.getmtime(self.dupe)
            date_str = time.strftime('%Y-%m-%d', time.localtime(mtime))
            folder_name = f"{date_str}_Archive"
            
            stem = self.dupe.stem
            if sum(c.isalpha() for c in stem) > 5 and not stem.upper().startswith(('IMG_', 'DSC', 'SCAN_')):
                folder_name += f"_{stem}"
                
            sub_folder = target_path / folder_name
            sub_folder.mkdir(parents=True, exist_ok=True)
            
            dest = sub_folder / self.dupe.name
            if dest.exists(): dest = sub_folder / f"{self.dupe.stem}_{int(time.time())}{self.dupe.suffix}"
            shutil.copy2(str(self.dupe), str(dest))
            operations = [(dest, self.dupe)]
            self._save_target()
            self.undo_stack.append((operations, self.current_index))
            self.next_pair()
        except Exception as e: messagebox.showerror("Error", str(e))

    def smart_select(self):
        for i in range(self.current_index, len(self.pairs)):
            o, d = self.pairs[i]
            try:
                if d.stat().st_size > o.stat().st_size: self.pairs[i] = (d, o)
            except: pass
        self._load_pair()
        messagebox.showinfo("Info", "Smart select complete")

    def find_similar(self):
        if not self.hash_cache:
            messagebox.showinfo("Info", "This feature is only available in Visual/Video audit mode.", parent=self.top)
            return

        try:
            target_hash = self.hash_cache.get(self.dupe)
            if not target_hash:
                messagebox.showerror("Error", f"Could not find hash for {self.dupe.name}.", parent=self.top)
                return

            similar_files = []
            checked_paths = {self.dupe}
            similarity_threshold = self.threshold if self.threshold > 0 else 5

            for path, h in self.hash_cache.items():
                if path in checked_paths: continue
                distance = target_hash - h
                if 0 < distance <= similarity_threshold:
                    similar_files.append((path, distance))
                checked_paths.add(path)

            if not similar_files:
                messagebox.showinfo("No Similar Found", f"No other files found within a similarity threshold of {similarity_threshold}.", parent=self.top)
            else:
                self._show_similar_results(similar_files)
        except Exception as e:
            messagebox.showerror("Error", f"Could not find similar files: {e}", parent=self.top)

    def _show_similar_results(self, similar_files):
        win = ctk.CTkToplevel(self.top)
        win.title(f"Files similar to {self.dupe.name}")
        win.geometry("700x400")
        win.transient(self.top)
        win.grab_set()
        
        similar_files.sort(key=lambda x: x[1])
        
        f = ctk.CTkFrame(win)
        f.pack(fill="both", expand=True, padx=20, pady=20)
        ctk.CTkLabel(f, text=f"Found {len(similar_files)} similar files:").pack(anchor="w", pady=(0, 10))
        
        txt = ctk.CTkTextbox(f, font=("Consolas", 12))
        txt.pack(fill="both", expand=True, pady=5)
        for path, distance in similar_files: 
            txt.insert("end", f"Distance: {distance}\t| Path: {path}\n")
        txt.configure(state="disabled")
        ctk.CTkButton(win, text="Close", command=win.destroy).pack(pady=10)

    def export_pdf(self):
        f = filedialog.asksaveasfilename(defaultextension=".pdf")
        if f:
            c = canvas.Canvas(f, pagesize=letter)
            y = 750
            for o, d in self.pairs:
                if y < 100: c.showPage(); y = 750
                c.drawString(50, y, f"Orig: {o.name}"); c.drawString(300, y, f"Dupe: {d.name}")
                y -= 20
            c.save()
            messagebox.showinfo("Export", "PDF Saved")

    def export_csv(self):
        f = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")])
        if f:
            try:
                with open(f, "w", newline="", encoding="utf-8") as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(["Original", "Duplicate", "Size", "Original Path", "Duplicate Path"])
                    for o, d in self.pairs:
                        writer.writerow([o.name, d.name, self._fmt_size(o), str(o), str(d)])
                messagebox.showinfo("Export", "CSV Saved")
            except Exception as e: messagebox.showerror("Error", f"Could not save CSV: {e}")

    def _show_properties(self, path):
        try:
            stats = path.stat()
            prop_win = ctk.CTkToplevel(self.top)
            prop_win.title(f"Properties: {path.name}")
            
            f = ctk.CTkFrame(prop_win)
            f.pack(fill="both", expand=True, padx=20, pady=20)

            details = {
                "File Name:": path.name,
                "Full Path:": str(path),
                "Size:": f"{self._fmt_size(path)} ({stats.st_size:,} bytes)",
                "Date Created:": time.ctime(stats.st_ctime),
                "Date Modified:": time.ctime(stats.st_mtime),
            }

            try:
                if path.suffix.lower() in {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v'}:
                    cap = cv2.VideoCapture(str(path))
                    if cap.isOpened():
                        details["Dimensions:"] = f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))} x {int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}"
                        cap.release()
                else:
                    with Image.open(path) as img: details["Dimensions:"] = f"{img.width} x {img.height}"
            except: details["Dimensions:"] = "N/A"

            for i, (key, value) in enumerate(details.items()):
                ctk.CTkLabel(f, text=key, font=("Segoe UI", 12, "bold")).grid(row=i, column=0, sticky="nw", padx=10, pady=5)
                entry = ctk.CTkEntry(f, width=300, font=("Consolas", 12))
                entry.insert(0, value)
                entry.configure(state="readonly")
                entry.grid(row=i, column=1, sticky="ew", padx=10, pady=5)
            
            f.columnconfigure(1, weight=1)
            ctk.CTkButton(prop_win, text="Close", command=prop_win.destroy).pack(pady=10)
        except Exception as e: messagebox.showerror("Error", f"Could not get properties for {path.name}:\n{e}", parent=self.top)

    def _bind_context_menu(self, widget, is_original):
        menu = tk.Menu(self.top, tearoff=0)
        menu.add_command(label="Open File", command=lambda: self._ctx_action(is_original, 'file'))
        menu.add_command(label="Open Folder", command=lambda: self._ctx_action(is_original, 'folder'))
        menu.add_command(label="Copy Path", command=lambda: self._ctx_action(is_original, 'copy'))
        menu.add_separator()
        menu.add_command(label="Properties", command=lambda: self._ctx_action(is_original, 'properties'))
        widget.bind("<Button-3>", lambda e: menu.post(e.x_root, e.y_root))

    def _ctx_action(self, is_original, action):
        if not hasattr(self, 'orig') or not hasattr(self, 'dupe'): return
        path = self.orig if is_original else self.dupe
        
        if action == 'copy':
            self.top.clipboard_clear(); self.top.clipboard_append(str(path)); self.top.update()
        elif action == 'file' and path.exists():
            try:
                if platform.system() == 'Windows': os.startfile(path)
                elif platform.system() == 'Darwin': subprocess.call(['open', path])
                else: subprocess.call(['xdg-open', path])
            except: pass
        elif action == 'folder' and path.exists():
            try:
                if platform.system() == 'Windows':
                    # AUDIT-REVIEW: Pass explorer arguments as a list to avoid shell/path injection.
                    subprocess.Popen(['explorer', '/select,', str(path.resolve())])
                elif platform.system() == 'Darwin':
                    subprocess.call(['open', '-R', str(path)])
                else:
                    subprocess.call(['xdg-open', str(path.parent)])
            except OSError:
                pass
        elif action == 'properties' and path.exists():
            self._show_properties(path)

# ==========================================
#            UI THEME CONSTANTS
# ==========================================
# Semantic colour palette: safe (green), caution (amber), destructive (red),
# informational/utility (teal/grey), used consistently so users can read an
# action's intent from its colour at a glance.
COLOR_SAFE = "#1d6a73"
COLOR_SAFE_HOVER = "#154d54"
COLOR_CAUTION = "#4A4D50"
COLOR_CAUTION_HOVER = "#393C3E"
COLOR_DANGER = "#4A2222"
COLOR_DANGER_HOVER = "#3A1A1A"
# Brand cyan sampled from the sovraan 2.0 logo (#66FCF1). Because the fill is
# bright, on-cyan text/icons use a near-black foreground for accessible contrast.
COLOR_INFO = "#1d6a73"
COLOR_INFO_HOVER = "#154d54"
COLOR_ON_INFO = "#EAEAEA"
COLOR_NEUTRAL = "#4A4D50"
COLOR_NEUTRAL_HOVER = "#393C3E"
COLOR_HINT = "#9E9E9E"
BRAND_BLACK = "#0B0B0D"

FONT_TITLE = ("Segoe UI", 20, "bold")
FONT_HEADER = ("Segoe UI", 15, "bold")
FONT_BODY = ("Segoe UI", 12)
FONT_HINT = ("Segoe UI", 12)
FONT_CALM_STEP = ("Segoe UI", 14, "bold")
FONT_CALM_BODY = ("Segoe UI", 12)
FONT_CALM_SMALL = ("Segoe UI", 12)
JOURNEY_PADY = 0
STEP3_RUN_GAP = 8

CALM_STEP1_TITLE = "Map the Swamp"
CALM_STEP2_TITLE = "Secure the Gold"
CALM_STEP3_TITLE = "Ignite Your Mind"

COLLISION_POLICY_LABELS = ("Skip (Safe)", "Overwrite", "Rename")
COLLISION_LABEL_TO_CONFIG = {
    "Skip (Safe)": "skip",
    "Overwrite": "overwrite",
    "Rename": "rename",
}
COLLISION_CONFIG_TO_LABEL = {v: k for k, v in COLLISION_LABEL_TO_CONFIG.items()}

HASHING_DEPTH_LABELS = ("Quick Check", "Deep Cryptographic")
HASHING_LABEL_TO_CONFIG = {
    "Quick Check": "quick",
    "Deep Cryptographic": "deep",
}
HASHING_CONFIG_TO_LABEL = {v: k for k, v in HASHING_LABEL_TO_CONFIG.items()}


class Tooltip:
    """Lightweight hover tooltip for any Tk/CustomTkinter widget.

    CustomTkinter ships no tooltip primitive, so this binds enter/leave events
    and shows a small borderless ``Toplevel`` after a short delay. Used to
    explain advanced toggles without crowding the layout with descriptions.
    """

    def __init__(self, widget: Any, text: str, delay: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _show(self) -> None:
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        except tk.TclError:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        try:
            self._tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(
            self._tip, text=self.text, justify="left", bg="#1C1C1E", fg="#EAEAEA",
            relief="solid", borderwidth=1, font=("Segoe UI", 12), padx=8, pady=5,
            wraplength=320,
        ).pack()

    def _hide(self, _event: Any = None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None


class SovraanApp:
    def __init__(self):
        class LoggerWrapper:
            def __init__(self, log_func):
                self.log_func = log_func
            def info(self, msg):
                self.log_func(msg)
        self.log_func = lambda msg: self.log(msg)
        self.logger = LoggerWrapper(self.log_func)

        self.root = ctk.CTk()
        self.root.configure(fg_color="#181818")
        self.root.title("sovraan — Deduplication & Cryptographic Notary")
        self.root.minsize(1100, 700)
        self.root.after(100, lambda: self.root.state('zoomed'))
        self._center_window(1100, 720)
        self.review_dialog = None

        # Set application window icon (brand mark) via the lazy asset cache.
        self._set_window_icon()

        self.db_manager = DatabaseManager()
        self.db_path = self.db_manager.db_path
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        
        # Theme Setup
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        
        self.icons = IconFactory.create_icons()
        # Dark-foreground icon variant for use on bright (brand cyan) fills.
        self.icons_dark = IconFactory.create_icons(color=COLOR_ON_INFO)

        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=0)
        self.root.grid_rowconfigure(2, weight=0)
        self.root.grid_rowconfigure(3, weight=0)
        self.root.grid_columnconfigure(0, weight=1)

        self.nb = ctk.CTkTabview(
            self.root,
            segmented_button_selected_color=COLOR_INFO,
            segmented_button_selected_hover_color=COLOR_INFO_HOVER,
            segmented_button_unselected_color="#2A2A2A",
            text_color="#EAEAEA",
        )
        self.nb.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 0))

        self.t_journey = self.nb.add("Your Journey")
        self.t_merge = self.nb.add("Merge Folders")
        self.t_expert = self.nb.add("Expert Studio")
        self.t_vault_index = self.nb.add("The Vault Index")

        f_log = ctk.CTkFrame(self.root, fg_color="transparent")
        f_log.grid(row=1, column=0, sticky="ew", padx=20, pady=(10, 5))
        ctk.CTkLabel(f_log, text="Background notes:").pack(side="left", padx=5)
        ctk.CTkButton(f_log, text="Clear Log", image=self.icons['trash'], compound="left", fg_color="gray", command=self.clear_log, width=100).pack(side="right")
        self.btn_save_log = ctk.CTkButton(
            f_log, text="Save Log", image=self.icons['save'], compound="left",
            fg_color="gray", command=self.save_log, width=100,
        )
        self.btn_save_log.pack(side="right", padx=10)

        self.log_area = ctk.CTkTextbox(self.root, height=120, font=("Consolas", 12))
        self.log_area.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 10))
        self.pbar = ctk.CTkProgressBar(self.root)
        self.pbar.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 20))
        self.pbar.set(0)

        self.journey_export_mode = tk.StringVar(
            value=config.get("journey_export_mode", "Standard Mode"),
        )
        _vault_init = self._initial_vault_path()
        self.target_vault_dir = tk.StringVar(value=_vault_init)
        try:
            self.vector_engine = SovereignVectorEngine(_vault_init)
        except Exception as exc:
            self.vector_engine = None
        self.mode_var = tk.StringVar(value="Exact")
        self.review_var = tk.BooleanVar(value=bool(config.get("review_duplicates", True)))
        self.notarise_var = tk.BooleanVar(value=bool(config.get("notarise", True)))

        self._init_your_journey_tab()
        self._init_merge_folders_tab()
        self._init_expert_studio_tab()
        self._init_vault_index_tab()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Thread-safe log buffer drained onto the widget every 100ms on the
        # main thread. Worker threads enqueue lines (never touch the widget).
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self.root.after(100, self._process_log_queue)

    def _initial_vault_path(self) -> str:
        raw = (config.get("vault_path") or config.get("last_dest") or "").strip()
        if raw:
            return raw
        return str((Path.home() / "Desktop" / "sovraan_vault").resolve())

    @staticmethod
    def _split_csv_setting(key: str) -> List[str]:
        raw = config.get(key, "") or ""
        return [part.strip() for part in str(raw).split(",") if part.strip()]

    @staticmethod
    def _shutil_copy_from_config() -> Callable[[str, str], Any]:
        method_name = config.get("copy_method", "copy2")
        return getattr(shutil, method_name, shutil.copy2)

    def _center_window(self, width, height):
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        self.root.geometry(f'{width}x{height}+{x}+{y}')

    def _asset_base(self) -> str:
        """Return the directory that holds bundled assets (icon, wordmark).

        Resolves correctly both when running from source and when frozen by
        PyInstaller (where assets are unpacked into ``sys._MEIPASS``).
        """
        if getattr(sys, 'frozen', False):
            return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        return os.path.dirname(os.path.abspath(__file__))

    def _runtime_base(self) -> str:
        """Return the directory for *writable* runtime state.

        Unlike :meth:`_asset_base` (read-only bundle, ``sys._MEIPASS`` when
        frozen), this resolves next to the executable when frozen so the
        database and any exported logs live alongside the binary rather than in
        the ephemeral one-file extraction directory.
        """
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    def _resolve_brand_source(self, *names: str) -> Optional[str]:
        """Return the first existing branding source from ``assets/``.

        Lets an SVG master take precedence over a raster fallback when both are
        shipped (e.g. ``icon.svg`` before ``icon.png``).

        Args:
            *names: Candidate file names to probe inside ``assets/``.

        Returns:
            Absolute path to the first existing candidate, or ``None``.
        """
        assets_dir = os.path.join(self._asset_base(), "assets")
        for name in names:
            candidate = os.path.join(assets_dir, name)
            if os.path.exists(candidate):
                return candidate
        return None

    @staticmethod
    def _rasterise_asset(source_path: str, size: Tuple[int, int]) -> Image.Image:
        """Rasterise/resize a branding source to an RGBA image of ``size``.

        SVG sources are rendered with ``cairosvg`` (imported lazily so it stays
        an optional dependency); raster masters are resized with high-quality
        Lanczos resampling.

        Args:
            source_path: Path to the master asset (``.svg`` or raster image).
            size: Target ``(width, height)`` in pixels.

        Returns:
            An RGBA :class:`PIL.Image.Image` at the requested size.
        """
        width, height = size
        if source_path.lower().endswith(".svg"):
            try:
                import cairosvg
            except ImportError as exc:
                raise RuntimeError(
                    "cairosvg is required to rasterise SVG branding assets; "
                    "install it or provide a raster (PNG) master."
                ) from exc
            import io
            png_bytes = cairosvg.svg2png(
                url=source_path, output_width=width, output_height=height
            )
            return Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        return Image.open(source_path).convert("RGBA").resize((width, height), Image.LANCZOS)

    def _ensure_cached_png(self, source_path: str, size: Tuple[int, int]) -> str:
        """Return the path to a cached PNG of ``source_path`` at ``size``.

        Implements the lazy-loading cache: on a hit the existing PNG path is
        returned immediately; on a miss the source is rasterised/resized once
        and written to ``assets/cache/``. The cache key embeds the source stem
        and dimensions (e.g. ``icon_32.png`` / ``wordmark_320x64.png``).

        Args:
            source_path: Path to the master asset.
            size: Target ``(width, height)`` in pixels.

        Returns:
            Absolute path to the cached PNG.
        """
        width, height = size
        cache_dir = os.path.join(self._asset_base(), "assets", "cache")
        os.makedirs(cache_dir, exist_ok=True)
        stem = Path(source_path).stem
        suffix = f"{width}" if width == height else f"{width}x{height}"
        cache_path = os.path.join(cache_dir, f"{stem}_{suffix}.png")
        if os.path.exists(cache_path):
            return cache_path
        self._rasterise_asset(source_path, (width, height)).save(cache_path, format="PNG")
        return cache_path

    def get_branded_image(self, svg_path: str, size: Union[int, Tuple[int, int]]) -> ctk.CTkImage:
        """Return a ``CTkImage`` for a branding asset, using the lazy PNG cache.

        Branding sources (SVG or a high-resolution raster master) are converted
        to a PNG of the requested size exactly once and stored under
        ``assets/cache/``; subsequent launches load the cached PNG directly.
        This keeps startup fast (no repeated rasterisation) without committing
        dozens of static size variants.

        Args:
            svg_path: Path to the master asset (``.svg`` or raster image).
            size: Target size as an int (square) or ``(width, height)`` tuple.

        Returns:
            A :class:`customtkinter.CTkImage` ready to place in a widget.
        """
        width, height = (size, size) if isinstance(size, int) else size
        cached = self._ensure_cached_png(svg_path, (width, height))
        img = Image.open(cached)
        return ctk.CTkImage(light_image=img, dark_image=img, size=(width, height))

    def _set_window_icon(self) -> None:
        """Set the window/taskbar icon from the cached brand mark.

        Uses :meth:`_ensure_cached_png` to produce a 64px PNG once, applied via
        ``iconphoto``. A true ``.ico`` (if present) is additionally applied for
        the best Windows title-bar rendering. All failures degrade silently.
        """
        try:
            source = self._resolve_brand_source("icon.svg", "icon.png")
            if source:
                png_path = self._ensure_cached_png(source, (64, 64))
                self._window_icon_photo = tk.PhotoImage(file=png_path)
                self.root.iconphoto(True, self._window_icon_photo)
            ico_path = os.path.join(self._asset_base(), "app.ico")
            if os.path.exists(ico_path):
                self.root.iconbitmap(ico_path)
        except (tk.TclError, OSError, RuntimeError):
            pass

    def _init_header(self, parent: Any) -> None:
        """Build the branded header bar inside the given parent (Vault Elevation tab).

        Prefers a pre-rendered wordmark lockup at ``assets/wordmark.png`` (drop
        the official ``sovraan 2.0`` lockup there and it is used verbatim).
        If absent, a faithful lockup is composed from the brand icon plus the
        wordmark text and a cyan version badge. CustomTkinter needs images
        wrapped in :class:`CTkImage`, so references are retained on ``self`` to
        prevent garbage collection.
        """
        header = ctk.CTkFrame(parent, fg_color=BRAND_BLACK, corner_radius=8, height=82)
        header.pack(side="top", fill="x", padx=12, pady=(12, 8))
        header.pack_propagate(False)

        # Right-aligned background-task status indicator (click for an info
        # pop-up). Created first so the wordmark early-return cannot skip it.
        self.header_status = ctk.CTkLabel(
            header, text="", font=("Segoe UI", 12), text_color=COLOR_INFO, cursor="hand2",
        )
        self.header_status.pack(side="right", padx=18)
        self.header_status.bind("<Button-1>", lambda _e: self._show_rationalise_info())

        brand_col = ctk.CTkFrame(header, fg_color="transparent")
        brand_col.pack(side="left", padx=18, pady=8)
        title_row = ctk.CTkFrame(brand_col, fg_color="transparent")
        title_row.pack(anchor="w")

        # Preferred: official wordmark lockup image (SVG master or raster),
        # rendered once and cached at a header-friendly height.
        wordmark_source = self._resolve_brand_source("wordmark.svg", "wordmark.png")
        wordmark_packed = False
        if wordmark_source:
            try:
                target_h = 46
                ratio = 4.0  # default lockup aspect; refined for raster masters
                if not wordmark_source.lower().endswith(".svg"):
                    with Image.open(wordmark_source) as wm:
                        if wm.height:
                            ratio = wm.width / wm.height
                self._wordmark_img = self.get_branded_image(
                    wordmark_source, (int(target_h * ratio), target_h)
                )
                ctk.CTkLabel(title_row, image=self._wordmark_img, text="").pack(side="left")
                wordmark_packed = True
            except Exception:
                pass  # fall through to composed lockup

        if not wordmark_packed:
            # Fallback: brand icon + wordmark text + cyan version badge.
            icon_source = self._resolve_brand_source("icon.svg", "icon.png")
            if icon_source:
                try:
                    self._brand_icon_img = self.get_branded_image(icon_source, 42)
                    ctk.CTkLabel(title_row, image=self._brand_icon_img, text="").pack(
                        side="left", padx=(0, 12)
                    )
                except Exception:
                    pass
            ctk.CTkLabel(
                title_row, text="sovraan", font=("Segoe UI", 22, "bold"), text_color="#FFFFFF",
            ).pack(side="left")
            ctk.CTkLabel(
                title_row, text="2.0", font=("Segoe UI", 13, "bold"), text_color=COLOR_INFO,
            ).pack(side="left", padx=(8, 0), pady=(4, 0), anchor="n")

        ctk.CTkLabel(
            brand_col,
            text="YOUR DATA, YOUR FUTURE. LOCAL, VERIFIED AND IMMUTABLE.",
            font=("Segoe UI", 12),
            text_color="#9E9E9E",
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

    def log(self, msg):
        self.root.after(0, lambda: self._log_ui(msg))

    def _log_ui(self, msg):
        self.log_area.insert(tk.END, msg + "\n"); self.log_area.see(tk.END)

    def _enqueue_log(self, msg: str) -> None:
        """Thread-safe: buffer a log line for the main-thread drainer.

        Safe to call from any worker thread; the message is appended to the
        GUI only by :meth:`_process_log_queue`.
        """
        self._log_queue.put(msg)

    def _process_log_queue(self) -> None:
        """Drain buffered log lines onto the widget; reschedules every 100ms.

        Runs exclusively on the Tkinter main thread, batching all pending
        messages into a single widget update to avoid per-message churn.
        """
        try:
            lines: List[str] = []
            while True:
                try:
                    lines.append(self._log_queue.get_nowait())
                except queue.Empty:
                    break
            if lines:
                self.log_area.insert(tk.END, "\n".join(lines) + "\n")
                self.log_area.see(tk.END)
        finally:
            self.root.after(100, self._process_log_queue)

    def clear_log(self):
        self.log_area.delete(1.0, tk.END)

    def save_log(self):
        """Archive the activity log to a user-chosen file.

        Opens a native "Save As" file dialog (pre-filled with a dated default
        name) so the user can manually archive the current Activity Log to any
        location. Writing is skipped if the dialog is cancelled, and any I/O
        error is surfaced via a message box.
        """
        default_name = f"{time.strftime('%Y-%m-%d_%H%M%S')}_sovraan_Log.txt"
        # Default to a "logs" folder next to the app/executable for predictable,
        # path-relative archival (created lazily only if the user saves there).
        initial_dir = os.path.join(self._runtime_base(), "logs")
        try:
            os.makedirs(initial_dir, exist_ok=True)
        except OSError:
            initial_dir = self._runtime_base()
        path = filedialog.asksaveasfilename(
            title="Save Activity Log",
            defaultextension=".txt",
            initialdir=initial_dir,
            initialfile=default_name,
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.log_area.get(1.0, tk.END))
            self.log(f"Log saved: {path}")
            self._toast(f"Log saved: {os.path.basename(path)}", kind="success")
        except OSError as exc:
            traceback.print_exc()
            messagebox.showerror("Save Log", f"Could not save log:\n{exc}")

    @staticmethod
    def _human_size(num_bytes: float) -> str:
        """Format a byte count as a human-readable string (B…TB)."""
        size = float(num_bytes or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} TB"

    def _toast(self, message: str, kind: str = "info", duration: int = 3200) -> None:
        """Show a small, non-blocking toast near the window's bottom-right.

        Auto-dismisses after ``duration`` ms. Must be called on the main thread
        (dispatch via ``root.after`` from worker threads).

        Args:
            message: Text to display.
            kind: ``"info"``, ``"success"`` or ``"error"`` (accent colour).
            duration: Milliseconds before the toast disappears.
        """
        try:
            previous = getattr(self, "_toast_win", None)
            if previous is not None and previous.winfo_exists():
                previous.destroy()
        except tk.TclError:
            pass
        accent = {"error": COLOR_DANGER, "success": COLOR_SAFE}.get(kind, COLOR_INFO)
        try:
            win = tk.Toplevel(self.root)
            win.wm_overrideredirect(True)
            try:
                win.attributes("-topmost", True)
            except tk.TclError:
                pass
            frame = tk.Frame(win, bg="#1C1C1E", highlightbackground=accent, highlightthickness=1)
            frame.pack(fill="both", expand=True)
            tk.Label(
                frame, text=message, bg="#1C1C1E", fg="#EAEAEA",
                font=("Segoe UI", 12), padx=14, pady=8,
            ).pack()
            win.update_idletasks()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            x = rx + rw - win.winfo_width() - 24
            y = ry + rh - win.winfo_height() - 24
            win.wm_geometry(f"+{x}+{y}")
            self._toast_win = win
            self.root.after(duration, lambda w=win: w.winfo_exists() and w.destroy())
        except tk.TclError:
            pass

    def _set_header_status(self, text: str) -> None:
        """Update the header background-task indicator (empty string clears it)."""
        if hasattr(self, "header_status"):
            self.header_status.configure(text=text)

    def _show_rationalise_info(self) -> None:
        """Explain what Rationalisation does, in a small info pop-up."""
        messagebox.showinfo(
            "About Rationalisation",
            "Identifies and elevates Verified Golden Masters vs. legacy copies "
            "across your vault index, cryptographically linking legacy files to "
            "their source.\n\n"
            "This runs in the background — you remain free to use sovraan "
            "while it works.",
        )

    def progress(self, cur, tot, msg=""):
        self.root.after(0, lambda: self._progress_ui(cur, tot, msg))

    def _progress_ui(self, cur, tot, msg):
        if tot > 0: self.pbar.set(cur/tot)
        self.root.title(f"sovraan - {msg}")

    def _section(
        self,
        parent: Any,
        title: str,
        hint: Optional[str] = None,
        *,
        tight: bool = False,
    ) -> Any:
        """Create a titled "card" frame and return its content container.

        Provides consistent visual grouping: a bordered card, a bold header,
        and an optional muted hint line. Callers pack their controls into the
        returned frame.

        Args:
            parent: The widget to pack the card into.
            title: Section header text.
            hint: Optional muted one-line description shown under the header.
            tight: When True, use minimal vertical gaps (audit journey steps).

        Returns:
            A transparent content frame inside the card for the caller's widgets.
        """
        card = ctk.CTkFrame(parent, fg_color="#2A2A2A")
        card.pack(fill="x", padx=20, pady=(3, 0) if tight else (15, 0))
        ctk.CTkLabel(card, text=title, font=FONT_HEADER, anchor="w").pack(
            fill="x", padx=15, pady=(4, 1) if tight else (12, 2)
        )
        if hint:
            ctk.CTkLabel(
                card, text=hint, font=FONT_HINT, text_color=COLOR_HINT,
                anchor="w", justify="left",
            ).pack(fill="x", padx=15, pady=(0, 2) if tight else (0, 6))
        content = ctk.CTkFrame(card, fg_color="transparent")
        content.pack(fill="x", padx=15, pady=(0, 3) if tight else (0, 14))
        return content

    def _pick_merge_master(self) -> None:
        chosen = filedialog.askdirectory(title="Choose master folder")
        if chosen:
            self.m_master.set(chosen)
            config.set("merge_master", chosen)

    def _pick_merge_incoming(self) -> None:
        chosen = filedialog.askdirectory(title="Choose incoming folder")
        if chosen:
            self.m_inc.set(chosen)
            config.set("merge_incoming", chosen)

    def _pick_source_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose the folder to rescue")
        if chosen:
            self.src_var.set(chosen)
            config.set("last_source", chosen)

    def _pick_destination_vault(self) -> None:
        chosen = filedialog.askdirectory(title="Choose Destination Vault")
        if chosen:
            self.target_vault_dir.set(chosen)
            config.set("vault_path", chosen)
            self._sync_vault_path_display()

    def _require_destination_vault(self) -> Optional[str]:
        """Resolve Destination Vault for export; warn if unset before Begin Rescue."""
        raw = self.target_vault_dir.get().strip()
        if not raw:
            messagebox.showwarning(
                "Destination Vault Required",
                "Please select a Destination Vault in Step 1 before Begin Rescue.\n\n"
                "Obsidian export must not write into your source folder.",
            )
            return None
        try:
            root = Path(raw)
            root.mkdir(parents=True, exist_ok=True)
            return str(root.resolve())
        except OSError as exc:
            messagebox.showerror(
                "Destination Vault",
                f"Could not create or access the Destination Vault path:\n{exc}",
            )
            return None

    def _bind_drop_target(self, widget: Any) -> None:
        """Best-effort folder drag-and-drop on Windows; click-to-browse always works."""
        widget.bind("<Button-1>", lambda _e: self._pick_source_folder())
        try:
            import windnd  # type: ignore[import-untyped]

            def _on_drop(files: Any) -> None:
                for raw in files:
                    path = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                    path = path.strip("{}")
                    if os.path.isdir(path):
                        self.src_var.set(path)
                        config.set("last_source", path)
                        return

            windnd.hook_dropfiles(widget.winfo_toplevel(), func=_on_drop)
        except ImportError:
            pass

    def _populate_rescue_matrix(self, paths: Sequence[Path], *, max_cells: int = 24) -> None:
        """Render a compact grid of rescued master file names."""
        for cell in self._rescue_matrix_cells:
            cell.destroy()
        self._rescue_matrix_cells.clear()
        if not paths:
            empty = ctk.CTkLabel(
                self.f_rescue_matrix,
                text="Verified Golden Masters will appear here as ingestion runs.",
                font=FONT_CALM_SMALL,
                text_color=COLOR_HINT,
                wraplength=700,
            )
            empty.grid(row=0, column=0, columnspan=4, padx=4, pady=2, sticky="ew")
            self._rescue_matrix_cells.append(empty)
            return
        columns = 4
        for idx, path in enumerate(paths[:max_cells]):
            tile = ctk.CTkFrame(self.f_rescue_matrix, corner_radius=6, height=36)
            tile.grid(row=idx // columns, column=idx % columns, padx=3, pady=2, sticky="nsew")
            ctk.CTkLabel(
                tile,
                text=path.name,
                font=FONT_CALM_SMALL,
                wraplength=160,
                justify="center",
            ).pack(expand=True, padx=4, pady=4)
            self._rescue_matrix_cells.append(tile)
        for col in range(columns):
            self.f_rescue_matrix.columnconfigure(col, weight=1)

    def _calm_step_frame(
        self,
        parent: Any,
        step_num: int,
        title: str,
        subtitle: str,
        *,
        grid_row: int,
    ) -> ctk.CTkFrame:
        """Compact step card; minimal vertical gap between journey steps."""
        card = ctk.CTkFrame(parent, corner_radius=8, fg_color="#2A2A2A")
        card.grid(row=grid_row, column=0, sticky="ew", padx=4, pady=JOURNEY_PADY)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=8, pady=(2, 0))
        ctk.CTkLabel(
            head,
            text=f"Step {step_num}",
            font=FONT_CALM_SMALL,
            text_color=COLOR_INFO,
        ).pack(side="left")
        ctk.CTkLabel(
            head, text=title, font=FONT_CALM_STEP, anchor="w",
        ).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            card, text=subtitle, font=FONT_CALM_SMALL, text_color=COLOR_HINT,
            anchor="w", justify="left", wraplength=820,
        ).pack(fill="x", padx=8, pady=(0, JOURNEY_PADY))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=8, pady=(0, JOURNEY_PADY))
        return body

    def _calm_step_pack(
        self,
        parent: Any,
        step_num: int,
        title: str,
        subtitle: str,
    ) -> ctk.CTkFrame:
        """Pack-based step card for scrollable Calm Journey layouts."""
        card = ctk.CTkFrame(parent, corner_radius=8, fg_color="#2A2A2A")
        card.pack(fill="x", padx=4, pady=JOURNEY_PADY)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=8, pady=(2, 0))
        ctk.CTkLabel(
            head,
            text=f"Step {step_num}",
            font=FONT_CALM_SMALL,
            text_color=COLOR_INFO,
        ).pack(side="left")
        ctk.CTkLabel(
            head, text=title, font=FONT_CALM_STEP, anchor="w",
        ).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            card, text=subtitle, font=FONT_CALM_SMALL, text_color=COLOR_HINT,
            anchor="w", justify="left", wraplength=820,
        ).pack(fill="x", padx=8, pady=(0, JOURNEY_PADY))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=8, pady=(0, JOURNEY_PADY))
        return body

    def _collision_policy_label(self) -> str:
        stored = str(config.get("collision_policy", "skip")).lower()
        return COLLISION_CONFIG_TO_LABEL.get(stored, "Skip (Safe)")

    def _hashing_depth_label(self) -> str:
        stored = str(config.get("hashing_depth", "quick")).lower()
        return HASHING_CONFIG_TO_LABEL.get(stored, "Quick Check")

    def _apply_collision_policy(self, label: str) -> None:
        config.set("collision_policy", COLLISION_LABEL_TO_CONFIG.get(label, "skip"))

    def _apply_hashing_depth(self, label: str) -> None:
        depth = HASHING_LABEL_TO_CONFIG.get(label, "quick")
        config.set("hashing_depth", depth)
        if label == "Deep Cryptographic":
            config.set("scan_mode", "Deep Cryptographic")
            self.mode_var.set("Exact")
        else:
            config.set("scan_mode", "Exact Match (Fast)")
            self.mode_var.set("Exact")

    def _sync_vault_path_display(self) -> None:
        path = (config.get("vault_path") or config.get("last_dest") or self.target_vault_dir.get() or "").strip()
        if not path:
            path = self._initial_vault_path()
        self.target_vault_dir.set(path)
        if hasattr(self, "lbl_vault_destination"):
            self.lbl_vault_destination.configure(text=path)

    def _sync_simulate_only_config(self) -> None:
        """Persist simulate-only toggle for the one-click rescue pipeline."""
        val = self.simulate_only_var.get()
        config.set("simulate_only", val)

    def _simulate_only_from_config(self) -> bool:
        return bool(config.get("simulate_only", False))

    def _on_ai_model_selected(self, choice: str) -> None:
        print(f"[Pro Studio AI] Model selected: {choice}")

    def _on_ai_truthfulness_changed(self, value: float) -> None:
        if hasattr(self, "lbl_ai_truthfulness_value"):
            self.lbl_ai_truthfulness_value.configure(text=f"{float(value):.2f}")
        print(f"[Pro Studio AI] Truthfulness threshold: {float(value):.2f}")

    def _on_sync_ai_index(self):
        import os, threading
        vault_path = self.vault_entry.get()
        if not vault_path or not os.path.exists(vault_path):
            self.chat_display.configure(state="normal")
            self.chat_display.insert("end", "[ERROR] Please set a valid Vault Destination on Your Journey.\n")
            self.chat_display.configure(state="disabled")
            return

        self.chat_display.configure(state="normal")
        self.chat_display.insert("end", f"[SYSTEM] Initializing Sovereign Vector Engine at {vault_path}...\n")
        self.chat_display.configure(state="disabled")

        def ingestion_task():
            try:
                engine = SovereignVectorEngine(vault_path)
                self.vector_engine = engine
                def safe_update(msg):
                    self.chat_display.configure(state="normal")
                    self.chat_display.insert("end", msg + "\n")
                    self.chat_display.see("end")
                    self.chat_display.configure(state="disabled")
                
                engine.sync_index(progress_callback=lambda msg: self.root.after(0, safe_update, msg))
                self.root.after(0, safe_update, "[SUCCESS] Vault successfully indexed!")
            except Exception as e:
                self.root.after(0, safe_update, f"[ERROR] Indexing failed: {str(e)}\n")

        threading.Thread(target=ingestion_task, daemon=True).start()

    def start_indexing(self) -> None:
        """Start the vault indexing process on a background thread to prevent UI freezing."""
        directory_path = self.target_vault_dir.get()
        if not directory_path:
            directory_path = self._initial_vault_path()

        try:
            indexer = SovereignVectorEngine(directory_path)
            self.vector_engine = indexer
        except Exception as exc:
            self.log(f"[AI Indexer] Error initializing indexer: {exc}")
            messagebox.showerror("AI Indexer Error", f"Failed to initialize SovereignVectorEngine:\n{exc}")
            return

        def update_progress(progress_msg: str) -> None:
            # Update the placeholder label inside the CTkScrollableFrame on the main thread
            self.root.after(0, lambda: self.lbl_ai_source_placeholder.configure(text=progress_msg))
            # Enqueue to background log notes
            self._enqueue_log(f"[AI Indexer] {progress_msg}")

        # Spawn a background thread to run sync_index
        indexing_thread = threading.Thread(
            target=indexer.sync_index,
            args=(update_progress,),
            daemon=True
        )
        indexing_thread.start()
        self.log(f"[AI Indexer] Spawning indexing thread for vault path: {directory_path}")

    def sanitize_filename(self, filename):
        import re, os
        name, ext = os.path.splitext(filename)
        # Lowercase, replace spaces and hyphens with underscores, strip weird chars
        clean_name = re.sub(r'[^a-z0-9_]', '', re.sub(r'[\s\-]+', '_', name.lower()))
        return f"{clean_name}{ext.lower()}"

    @staticmethod
    def _open_path_in_explorer(target: Union[str, Path]) -> None:
        """Open a folder in the system file manager (post-elevation reward)."""
        path = str(Path(target).resolve())
        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(path)  # type: ignore[attr-defined]
            elif system == "Darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except Exception as exc:
            raise OSError(str(exc)) from exc

    def _elevation_paths_from_config(self) -> Tuple[str, str]:
        source = (config.get("last_source") or "").strip()
        vault = (config.get("vault_path") or config.get("last_dest") or "").strip()
        return source, vault

    def _validate_elevation_paths(self, source: str, vault: str) -> bool:
        """Validate source and vault paths; log failures, alert only on critical I/O."""
        if not source:
            self.log("Elevation aborted: no source folder configured (Select Source).")
            return False
        if not Path(source).is_dir():
            self.log(f"Elevation aborted: source is not a directory: {source}")
            return False
        if not vault:
            self.log("Elevation aborted: no vault destination configured.")
            return False
        try:
            vault_root = Path(vault)
            vault_root.mkdir(parents=True, exist_ok=True)
            shutil.disk_usage(vault_root.resolve())
        except OSError as exc:
            self.log(f"Elevation aborted: vault destination unavailable — {exc}")
            err = str(exc).lower()
            if any(token in err for token in ("not ready", "device", "no such", "denied", "disconnected")):
                self.root.after(
                    0,
                    lambda e=exc: messagebox.showerror(
                        "Storage Unavailable",
                        f"The destination drive or path is not accessible:\n{e}",
                    ),
                )
            return False
        return True

    def _auditor_class_from_config(self, hashing_depth: str) -> type:
        if hashing_depth == "deep":
            return FileAuditor
        scan_mode = str(config.get("scan_mode", "")).lower()
        if "visual" in scan_mode or "video" in scan_mode:
            return VideoFileAuditor
        return FileAuditor

    def _set_elevation_status(self, text: str) -> None:
        if hasattr(self, "lbl_rescue_progress"):
            self.root.after(0, lambda t=text: self.lbl_rescue_progress.configure(text=t))

    def _ensure_file_index_status_column(self) -> None:
        with self.db_manager.conn:
            cur = self.db_manager.conn.cursor()
            cur.execute("PRAGMA table_info(file_index)")
            columns = [info[1] for info in cur.fetchall()]
            if "status" not in columns:
                self.db_manager.conn.execute(
                    "ALTER TABLE file_index ADD COLUMN status TEXT DEFAULT 'active'"
                )

    def _fetch_session_legacy_records(self, session_id: str) -> List[Tuple[Any, ...]]:
        sql = (
            "SELECT rowid, full_path, file_size, modified_time FROM file_index "
            "WHERE is_golden = 0 AND last_session_id = ? "
            "AND (status != 'archived' OR status IS NULL)"
        )
        with self.db_manager.conn:
            cur = self.db_manager.conn.cursor()
            cur.execute(sql, (session_id,))
            return cur.fetchall()

    def _fetch_session_golden_records(self, session_id: str) -> List[Tuple[Any, ...]]:
        sql = (
            "SELECT rowid, full_path, file_size, modified_time FROM file_index "
            "WHERE is_golden = 1 AND last_session_id = ? AND sha256_hash IS NOT NULL"
        )
        with self.db_manager.conn:
            cur = self.db_manager.conn.cursor()
            cur.execute(sql, (session_id,))
            return cur.fetchall()

    def _write_vault_asset_index_only(
        self,
        vault_source_root: Path,
        source_label: str,
        asset_relpaths: Sequence[str],
    ) -> Path:
        """Write only ``_Index_[Source].md`` — no per-file Markdown sidecars."""
        vault_source_root.mkdir(parents=True, exist_ok=True)
        index_path = vault_source_root / f"_Index_{source_label}.md"
        unique_paths = sorted(set(asset_relpaths))
        lines = "\n".join(f"- `{rel}`" for rel in unique_paths)
        body = (
            f"# Map of Content: {source_label}\n\n"
            "Vaulted binary assets (original extensions preserved):\n\n"
            f"{lines}\n"
        )
        index_path.write_text(body, encoding="utf-8")
        return index_path

    def _resolve_vault_asset_dest(
        self,
        src_path: Path,
        vault_source_root: Path,
        reserved: Optional[set] = None,
        collision_policy: str = "skip",
    ) -> Optional[Path]:
        if reserved is None:
            reserved = set()
        year_dir = vault_source_root / _obsidian_year_bucket(
            UniqueFileRecord(path=src_path, file_hash=""),
        )
        return self._resolve_vault_dest(src_path, year_dir, reserved, collision_policy, vault_source_root)

    def _resolve_vault_dest(
        self,
        src_path: Path,
        dest_dir: Path,
        reserved: set,
        collision_policy: str,
        vault_source_root: Optional[Path] = None,
    ) -> Optional[Path]:
        filename = os.path.basename(str(src_path))
        clean_name = self.sanitize_filename(filename)
        dest_file = dest_dir / clean_name
        policy = (collision_policy or "skip").lower()
        if policy == "skip":
            if dest_file.exists() or str(dest_file) in reserved:
                return None
            return dest_file
        if policy == "overwrite":
            return dest_file
        counter = 2
        if vault_source_root is None:
            isolation_path = dest_dir
        else:
            isolation_path = Path(os.path.join(vault_source_root, "Isolated_Legacy_Copies", clean_name))
        
        name_stem, name_ext = os.path.splitext(clean_name)
        while dest_file.exists() or str(dest_file) in reserved:
            dest_file = isolation_path / f"{name_stem}_v{counter}{name_ext}"
            counter += 1
        return dest_file

    def _write_archive_manifest(self, report_path: Path, csv_entries: List[List[Any]]) -> None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "Status", "Filename", "Original Path", "New Path", "Modified Date",
                "File Size (MB)", "Transaction ID", "Session ID",
            ])
            writer.writerows(csv_entries)

    def _build_manifest_rows(
        self,
        records: Sequence[Tuple[Any, ...]],
        isolation_path: Path,
        collision_policy: str,
        session_id: str,
        transaction_id: str,
        *,
        status_planned: str,
    ) -> Tuple[List[List[Any]], set]:
        csv_entries: List[List[Any]] = []
        reserved: set = set()
        for _rowid, fp_str, size, mtime in records:
            src_path = Path(fp_str)
            if not src_path.exists():
                continue
            dest_file = self._resolve_vault_dest(src_path, isolation_path, reserved, collision_policy)
            if dest_file is None:
                status = "SKIPPED"
                dest_display = ""
            else:
                status = status_planned
                reserved.add(str(dest_file))
                dest_display = str(dest_file)
            try:
                mtime_val = mtime if mtime else os.path.getmtime(src_path)
            except OSError:
                mtime_val = time.time()
            size_mb = f"{(size or 0) / (1024 * 1024):.2f}"
            csv_entries.append([
                status,
                src_path.name,
                str(src_path),
                dest_display,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime_val)),
                size_mb,
                transaction_id,
                session_id,
            ])
        return csv_entries, reserved

    def _run_elevation_pipeline(self, storage: Optional[StoragePort] = None) -> None:
        """One-click PLAN → optional EXECUTE pipeline (no blocking dialogs)."""
        if storage is None:
            storage = LocalDiskStorage()
        source_path, vault_path = self._elevation_paths_from_config()
        collision_policy = str(config.get("collision_policy", "skip")).lower()
        hashing_depth = str(config.get("hashing_depth", "quick")).lower()
        simulate_only = self._simulate_only_from_config()

        try:
            self._set_elevation_status("Validating paths…")
            if not self._validate_elevation_paths(source_path, vault_path):
                return

            self.log("—— Calm Journey rescue pipeline started ——")
            self.log(f"Source: {source_path}")
            self.log(f"Vault: {vault_path}")
            self.log(
                f"Policy: collision={collision_policy}, hashing={hashing_depth}, "
                f"simulate_only={simulate_only}"
            )

            self._set_elevation_status("Planning elevation (audit)…")
            cls = self._auditor_class_from_config(hashing_depth)
            ignore_exts = self._split_csv_setting("ignore_exts")
            ignore_folders = self._split_csv_setting("ignore_folders")
            auditor = cls(
                source_path,
                log_callback=self.log,
                progress_callback=self.progress,
                review_mode=False,
                stop_event=self.stop_event,
                pause_event=self.pause_event,
                threshold=config.get("threshold", 0),
                threads=config.get("threads", 4),
                ignore_exts=ignore_exts,
                ignore_folders=ignore_folders,
                db_manager=self.db_manager,
                session_id=self.current_session_id,
                license_key=config.get("proLicenseKey"),
            )
            auditor.run()
            self.db_manager.identify_golden_versions(session_id=self.current_session_id)

            if bool(config.get("notarise", self.notarise_var.get())):
                try:
                    notary = DedupNotary(self.db_path)
                    threading.Thread(
                        target=notary.batch_submit_unnotarised, daemon=True,
                    ).start()
                except Exception as exc:
                    self.log(f"Notarisation background task failed to start: {exc}")
            else:
                self.log("Notarisation skipped (disabled in Expert Studio).")

            self._ensure_file_index_status_column()
            source_label = _obsidian_source_folder_label(source_path)
            vault_source_root = storage.join_paths(vault_path, source_label)
            target_vault_dir = vault_source_root
            records = self._fetch_session_legacy_records(self.current_session_id)
            session_timestamp = time.strftime("%Y%m%d_%H%M%S")
            transaction_id = session_timestamp
            vault_root = storage.join_paths(vault_path)
            isolation_path = Path(storage.join_paths(vault_root, "Isolated_Legacy_Copies", f"Session_{session_timestamp}"))
            report_path = Path(storage.join_paths(vault_root, f"Archive_Manifest_{session_timestamp}.csv"))

            plan_rows, _reserved = self._build_manifest_rows(
                records,
                isolation_path,
                collision_policy,
                self.current_session_id,
                transaction_id,
                status_planned="PLANNED",
            )
            self._write_archive_manifest(report_path, plan_rows)
            self.log(f"Cryptographic manifest written: {report_path.resolve()}")
            self.log(f"Plan catalogued {len(plan_rows)} legacy record(s) for vault isolation.")
            self._set_elevation_status(
                f"Plan complete — {len(plan_rows)} record(s) in manifest.",
            )

            if simulate_only:
                self.log(
                    "Simulate Only is enabled in The Vault Index — file transfer skipped. "
                    "Manifest catalogued; originals remain untouched.",
                )
                self._set_elevation_status("Elevation Complete (Simulate Only)")
                self.root.after(0, self.update_datamine_stats)
                return

            golden_records = self._fetch_session_golden_records(self.current_session_id)
            if not golden_records:
                self.log("No golden masters to vault for this session; elevation finished.")
                self._set_elevation_status("Elevation Complete")
                try:
                    index_path = self._write_vault_asset_index_only(Path(vault_source_root), source_label, [])
                    self.log(f"Vault index written: {index_path.resolve()}")
                except OSError as exc:
                    self.log(f"Could not write vault index: {exc}")
                try:
                    self._open_path_in_explorer(Path(vault_root))
                    self.log(f"Opened vault destination: {vault_root}")
                except OSError as exc:
                    self.log(f"Could not open vault folder: {exc}")
                self.root.after(0, self.update_datamine_stats)
                return

            total_size = sum(r[2] or 0 for r in golden_records)
            try:
                free_space = storage.get_free_space(vault_root)
            except Exception as exc:
                self.log(f"Elevation aborted: cannot read vault disk space — {exc}")
                self.root.after(
                    0,
                    lambda e=exc: messagebox.showerror(
                        "Storage Unavailable",
                        f"Cannot access the vault volume:\n{e}",
                    ),
                )
                return
            if free_space < total_size:
                req_gb = total_size / (1024 ** 3)
                free_gb = free_space / (1024 ** 3)
                self.log(
                    f"Elevation aborted: insufficient disk space "
                    f"(need {req_gb:.2f} GB, have {free_gb:.2f} GB).",
                )
                self.root.after(
                    0,
                    lambda: messagebox.showerror(
                        "Insufficient Disk Space",
                        f"Required: {req_gb:.2f} GB\nAvailable: {free_gb:.2f} GB",
                    ),
                )
                return

            self._set_elevation_status("Executing non-destructive vault copy…")
            storage.makedirs(vault_source_root)
            csv_entries: List[List[Any]] = []
            reserved_exec: set = set()
            copied_count = 0
            copied_size = 0
            aborted = False
            vaulted_relpaths: List[str] = []
            elevation_records: List[ElevationRecord] = []

            self.log(f"[PIPELINE] Starting with {len(golden_records)} golden records.")

            for i, (rowid, fp_str, size, mtime) in enumerate(golden_records):
                if self.stop_event.is_set():
                    aborted = True
                    self.log("Aborted by User.")
                    break

                try:
                    # --- LOCK: Derive and freeze target_path. Nothing below may alter it. ---
                    raw_name      = storage.get_basename(fp_str)
                    clean_base    = self.sanitize_filename(raw_name)
                    
                    try:
                        file_mtime = mtime if mtime else storage.get_mtime(fp_str)
                    except Exception:
                        file_mtime = time.time()
                        
                    date_prefix   = datetime.fromtimestamp(file_mtime).strftime('%Y-%m-%d')
                    year_folder   = datetime.fromtimestamp(file_mtime).strftime('%Y')
                    final_name    = f"{date_prefix}_{clean_base}"

                    # Insert the [YYYY] subdirectory as per your architecture requirement
                    target_path   = storage.join_paths(target_vault_dir, year_folder, final_name)
                    # target_path is now a plain immutable str. Do not reassign it.

                    # Instantiate record in PENDING state
                    record = ElevationRecord(
                        src_path=fp_str,
                        target_path=target_path,
                        stage=ElevationStage.PENDING
                    )
                    elevation_records.append(record)

                    # --- CHECK: Filesystem existence only. DB state is irrelevant here. ---
                    if storage.exists(target_path):
                        self.log(f"[SKIP-EXISTS] {target_path}")
                        csv_entries.append([
                            "SKIPPED",
                            raw_name,
                            fp_str,
                            target_path,
                            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(file_mtime)),
                            f"{(size or 0) / (1024*1024):.2f}",
                            transaction_id,
                            self.current_session_id,
                        ])
                        record.stage = ElevationStage.COMPLETE
                        continue

                    self.log(
                        f"[PIPELINE] [{i+1}/{len(golden_records)}] "
                        f"src={raw_name} -> target={target_path}"
                    )

                    # Sidecar markdown
                    sidecar_content = (
                        "---\n"
                        f"original_filename: {raw_name}\n"
                        f"vaulted_name: {storage.get_basename(target_path)}\n"
                        "---\n"
                        f"# {raw_name}\n\n"
                        "Vaulted binary asset.\n"
                    )

                    # Drive the State Machine transitions
                    transition_elevation_file(
                        record=record,
                        storage=storage,
                        target_path=target_path,
                        sidecar_content=sidecar_content
                    )

                    # Manifest relative paths
                    rel = storage.get_relative_posix_path(target_path, vault_source_root)
                    vaulted_relpaths.append(rel)

                    size_mb = f"{(size or 0) / (1024*1024):.2f}"
                    csv_entries.append([
                        "COPIED",
                        raw_name,
                        fp_str,
                        target_path,
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(file_mtime)),
                        size_mb,
                        transaction_id,
                        self.current_session_id,
                    ])
                    copied_count += 1
                    copied_size  += (size or 0)

                    if copied_count % 500 == 0:
                        self.log(f"Vault copy: {copied_count} file(s) copied…")

                    self.progress(i + 1, len(golden_records), f"Copying {raw_name}")

                except Exception as e:
                    self.log(f"[ERROR] Failed to copy {fp_str}: {type(e).__name__}: {e}")
                    continue

            self.log(
                f"[PIPELINE] Complete. copied={copied_count}, "
                f"skipped={sum(1 for e in csv_entries if e[0]=='SKIPPED')}, "
                f"total_records={len(golden_records)}"
            )

            if csv_entries:
                self._write_archive_manifest(report_path, csv_entries)

            if elevation_records:
                try:
                    manifest_path = Path(storage.join_paths(vault_root, f"run_manifest_{session_timestamp}.json"))
                    manifest_json = serialize_records(elevation_records)
                    storage.write_text(str(manifest_path), manifest_json)
                    self.log(f"JSON run manifest written: {manifest_path.resolve()}")
                except Exception as exc:
                    self.log(f"Failed to write run manifest: {exc}")

            if aborted:
                self._set_elevation_status("Elevation aborted")
                self.root.after(0, self.update_datamine_stats)
                return

            try:
                index_path = self._write_vault_asset_index_only(
                    Path(vault_source_root), source_label, vaulted_relpaths,
                )
                self.log(f"Vault index written: {index_path.resolve()}")
            except OSError as exc:
                self.log(f"Could not write vault index: {exc}")

            self.log(
                f"Elevation succeeded: {copied_count} binary asset(s) copied "
                f"({copied_size / (1024 ** 3):.2f} GB). Manifest: {report_path.resolve()}"
            )
            self.log("Original source files remain untouched (non-destructive copy2).")
            self._set_elevation_status("Elevation Complete")
            try:
                self._open_path_in_explorer(Path(vault_root))
                self.log(f"Opened vault destination: {vault_root}")
            except OSError as exc:
                self.log(f"Could not open vault folder: {exc}")
            self.root.after(0, self.update_datamine_stats)
        except FreemiumLimitExceeded as exc:
            self.log(str(exc))
            self._set_elevation_status("Rescue stopped — Pro license required.")
        except Exception as exc:
            self.log(f"Elevation pipeline error: {exc}")
            self._set_elevation_status("Elevation failed — see Background notes.")
        finally:
            self.root.after(0, self.reset_scan_buttons)

    def _journey_export_mode_value(self) -> str:
        """Map Calm Journey UI label to ``db_ingest`` export profile."""
        if self.journey_export_mode.get() == "Intelligence Mode":
            return "obsidian"
        return "standard"

    def _on_journey_export_mode(self, value: str) -> None:
        self.journey_export_mode.set(value)
        config.set("journey_export_mode", value)

    def _init_your_journey_tab(self) -> None:
        """Calm Journey: Map the Swamp → Secure the Gold → Ignite Your Mind."""
        self._rescue_matrix_cells: list[Any] = []
        self._init_header(self.t_journey)

        master_frame = ctk.CTkFrame(self.t_journey, fg_color="transparent")
        master_frame.pack(fill="both", expand=True, padx=10, pady=10)

        action_frame = ctk.CTkFrame(master_frame, fg_color="transparent")
        action_frame.pack(side="bottom", fill="x", pady=(10, 0))

        button_container = ctk.CTkFrame(action_frame, fg_color="transparent")
        button_container.pack(anchor="center", pady=(5, 5))

        self.btn_start = ctk.CTkButton(
            button_container,
            text="Begin Rescue",
            image=self.icons["play"],
            compound="left",
            fg_color=COLOR_INFO,
            hover_color=COLOR_INFO_HOVER,
            text_color=COLOR_ON_INFO,
            command=self.start_audit,
            width=200,
            height=40,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
        )
        self.btn_start.pack(side="left", padx=10)

        self.btn_pause = ctk.CTkButton(
            button_container, text="Pause", image=self.icons["pause"], compound="left",
            fg_color=COLOR_CAUTION, hover_color=COLOR_CAUTION_HOVER,
            command=self.toggle_pause, state="disabled", width=150, height=40,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
        )
        self.btn_pause.pack(side="left", padx=10)

        self.btn_stop = ctk.CTkButton(
            button_container, text="Stop", image=self.icons["stop"], compound="left",
            fg_color=COLOR_DANGER, hover_color=COLOR_DANGER_HOVER,
            command=self.stop_scan, state="disabled", width=150, height=40,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
        )
        self.btn_stop.pack(side="left", padx=10)

        content_frame = ctk.CTkScrollableFrame(master_frame, fg_color="transparent")
        content_frame.pack(side="top", fill="both", expand=True)

        step1 = self._calm_step_pack(
            content_frame,
            1,
            CALM_STEP1_TITLE,
            "Select the folder to audit and your Obsidian vault destination.",
        )
        ctk.CTkLabel(
            step1, text="Select Source", font=FONT_CALM_STEP, anchor="w",
        ).pack(fill="x", pady=(2, 2))
        ctk.CTkLabel(
            step1,
            text="Point sovraan at the folder to ingest. Golden Files are discovered locally — no cloud uploads.",
            font=ctk.CTkFont(size=13),
            text_color="gray75",
            anchor="w",
            justify="left",
            wraplength=820,
        ).pack(fill="x", pady=(0, 4))

        self.src_var = tk.StringVar(value=config.get("last_source", ""))
        self.drop_zone = ctk.CTkFrame(
            step1, height=54, corner_radius=14,
            border_width=2, border_color=COLOR_NEUTRAL,
            fg_color="#2A2A2A",
        )
        self.drop_zone.pack(fill="x", pady=(0, 4))
        self.drop_zone.pack_propagate(False)
        ctk.CTkLabel(
            self.drop_zone,
            text="Click or drag a folder here",
            font=FONT_CALM_BODY,
        ).pack(expand=True)
        ctk.CTkLabel(
            self.drop_zone,
            text="Choose the folder to elevate into your vault",
            font=ctk.CTkFont(size=13),
            text_color="gray75",
        ).pack(pady=(0, 2))
        self._bind_drop_target(self.drop_zone)

        path_row = ctk.CTkFrame(step1, fg_color="transparent")
        path_row.pack(fill="x", pady=(0, 8))
        ctk.CTkEntry(
            path_row, textvariable=self.src_var,
            placeholder_text="Source folder path…",
            height=32, font=("Consolas", 12),
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(
            path_row, text="Browse", image=self.icons["folder"], compound="left",
            width=100, height=32, command=self._pick_source_folder,
        ).pack(side="left")

        ctk.CTkLabel(
            step1, text="Vault Destination", font=FONT_CALM_STEP, anchor="w",
        ).pack(fill="x", pady=(2, 2))
        vault_card = ctk.CTkFrame(step1, corner_radius=10, fg_color="#2A2A2A")
        vault_card.pack(fill="x", pady=(0, 4))
        vault_inner = ctk.CTkFrame(vault_card, fg_color="transparent")
        vault_inner.pack(fill="x", padx=12, pady=4)
        self.lbl_vault_destination = ctk.CTkLabel(
            vault_inner,
            text=self._initial_vault_path(),
            font=("Consolas", 12),
            text_color="#9E9E9E",
            anchor="w",
            justify="left",
            wraplength=760,
        )
        self.lbl_vault_destination.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            vault_inner, text="Change", image=self.icons["folder"], compound="left",
            width=100, height=32, command=self._pick_destination_vault,
        ).pack(side="right", padx=(8, 0))
        self._sync_vault_path_display()

        step2 = self._calm_step_pack(
            content_frame,
            2,
            CALM_STEP2_TITLE,
            "Live rescue status and verified Golden Masters discovered during the audit.",
        )

        self.lbl_rescue_progress = ctk.CTkLabel(
            step2,
            text="Ready to rescue Golden Files into your vault.",
            font=ctk.CTkFont(size=13),
            text_color=COLOR_INFO,
            anchor="w",
            justify="left",
            wraplength=820,
        )
        self.lbl_rescue_progress.pack(fill="x", pady=(2, 2))
        self.f_rescue_matrix = ctk.CTkFrame(step2, fg_color="transparent", height=28)
        self.f_rescue_matrix.pack(fill="x", pady=(0, 4))
        self.f_rescue_matrix.pack_propagate(False)
        self._populate_rescue_matrix([])

        step3 = self._calm_step_pack(
            content_frame,
            3,
            CALM_STEP3_TITLE,
            "Choose export profile, then start the rescue. Intelligence Mode adds Markdown sidecars for Obsidian.",
        )
        ctk.CTkLabel(
            step3, text="Export profile", font=FONT_CALM_BODY, anchor="w",
        ).pack(fill="x", pady=(0, 4))
        self.journey_mode_selector = ctk.CTkSegmentedButton(
            step3,
            values=["Standard Mode", "Intelligence Mode"],
            variable=self.journey_export_mode,
            command=self._on_journey_export_mode,
            selected_color=COLOR_INFO,
            selected_hover_color=COLOR_INFO_HOVER,
            unselected_color="#2A2A2A",
        )
        self.journey_mode_selector.pack(anchor="w", pady=(0, 6))

    def _init_merge_folders_tab(self) -> None:
        """Consolidate incoming folders into a master tree."""
        scroll = ctk.CTkScrollableFrame(self.t_merge, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=10, pady=10)
        self._init_merge_section(scroll)

    def _init_expert_studio_tab(self) -> None:
        """Advanced scanner settings and semantic vault chat (Intelligence Mode companion)."""
        master_frame = ctk.CTkFrame(self.t_expert, fg_color="transparent")
        master_frame.pack(fill="both", expand=True, padx=10, pady=10)

        config_scroll = ctk.CTkScrollableFrame(master_frame, fg_color="transparent", height=220)
        config_scroll.pack(side="top", fill="x", pady=(0, 8))

        scan_sec = self._section(
            config_scroll,
            "Scanner settings",
            hint="Tune hashing depth, duplicate review, and notarisation for Expert workflows.",
        )
        depth_row = ctk.CTkFrame(scan_sec, fg_color="transparent")
        depth_row.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(depth_row, text="Hashing depth:", font=FONT_BODY).pack(side="left", padx=(0, 8))
        self.hashing_depth_var = tk.StringVar(value=self._hashing_depth_label())
        ctk.CTkSegmentedButton(
            depth_row,
            values=list(HASHING_DEPTH_LABELS),
            variable=self.hashing_depth_var,
            command=self._apply_hashing_depth,
            selected_color=COLOR_INFO,
            selected_hover_color=COLOR_INFO_HOVER,
        ).pack(side="left")

        toggles = ctk.CTkFrame(scan_sec, fg_color="transparent")
        toggles.pack(fill="x", pady=(0, 4))
        ctk.CTkCheckBox(
            toggles,
            text="Review duplicates before vaulting",
            variable=self.review_var,
            font=FONT_BODY,
            command=lambda: config.set("review_duplicates", self.review_var.get()),
        ).pack(anchor="w", pady=2)
        ctk.CTkCheckBox(
            toggles,
            text="Notarise hashes (OpenTimestamps background worker)",
            variable=self.notarise_var,
            font=FONT_BODY,
            command=lambda: config.set("notarise", self.notarise_var.get()),
        ).pack(anchor="w", pady=2)

        self._advanced_visible = False
        self.btn_adv_toggle = ctk.CTkButton(
            config_scroll,
            text="▶ Advanced AI settings",
            fg_color=COLOR_NEUTRAL,
            hover_color=COLOR_NEUTRAL_HOVER,
            command=self.toggle_advanced_settings,
            width=200,
        )
        self.btn_adv_toggle.pack(anchor="w", padx=5, pady=5)

        self.advanced_settings_frame = ctk.CTkFrame(config_scroll, fg_color="transparent")
        self.ai_model_var = tk.StringVar(value="CPU-Native Embedded")
        ctk.CTkOptionMenu(
            self.advanced_settings_frame,
            values=["CPU-Native Embedded", "Local LLM"],
            variable=self.ai_model_var,
            width=180,
        ).pack(side="left", padx=5)
        ctk.CTkLabel(self.advanced_settings_frame, text="Truthfulness:").pack(side="left", padx=5)
        self.ai_truthfulness_var = tk.DoubleVar(value=0.5)
        ctk.CTkSlider(
            self.advanced_settings_frame,
            from_=0,
            to=1,
            variable=self.ai_truthfulness_var,
            width=150,
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            self.advanced_settings_frame,
            text="Sync AI Index",
            command=self._on_sync_ai_index,
            width=120,
            fg_color=COLOR_INFO,
            hover_color=COLOR_INFO_HOVER,
            text_color=COLOR_ON_INFO,
        ).pack(side="left", padx=5)

        chat_label = ctk.CTkLabel(
            master_frame,
            text="Vault Chat — ask questions about your indexed vault",
            font=FONT_HEADER,
            anchor="w",
        )
        chat_label.pack(fill="x", pady=(4, 4))

        control_frame = ctk.CTkFrame(master_frame, fg_color="transparent")
        control_frame.pack(side="top", fill="x", pady=(0, 6))
        ctk.CTkButton(
            control_frame,
            text="Boot AI Engine",
            image=self.icons.get("play"),
            compound="left",
            fg_color=COLOR_SAFE,
            hover_color=COLOR_SAFE_HOVER,
            command=self._boot_ai_engine,
            height=36,
        ).pack(side="left", padx=(0, 10))
        ctk.CTkButton(
            control_frame,
            text="Kill AI Engine",
            image=self.icons.get("stop"),
            compound="left",
            fg_color=COLOR_DANGER,
            hover_color=COLOR_DANGER_HOVER,
            command=self._kill_ai_engine,
            height=36,
        ).pack(side="left")

        self.chat_display = ctk.CTkTextbox(
            master_frame,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color="#0D0D11",
            text_color="#FFFFFF",
            border_color=COLOR_NEUTRAL,
            border_width=1,
        )
        self.chat_display.pack(side="top", fill="both", expand=True, pady=(0, 8))
        self.chat_display.configure(state="disabled")

        input_frame = ctk.CTkFrame(master_frame, fg_color="transparent")
        input_frame.pack(side="bottom", fill="x")
        self.chat_input = ctk.CTkEntry(
            input_frame,
            placeholder_text="Ask your vault a question...",
            font=FONT_BODY,
            height=40,
        )
        self.chat_input.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.chat_input.bind("<Return>", lambda _e: self._send_message())
        ctk.CTkButton(
            input_frame,
            text="Send",
            image=self.icons.get("arrow"),
            compound="left",
            fg_color=COLOR_INFO,
            hover_color=COLOR_INFO_HOVER,
            text_color=COLOR_ON_INFO,
            command=self._send_message,
            width=100,
            height=40,
        ).pack(side="right")

    def _init_vault_index_tab(self) -> None:
        """Ledger summary, vault commit controls, and maintenance."""
        scroll = ctk.CTkScrollableFrame(self.t_vault_index, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=10, pady=10)
        self._init_datamine_section(scroll)

    def toggle_advanced_settings(self) -> None:
        if self._advanced_visible:
            self.advanced_settings_frame.pack_forget()
            self.btn_adv_toggle.configure(text="▶ Advanced AI settings")
            self._advanced_visible = False
        else:
            self.advanced_settings_frame.pack(side="top", fill="x", pady=5)
            self.btn_adv_toggle.configure(text="▼ Advanced AI settings")
            self._advanced_visible = True

    def _init_merge_section(self, parent: Any) -> None:
        self.m_master = tk.StringVar(value=config.get("merge_master", ""))
        self.m_inc = tk.StringVar(value=config.get("merge_incoming", ""))

        sec = self._section(
            parent, "Merge folders",
            hint="Consolidate an incoming folder into a master folder, skipping "
                 "files that already exist there.",
        )

        ctk.CTkLabel(sec, text="Master folder (destination):", font=FONT_BODY).pack(
            anchor="w", pady=(0, 2)
        )
        master_row = ctk.CTkFrame(sec, fg_color="transparent")
        master_row.pack(fill="x", pady=(0, 10))
        ctk.CTkEntry(
            master_row, textvariable=self.m_master,
            placeholder_text="Folder that will be kept and added to…",
        ).pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(
            master_row, text="Browse", image=self.icons['folder'], compound="left", width=110,
            command=self._pick_merge_master,
        ).pack(side="left")

        ctk.CTkLabel(sec, text="Incoming folder (source):", font=FONT_BODY).pack(
            anchor="w", pady=(0, 2)
        )
        inc_row = ctk.CTkFrame(sec, fg_color="transparent")
        inc_row.pack(fill="x", pady=(0, 10))
        ctk.CTkEntry(
            inc_row, textvariable=self.m_inc,
            placeholder_text="Folder whose unique files will be merged in…",
        ).pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(
            inc_row, text="Browse", image=self.icons['folder'], compound="left", width=110,
            command=self._pick_merge_incoming,
        ).pack(side="left")

        self.m_dry = tk.BooleanVar(value=bool(config.get("merge_dry_run", True)))
        dry_cb = ctk.CTkCheckBox(
            sec, text="Dry Run (simulate only — no files copied)",
            variable=self.m_dry, font=FONT_BODY,
            command=lambda: config.set("merge_dry_run", self.m_dry.get()),
        )
        dry_cb.pack(anchor="w", pady=(6, 12))
        Tooltip(
            dry_cb,
            "Preview exactly what would be merged without copying any files. "
            "Turn off only once you're satisfied with the simulation.",
        )

        ctk.CTkButton(
            sec, text="Start Merge", image=self.icons['play'], compound="left",
            fg_color=COLOR_SAFE, hover_color=COLOR_SAFE_HOVER, command=self.start_merge,
            width=160, height=40, font=FONT_BODY,
        ).pack(anchor="w")

    def _init_datamine_section(self, parent: Any) -> None:
        f_card = ctk.CTkFrame(parent, corner_radius=8, fg_color="#2A2A2A")
        f_card.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(f_card, text="Vault Index Summary", font=FONT_TITLE).pack(
            anchor="w", padx=15, pady=(14, 8)
        )

        stats = ctk.CTkFrame(f_card, fg_color="transparent")
        stats.pack(fill="x", padx=15, pady=(0, 10))
        self.lbl_tot_files = ctk.CTkLabel(stats, text="Files Ingested: 0", font=FONT_BODY, text_color="#EAEAEA")
        self.lbl_golden = ctk.CTkLabel(stats, text="Verified Golden Masters: 0", font=FONT_BODY, text_color=COLOR_INFO)
        self.lbl_storage = ctk.CTkLabel(stats, text="Total Storage Used: 0 B", font=FONT_BODY, text_color="#9E9E9E")
        self.lbl_tot_files.pack(anchor="w", pady=2)
        self.lbl_golden.pack(anchor="w", pady=2)
        self.lbl_storage.pack(anchor="w", pady=2)

        self.btn_rationalize = ctk.CTkButton(
            f_card, text="Rationalize", fg_color=COLOR_INFO, hover_color=COLOR_INFO_HOVER,
            text_color=COLOR_ON_INFO, command=self.rationalize_mine,
            image=self.icons_dark['refresh'], compound="left", height=38, font=FONT_BODY,
        )
        self.btn_rationalize.pack(anchor="w", padx=15, pady=(0, 14))
        Tooltip(self.btn_rationalize, "Recompute which copy of each file is the 'golden' (kept) version and refresh the summary above.")

        f_archive = self._section(
            parent, "Commit to Vault",
            hint="Safely copy legacy records into your vault location in one pass. Source files stay put.",
            tight=True,
        )
        self.simulate_only_var = tk.BooleanVar(value=self._simulate_only_from_config())
        simulate_cb = ctk.CTkCheckBox(
            f_archive,
            text="Simulate Only (No File Transfer)",
            variable=self.simulate_only_var,
            font=FONT_BODY,
            command=self._sync_simulate_only_config,
        )
        simulate_cb.pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            f_archive,
            text=(
                "The manifest is always generated during elevation. Enable this to catalog "
                "without copying files into the vault. Your originals are never moved or deleted."
            ),
            font=FONT_CALM_SMALL,
            text_color=COLOR_HINT,
            anchor="w",
            justify="left",
            wraplength=820,
        ).pack(anchor="w", padx=(24, 0), pady=(0, 10))
        Tooltip(
            simulate_cb,
            "When enabled, PLAN (audit + manifest) runs but EXECUTE (vault copy) is skipped.",
        )
        ctk.CTkButton(
            f_archive, text="Commit to Vault", font=FONT_HEADER,
            fg_color=COLOR_INFO, hover_color=COLOR_INFO_HOVER, text_color=COLOR_ON_INFO,
            command=self.execute_bulk_archive, height=42,
        ).pack(anchor="w")

        ctk.CTkLabel(parent, text="Recent Golden Files", font=FONT_HEADER).pack(
            anchor="w", pady=(12, 4),
        )
        self.txt_golden = ctk.CTkTextbox(parent, height=140, font=("Consolas", 12))
        self.txt_golden.pack(fill="x", pady=(0, 12))
        self.txt_golden.configure(state="disabled")

        self.f_danger_zone = ctk.CTkFrame(parent, corner_radius=8, fg_color="#2A2A2A")
        self.f_danger_zone.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(
            self.f_danger_zone, text='Recovery & Maintenance',
            font=("Segoe UI", 12, "bold"), text_color=COLOR_CAUTION,
        ).pack(anchor='w', padx=15, pady=(10, 4))

        danger_row = ctk.CTkFrame(self.f_danger_zone, fg_color="transparent")
        danger_row.pack(fill="x", padx=15, pady=(0, 12))
        self.btn_revert = ctk.CTkButton(
            danger_row, text='Revert Last Vault Commit', command=self.revert_last_archive,
            fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, height=36, font=FONT_BODY,
        )
        self.btn_revert.pack(side='left', padx=(0, 8))
        Tooltip(self.btn_revert, "Undo the most recent vault commit, restoring copied vault records to their original locations.")
        self.btn_clear_log = ctk.CTkButton(
            danger_row, text='Clear Transaction Log', command=self.clear_archive_history,
            fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, height=36, font=FONT_BODY,
        )
        self.btn_clear_log.pack(side='left')
        Tooltip(self.btn_clear_log, "Permanently delete the vault transaction history. Revert will no longer be possible afterwards.")

        self.update_datamine_stats()

    def _boot_ai_engine(self) -> None:
        self._append_chat_text("\n[SYSTEM: Booting AI Engine...]\n")
        try:
            subprocess.Popen(["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", "start_ollama_agent.ps1"])
            self._append_chat_text("[SYSTEM: Ollama engine start command dispatched.]\n")
        except Exception as e:
            self._append_chat_text(f"[SYSTEM ERROR: Failed to boot AI engine: {e}]\n")

    def _kill_ai_engine(self) -> None:
        self._append_chat_text("\n[SYSTEM: Stopping AI Engine...]\n")
        try:
            subprocess.Popen(["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", "stop_ollama_agent.ps1"])
            self._append_chat_text("[SYSTEM: Ollama engine stop command dispatched.]\n")
        except Exception as e:
            self._append_chat_text(f"[SYSTEM ERROR: Failed to stop AI engine: {e}]\n")

    def _append_chat_text(self, text: str) -> None:
        self.chat_display.configure(state="normal")
        self.chat_display.insert("end", text)
        self.chat_display.see("end")
        self.chat_display.configure(state="disabled")

    def _send_message(self) -> None:
        query = self.chat_input.get().strip()
        if not query:
            return

        self._append_chat_text(f"\nUser: {query}\n\nAI: ")
        self.chat_input.delete(0, "end")

        if not self.vector_engine:
            directory_path = self.target_vault_dir.get()
            if not directory_path:
                directory_path = self._initial_vault_path()
            try:
                self.vector_engine = SovereignVectorEngine(directory_path)
            except Exception as e:
                self._append_chat_text(f"[Vector Retrieval Error: Vector engine not initialized. Please set a valid Vault Destination. Error: {str(e)}]\n")
                return

        def run_query():
            try:
                for chunk in generate_rag_response(query, self.vector_engine):
                    self._schedule_on_main(self._append_chat_text, chunk)
                self._schedule_on_main(self._append_chat_text, "\n")
            except Exception as e:
                self._schedule_on_main(
                    self._append_chat_text, f"\n[RAG Error: {str(e)}]\n"
                )

        threading.Thread(target=run_query, daemon=True).start()

    def _schedule_on_main(self, callback: Any, *args: Any) -> None:
        """Marshal a callback onto the Tk main loop when the window still exists."""
        try:
            if self.root.winfo_exists():
                self.root.after(0, callback, *args)
        except tk.TclError:
            pass

    def rationalize_mine(self) -> None:
        """Recompute golden/legacy classification without blocking the UI.

        The ``identify_golden_versions`` pass is expensive on large indexes
        (tens of thousands of files) and previously ran on the Tkinter main
        thread, freezing the window ("Not Responding"). It now runs in a daemon
        thread; all widget updates are marshalled back to the main thread via
        ``root.after`` to respect Tkinter's single-threaded contract.
        """
        # Already on the main thread here (button callback): keep this path free
        # of ALL database access so the click returns instantly. Even reading
        # stats (size for the indicator) is deferred to the worker thread.
        self.btn_rationalize.configure(state="disabled", text="Rationalising…")
        self._set_header_status("Rationalising in the background…")
        self._toast("Rationalisation initiated: Task running in background.")
        self._enqueue_log("Rationalisation initiated: Task running in background.")

        # Capture an immutable copy of the path; the worker shares no mutable
        # state with the main thread beyond the thread-safe log queue.
        db_path = self.db_path

        def worker() -> None:
            conn: Optional[sqlite3.Connection] = None
            try:
                # Dedicated, thread-local connection — never the GUI's shared one.
                conn = sqlite3.connect(db_path, timeout=30)
                conn.execute("PRAGMA foreign_keys = ON;")
                conn.execute("PRAGMA busy_timeout = 30000;")

                # Compute the header size indicator off the main thread.
                try:
                    row = conn.execute("SELECT SUM(file_size) FROM file_index").fetchone()
                    size = self._human_size((row[0] if row else 0) or 0)
                    self.root.after(0, lambda s=size: self._set_header_status(
                        f"Rationalising {s} of data in the background…"
                    ))
                except sqlite3.Error:
                    pass

                def progress(done: int, total: int) -> None:
                    self._enqueue_log(
                        f"Rationalising… classified {done:,}/{total:,} legacy groups"
                    )

                DatabaseManager.classify_golden_versions(conn, progress=progress)
            except Exception as exc:
                self.root.after(0, lambda e=exc: self._finish_rationalize(error=e))
                return
            finally:
                if conn is not None:
                    conn.close()
            self.root.after(0, self._finish_rationalize)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_rationalize(self, error: Optional[Exception] = None) -> None:
        """Re-enable the button, clear the indicator, and refresh stats.

        Runs on the main thread (dispatched via ``root.after``). Captures the
        activity log on success.

        Args:
            error: Exception raised by the background pass, if any.
        """
        self.btn_rationalize.configure(state="normal", text="Rationalize")
        self._set_header_status("")
        if error is not None:
            self.log(f"Rationalisation failed: {error}")
            self._toast(f"Rationalisation failed: {error}", kind="error")
            return
        self.update_datamine_stats()
        self.log("Rationalisation complete. Vault Index Summary updated.")

    def update_datamine_stats(self):
        stats = self.db_manager.get_mine_stats()
        if hasattr(self, 'lbl_tot_files'):
            self.lbl_tot_files.configure(text=f"Files Ingested: {stats['total_files']}")
        if hasattr(self, 'lbl_golden'):
            self.lbl_golden.configure(text=f"Verified Golden Masters: {stats['golden_files']}")
        if hasattr(self, 'lbl_storage'):
            self.lbl_storage.configure(
                text=f"Total Storage Used: {self._human_size(stats['total_storage'])}"
            )
        
        recent = self.db_manager.get_recent_golden_files(50)
        if hasattr(self, 'txt_golden'):
            self.txt_golden.configure(state="normal")
            self.txt_golden.delete(1.0, tk.END)
            for p in recent:
                self.txt_golden.insert(tk.END, p + "\n")
            self.txt_golden.configure(state="disabled")

    def execute_bulk_archive(self):
        if getattr(self, 'current_session_id', None) is None:
            messagebox.showwarning("No Session", "Please run an ingest session on Your Journey before committing to the Vault.")
            return
            
        # 1. Ensure status column exists
        with self.db_manager.conn:
            cur = self.db_manager.conn.cursor()
            cur.execute("PRAGMA table_info(file_index)")
            columns = [info[1] for info in cur.fetchall()]
            if 'status' not in columns:
                self.db_manager.conn.execute("ALTER TABLE file_index ADD COLUMN status TEXT DEFAULT 'active'")
                
        # Strict Scope Lock: Get the current search directory
        active_path = self.src_var.get()
        if not active_path or not os.path.isdir(active_path):
            messagebox.showwarning("No Source", "Please select a valid source directory on Your Journey to limit the vault commit scope.")
            return

        vault_manifest_root = self._require_destination_vault()
        if vault_manifest_root is None:
            return

        # Query DB for legacy copies in this session
        with self.db_manager.conn:
            cur = self.db_manager.conn.cursor()
            
            cur.execute("SELECT COUNT(*) FROM file_index WHERE is_golden = 0 AND last_session_id = ?", (self.current_session_id,))
            popup_count = cur.fetchone()[0]

            sql = "SELECT rowid, full_path, file_size, modified_time FROM file_index WHERE is_golden = 0 AND last_session_id = ? AND (status != 'archived' OR status IS NULL)"
            cur.execute(sql, (self.current_session_id,))
            records = cur.fetchall()
            
        if popup_count == 0 or not records:
            messagebox.showinfo("Info", f"Found 0 legacy copies to copy within [{Path(active_path).name}] for this session.")
            return
            
        session_timestamp = time.strftime("%Y%m%d_%H%M%S")
        isolation_path = (
            Path(vault_manifest_root).resolve()
            / "Isolated_Legacy_Copies"
            / f"Session_{session_timestamp}"
        )

        # Pre-flight disk space check on the Destination Vault volume
        total_size = sum(r[2] or 0 for r in records)
        free_space = shutil.disk_usage(Path(vault_manifest_root).resolve()).free

        if free_space < total_size:
            req_gb = total_size / (1024**3)
            free_gb = free_space / (1024**3)
            messagebox.showerror("Error", f"Insufficient disk space!\n\nRequired: {req_gb:.2f} GB\nAvailable: {free_gb:.2f} GB")
            return

        is_dry_run = self.simulate_only_var.get()
        msg = (
            f"Found {popup_count} legacy copies. Verified Golden Masters remain in place.\n\n"
            f"Ready to safely COPY records to the Vault at:\n{isolation_path}\n\n"
            "Your original source files will remain completely untouched. Proceed?"
        )
        if not messagebox.askyesno("Confirm Commit to Vault", msg):
            return

        # 4. UI Progress Setup
        archive_win = ctk.CTkToplevel(self.root)
        archive_win.title("Committing to Vault")
        archive_win.geometry("400x200")
        archive_win.transient(self.root)
        archive_win.grab_set()
        
        ctk.CTkLabel(archive_win, text="Copying legacy records to the Vault...", font=("Segoe UI", 14, "bold")).pack(pady=(20, 10))
        pbar = ctk.CTkProgressBar(archive_win, width=300)
        pbar.pack(pady=10)
        pbar.set(0)
        
        lbl_status = ctk.CTkLabel(archive_win, text="Preparing...")
        lbl_status.pack(pady=5)
        
        stop_archive = threading.Event()
        btn_cancel = ctk.CTkButton(archive_win, text="Cancel", fg_color="#C92C2C", hover_color="#992222", command=lambda: [stop_archive.set(), lbl_status.configure(text="Canceling... please wait.")])
        btn_cancel.pack(pady=10)
        
        # 5. The Move Loop
        def archive_task():
            simulated_moves = set()
            csv_entries = []
            moved_count = 0
            moved_size = 0
            
            report_path = Path(vault_manifest_root).resolve() / f"Archive_Manifest_{session_timestamp}.csv"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            transaction_id = session_timestamp

            try:
                if not is_dry_run:
                    isolation_path.mkdir(parents=True, exist_ok=True)

                for i, (rowid, fp_str, size, mtime) in enumerate(records):
                    if stop_archive.is_set(): break
                    src_path = Path(fp_str)
                    if not src_path.exists(): continue
                    try:
                        mtime_val = mtime if mtime else os.path.getmtime(src_path)
                        clean_file = self.sanitize_filename(src_path.name)
                        dest_file = isolation_path / clean_file
                        counter = 2
                        while dest_file.exists() or str(dest_file) in simulated_moves:
                            name_stem, name_ext = os.path.splitext(clean_file)
                            dest_file = isolation_path / f"{name_stem}_v{counter}{name_ext}"
                            counter += 1
                            
                        status_str = "SIMULATED" if is_dry_run else "COPIED"
                        
                        if is_dry_run:
                            simulated_moves.add(str(dest_file))
                        else:
                            self._shutil_copy_from_config()(str(src_path), str(dest_file))
                            with self.db_manager.conn:
                                self.db_manager.conn.execute("UPDATE file_index SET full_path = ?, status = 'archived', pre_archive_path = ?, archive_transaction_id = ? WHERE rowid = ?", (str(dest_file), str(src_path), transaction_id, rowid))
                        
                        size_mb = f"{(size or 0) / (1024 * 1024):.2f}"
                        csv_entries.append([status_str, src_path.name, str(src_path), str(dest_file), time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime_val)), size_mb, transaction_id, self.current_session_id])
                        moved_count += 1
                        moved_size += (size or 0)
                        status_verb = "Simulated copy" if is_dry_run else "Copied"
                        
                        if moved_count % 1000 == 0:
                            self.log(f"Commit to Vault: {status_verb} {moved_count} records...")
                            
                        self.root.after(0, lambda p=(i+1)/len(records), c=moved_count, t=len(records), v=status_verb: (pbar.set(p), lbl_status.configure(text=f"{v} {c} of {t} files...")))
                    except Exception as e: self.log(f"Vault commit error on {src_path.name}: {e}")
            finally:
                if csv_entries:
                    try:
                        with open(report_path, "w", newline="", encoding="utf-8") as f:
                            writer = csv.writer(f)
                            writer.writerow(["Status", "Filename", "Original Path", "New Path", "Modified Date", "File Size (MB)", "Transaction ID", "Session ID"])
                            writer.writerows(csv_entries)
                    except Exception as e:
                        self.log(f"Could not create CSV manifest: {e}")
                    
            action_done = "simulated copying of" if is_dry_run else "copied"
            msg = (
                f"Successfully {action_done} {moved_count} records to the Vault "
                f"(Totaling {moved_size / (1024**3):.2f} GB). "
                "Your original source files remain untouched."
            )
            if report_path.exists():
                msg += f"\n\nManifest saved to:\n{report_path.resolve()}"
                try:
                    if platform.system() == 'Windows': os.startfile(report_path)
                    elif platform.system() == 'Darwin': subprocess.call(['open', str(report_path)])
                    else: subprocess.call(['xdg-open', str(report_path)])
                except: pass
            
            self.root.after(0, lambda m=msg: [archive_win.destroy(), messagebox.showinfo("Vault Commit Complete", m), self.update_datamine_stats()])
        threading.Thread(target=archive_task, daemon=True).start()

    def revert_last_archive(self):
        with self.db_manager.conn:
            cur = self.db_manager.conn.cursor()
            cur.execute("SELECT archive_transaction_id FROM file_index WHERE archive_transaction_id IS NOT NULL ORDER BY archive_transaction_id DESC LIMIT 1")
            res = cur.fetchone()
            if not res:
                messagebox.showinfo("Info", "No recent vault commits found to revert.")
                return
            last_tx_id = res[0]
            cur.execute("SELECT rowid, full_path, pre_archive_path FROM file_index WHERE archive_transaction_id = ?", (last_tx_id,))
            records = cur.fetchall()

        if not records:
            messagebox.showinfo("Info", "No files found in the last vault commit transaction.")
            return

        if not messagebox.askyesno("Confirm Revert", f"Are you sure you want to revert {len(records)} files from the previous vault commit?"):
            return

        revert_win = ctk.CTkToplevel(self.root)
        revert_win.title("Reverting Vault Commit")
        revert_win.geometry("400x200")
        revert_win.transient(self.root)
        revert_win.grab_set()
        
        ctk.CTkLabel(revert_win, text="Restoring copies to original locations...", font=("Segoe UI", 14, "bold")).pack(pady=(20, 10))
        pbar = ctk.CTkProgressBar(revert_win, width=300)
        pbar.pack(pady=10)
        pbar.set(0)
        
        lbl_status = ctk.CTkLabel(revert_win, text="Preparing...")
        lbl_status.pack(pady=5)
        
        def revert_task():
            reverted_count = 0
            errors = 0
            for i, (rowid, current_path_str, pre_archive_path_str) in enumerate(records):
                try:
                    curr_path = Path(current_path_str)
                    orig_path = Path(pre_archive_path_str)
                    
                    if curr_path.exists():
                        orig_path.parent.mkdir(parents=True, exist_ok=True)
                        dest_file = orig_path
                        counter = 2
                        while dest_file.exists():
                            dest_file = orig_path.parent / f"{orig_path.stem}_v{counter}{orig_path.suffix}"
                            counter += 1
                        
                        shutil.copy2(str(curr_path), str(dest_file))
                        with self.db_manager.conn:
                            self.db_manager.conn.execute("UPDATE file_index SET full_path = ?, status = 'active', pre_archive_path = NULL, archive_transaction_id = NULL WHERE rowid = ?", (str(dest_file), rowid))
                        reverted_count += 1
                        self.root.after(0, lambda p=(i+1)/len(records), c=reverted_count, t=len(records): (pbar.set(p), lbl_status.configure(text=f"Reverted {c} of {t} files...")))
                    else:
                        errors += 1
                except Exception as e:
                    self.log(f"Revert Error on {current_path_str}: {e}")
                    errors += 1

            msg = f"Successfully reverted {reverted_count} files."
            if errors > 0:
                msg += f"\nEncountered {errors} errors during revert. Check log for details."
            self.root.after(0, lambda: [revert_win.destroy(), messagebox.showinfo("Revert Complete", msg), self.update_datamine_stats()])
            
        threading.Thread(target=revert_task, daemon=True).start()

    def clear_archive_history(self):
        """Permanently clears the transaction log to save space and finalize vault commits."""
        if not messagebox.askyesno("Confirm Clear", 
            "This will permanently forget where vaulted files came from.\n\n"
            "The 'Revert' function will no longer work for past vault copies. Proceed?"):
            return

        try:
            with self.db_manager.conn:
                # 1. Clear the 'flight recorder' columns
                self.db_manager.conn.execute(
                    "UPDATE file_index SET pre_archive_path = NULL, archive_transaction_id = NULL"
                )
            # 2. Reclaim disk space (must be outside the transaction block)
            self.db_manager.conn.execute("VACUUM")
            
            messagebox.showinfo("Success", "Transaction history cleared and database compacted.")
            self.update_datamine_stats()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to clear history: {e}")

    def start_audit(self) -> None:
        """One-click Begin Rescue: config-driven PLAN → EXECUTE with no blocking dialogs."""
        src = self.src_var.get().strip()
        if src:
            config.set("last_source", src)
        vault = self.target_vault_dir.get().strip()
        if vault:
            config.set("vault_path", vault)
        config.set("journey_export_mode", self.journey_export_mode.get())
        self.stop_event.clear()
        self.pause_event.set()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_pause.configure(state="normal", text="Pause")
        self.current_session_id = str(uuid.uuid4())
        self._set_elevation_status("Starting elevation…")
        storage = LocalDiskStorage()
        threading.Thread(target=lambda: self._run_elevation_pipeline(storage), daemon=True).start()

    def export_knowledge_graph(
        self,
        session_id: str,
        source_path: str,
        vault_export_root: Optional[str] = None,
    ) -> None:
        """Export this session's unique files to Obsidian and cloud-anchor them.

        Thin GUI wrapper that delegates to :func:`export_knowledge_graph_core`
        so the same logic is shared with the headless CLI entry point.

        Args:
            session_id: The audit session whose golden files should be exported.
            source_path: The scanned source directory (metadata context only).
            vault_export_root: Destination Vault root for ``Obsidian_Export`` (Step 1).
        """
        export_root = vault_export_root or self._require_destination_vault()
        if export_root is None:
            return
        export_knowledge_graph_core(
            self.db_manager,
            self.db_path,
            session_id,
            source_path,
            log=self.log,
            export_root=export_root,
        )

    def _show_review(self, groups, total, hash_cache):
        self.review_dialog = ReviewDialog(self.root, groups, total, self.db_manager, self.mode_var.get(), precomputed_hashes=hash_cache, 
                                          threshold=config.get("threshold", 5))

    def start_merge(self):
        merger = FolderMerger(self.m_master.get(), self.m_inc.get(), log_callback=self.log, progress_callback=self.progress, dry_run=self.m_dry.get(), db_manager=self.db_manager)
        def run_merger():
            merger.run()
            self.root.after(0, self.update_datamine_stats)
        threading.Thread(target=run_merger, daemon=True).start()

    def on_close(self):
        import os
        try:
            self.destroy()
        except Exception:
            pass
        finally:
            os._exit(0)

    def stop_scan(self):
        self.stop_event.set()
        self.pause_event.set() # Unpause to allow threads to exit
        self.log("Stop signal sent. Finishing current operation...")

    def toggle_pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.btn_pause.configure(text="Resume")
            self.log("Scanning paused.")
        else:
            self.pause_event.set()
            self.btn_pause.configure(text="Pause")
            self.log("Scanning resumed.")

    def reset_scan_buttons(self):
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.btn_pause.configure(state="disabled", text="Pause")

    def save_settings(self):
        config.set("threshold", self.threshold_var.get())
        config.set("threads", self.threads_var.get())
        config.set("ignore_exts", self.ignore_exts_var.get())
        config.set("ignore_folders", self.ignore_folders_var.get())
        if hasattr(self, "collision_policy_var"):
            self._apply_collision_policy(self.collision_policy_var.get())
        if hasattr(self, "hashing_depth_var"):
            self._apply_hashing_depth(self.hashing_depth_var.get())
        config.set("review_duplicates", self.review_var.get())
        config.set("notarise", self.notarise_var.get())
        config.set("journey_export_mode", self.journey_export_mode.get())
        if hasattr(self, "simulate_only_var"):
            self._sync_simulate_only_config()
        messagebox.showinfo("Settings", "Settings saved successfully.")

    def check_updates(self):
        # Placeholder for update logic
        messagebox.showinfo("Updates", f"You are running the latest version (v{APP_VERSION}).")

    def create_shortcut(self) -> None:
        """Create a Windows desktop shortcut to this app (source or frozen exe)."""
        if platform.system() != "Windows":
            messagebox.showerror("Error", "Desktop shortcuts are only supported on Windows.")
            return
        try:
            desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
            desktop.mkdir(parents=True, exist_ok=True)
            lnk_path = str((desktop / "sovraan.lnk").resolve())

            if getattr(sys, "frozen", False):
                target = str(Path(sys.executable).resolve())
                arguments = ""
                wdir = str(Path(sys.executable).resolve().parent)
            else:
                exe = Path(sys.executable)
                pythonw = exe.with_name("pythonw.exe")
                target = str((pythonw if pythonw.is_file() else exe).resolve())
                arguments = str(Path(__file__).resolve())
                wdir = str(Path(__file__).resolve().parent)

            def _ps_quote(value: str) -> str:
                return "'" + value.replace("'", "''") + "'"

            ps_cmd = (
                "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
                f"{_ps_quote(lnk_path)}); "
                f"$s.TargetPath = {_ps_quote(target)}; "
                f"$s.Arguments = {_ps_quote(arguments)}; "
                f"$s.WorkingDirectory = {_ps_quote(wdir)}; "
                "$s.Save()"
            )
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Sta",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    ps_cmd,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                raise RuntimeError(stderr or f"PowerShell exited with code {result.returncode}")
            messagebox.showinfo("Success", "Shortcut created on Desktop!")
        except Exception as e:
            messagebox.showerror("Error", f"Could not create shortcut: {e}")

    def report_bug(self) -> None:
        webbrowser.open("mailto:support@adzeee.com")

    def reset_settings(self):
        if messagebox.askyesno("Reset Settings", "Are you sure you want to reset all settings to their defaults?"):
            config.reset_to_defaults()
            defaults = config.defaults()
            self.threshold_var.set(defaults["threshold"])
            self.threads_var.set(defaults["threads"])
            self.ignore_exts_var.set(defaults["ignore_exts"])
            self.ignore_folders_var.set(defaults["ignore_folders"])
            if hasattr(self, "collision_policy_var"):
                label = self._collision_policy_label()
                self.collision_policy_var.set(label)
                self._apply_collision_policy(label)
            if hasattr(self, "hashing_depth_var"):
                label = self._hashing_depth_label()
                self.hashing_depth_var.set(label)
                self._apply_hashing_depth(label)
            self.journey_export_mode.set(defaults.get("journey_export_mode", "Standard Mode"))
            self.review_var.set(True)
            self.notarise_var.set(True)
            if hasattr(self, "simulate_only_var"):
                self.simulate_only_var.set(defaults.get("simulate_only", False))
                self._sync_simulate_only_config()
            self._sync_vault_path_display()
            messagebox.showinfo("Settings", "Settings reset to defaults.")

def _obsidian_source_folder_label(source_path: str) -> str:
    """Sanitised basename of the scanned source tree for vault nesting."""
    return _sanitise(Path(source_path).resolve().name or "import")


def _obsidian_year_bucket(record: UniqueFileRecord) -> str:
    """Calendar year for yearly vault nesting (from file mtime when available)."""
    try:
        if record.path.exists():
            return str(datetime.datetime.fromtimestamp(record.path.stat().st_mtime).year)
    except OSError:
        pass
    return str(datetime.datetime.now().year)


def _write_vault_map_of_content(
    vault_source_root: Path,
    source_label: str,
    note_stems: List[str],
) -> Path:
    """Write ``_Index_[Source].md`` with wikilinks to every note stem in this export."""
    vault_source_root.mkdir(parents=True, exist_ok=True)
    index_path = vault_source_root / f"_Index_{source_label}.md"
    unique_stems = sorted(set(note_stems))
    lines = "\n".join(f"- [[{stem}]]" for stem in unique_stems)
    body = f"# Map of Content: {source_label}\n\n{lines}\n"
    index_path.write_text(body, encoding="utf-8")
    return index_path


def export_knowledge_graph_core(
    db_manager: DatabaseManager,
    db_path: str,
    session_id: str,
    source_path: str,
    log: Callable[[str], None] = print,
    *,
    export_root: Optional[Union[str, Path]] = None,
    export_dirname: str = "",
) -> None:
    """Render this session's unique files to Obsidian and cloud-anchor them.

    Reads the golden (unique) file rows for ``session_id`` from the ledger,
    renders an Obsidian knowledge graph via :class:`MarkdownTranslator`, and
    best-effort anchors each hash through :class:`CloudNotaryBridge`. All
    failures are logged and contained so neither the GUI nor a headless run is
    aborted by a network or filesystem error.

    Args:
        db_manager: Open ledger connection wrapper.
        db_path: Path to the SQLite database (used as a fallback export root).
        session_id: The audit session whose golden files should be exported.
        source_path: The scanned source directory (used when ``export_root`` is omitted).
        log: Callable used to surface human-readable progress messages.
        export_root: Absolute vault or folder root for Markdown export (overrides
            ``source_path`` when set).
        export_dirname: Optional nested segment under ``export_root``; when empty,
            notes use ``[export_root]/[source_name]/[YYYY]/`` contextual nesting.
    """
    try:
        with db_manager.conn:
            cur = db_manager.conn.cursor()
            cur.execute(
                """
                SELECT full_path, sha256_hash
                FROM file_index
                WHERE is_golden = 1
                  AND sha256_hash IS NOT NULL
                  AND last_session_id = ?
                """,
                (session_id,),
            )
            rows = cur.fetchall()

        if not rows:
            log("Knowledge graph: no unique files for this session.")
            return

        records = [
            UniqueFileRecord(path=Path(full_path), file_hash=sha256_hash)
            for full_path, sha256_hash in rows
        ]

        if export_root is not None:
            vault_base = Path(export_root).resolve()
        elif source_path and os.path.isdir(source_path):
            vault_base = Path(source_path).resolve()
        else:
            vault_base = Path(db_path).resolve().parent

        source_label = _obsidian_source_folder_label(source_path)
        vault_source_root = vault_base / source_label
        if export_dirname:
            vault_source_root = vault_source_root / export_dirname

        by_year: Dict[str, List[UniqueFileRecord]] = defaultdict(list)
        for record in records:
            by_year[_obsidian_year_bucket(record)].append(record)

        all_note_stems: List[str] = []
        total_notes = 0
        for year, year_records in sorted(by_year.items()):
            year_root = vault_source_root / year
            translator = MarkdownTranslator(year_root, export_dirname="")
            result = translator.translate(year_records)
            total_notes += len(result.note_paths)
            all_note_stems.extend(note_path.stem for note_path in result.note_paths)

        index_path = _write_vault_map_of_content(vault_source_root, source_label, all_note_stems)
        log(
            f"Knowledge graph: wrote {total_notes} notes under "
            f"{vault_source_root} (by year) and map-of-content {index_path}."
        )

        bridge = CloudNotaryBridge(logger=log)
        anchored = 0
        for record in records:
            outcome = bridge.anchor(record.file_hash)
            if outcome.ok:
                anchored += 1
                with db_manager.conn:
                    db_manager.conn.execute(
                        """
                        INSERT INTO blockchain_proofs (file_hash, status, updated_at)
                        VALUES (?, 'SUBMITTED', CURRENT_TIMESTAMP)
                        ON CONFLICT(file_hash) DO UPDATE SET
                            status = 'SUBMITTED', updated_at = CURRENT_TIMESTAMP
                        """,
                        (record.file_hash,),
                    )
        log(f"Cloud notary: anchored {anchored}/{len(records)} hashes via gateway.")
    except Exception as e:
        log(f"Knowledge graph export failed: {e}")


def run_headless(args: argparse.Namespace) -> int:
    """Run a full audit (and optional notarisation/export) without a GUI.

    Intended for invocation by the Obsidian plugin via ``child_process.spawn``.
    Drives the same core engine as the GUI -- hashing, golden-version
    identification, OpenTimestamps notarisation, and the Obsidian knowledge
    graph export -- but synchronously, so the process only exits once all work
    is complete.

    Args:
        args: Parsed command-line arguments (see :func:`main`).

    Returns:
        Process exit code: ``0`` on success, non-zero on a fatal error.
    """
    def log(message: str) -> None:
        print(f"[sovraan] {message}", flush=True)

    source = getattr(args, "source", None) or args.target
    target = os.path.abspath(source)
    if not os.path.isdir(target):
        print(f"[sovraan] ERROR: source is not a directory: {target}", file=sys.stderr, flush=True)
        return 2

    if args.db:
        db_manager = DatabaseManager(db_path=Path(args.db).expanduser().resolve())
    else:
        db_manager = DatabaseManager()
    if getattr(args, "license", None):
        config.set("proLicenseKey", args.license.strip())
    license_key = (getattr(args, "license", None) or config.get("proLicenseKey") or "").strip()
    try:
        session_id = str(uuid.uuid4())
        log(f"Headless audit starting on: {target} (session {session_id})")

        ignore_exts = [e.strip() for e in args.ignore_exts.split(",") if e.strip()]
        ignore_folders = [f.strip() for f in args.ignore_folders.split(",") if f.strip()]

        auditor_cls = VideoFileAuditor if args.mode == "visual" else FileAuditor
        auditor = auditor_cls(
            target,
            log_callback=log,
            progress_callback=lambda *_: None,
            review_mode=False,
            threads=args.threads,
            ignore_exts=ignore_exts,
            ignore_folders=ignore_folders,
            db_manager=db_manager,
            session_id=session_id,
            license_key=license_key,
        )
        auditor.run()

        stats = db_manager.identify_golden_versions(session_id=session_id)
        log(f"Audit complete. Golden/legacy stats: {stats}")

        if args.export:
            export_root = None
            export_dirname = "Obsidian_Export"
            if args.destination:
                export_root = os.path.abspath(args.destination)
                export_dirname = ""
            export_knowledge_graph_core(
                db_manager,
                db_manager.db_path,
                session_id,
                target,
                log=log,
                export_root=export_root,
                export_dirname=export_dirname,
            )
        else:
            log("Knowledge graph export skipped (--no-export).")

        if args.notarise:
            log("Notarising unanchored hashes via OpenTimestamps...")
            DedupNotary(db_manager.db_path).batch_submit_unnotarised()
            log("Notarisation pass complete.")
        else:
            log("Notarisation skipped (--no-notarise).")

        log("Headless run finished successfully.")
        return 0
    except FreemiumLimitExceeded as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    except Exception as exc:
        print(f"[sovraan] FATAL: headless run failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        db_manager.close()


def main(argv: Optional[List[str]] = None) -> None:
    """Entry point: dispatch to headless mode when invoked with arguments.

    With no command-line arguments the standard CustomTkinter GUI is launched.
    When a target directory (or ``--headless``) is supplied -- as the Obsidian
    ExecutionEngine does via a background spawn -- the GUI is bypassed and the
    core engine runs headlessly, exiting with an appropriate status code.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv[1:]``).
    """
    verify_environment()
    parser = argparse.ArgumentParser(
        prog="sovraan_core",
        description="sovraan deduplication engine (GUI by default, headless when given a target).",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Source directory to audit (headless). Alias: --source.",
    )
    parser.add_argument(
        "--source",
        dest="source",
        default=None,
        metavar="DIR",
        help="Absolute path to raw files to audit (headless). Overrides positional target.",
    )
    parser.add_argument(
        "--destination",
        default=None,
        metavar="DIR",
        help="Absolute path to the Obsidian vault for knowledge-graph export.",
    )
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Absolute path to the production SQLite database file.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Force headless execution even without a positional target.",
    )
    parser.add_argument(
        "--mode",
        choices=["exact", "visual"],
        default="exact",
        help="Audit mode: 'exact' (SHA-256) or 'visual' (perceptual/video). Default: exact.",
    )
    parser.add_argument(
        "--threads", type=int, default=4, help="Worker thread count for hashing. Default: 4.",
    )
    parser.add_argument(
        "--ignore-exts", default="", help="Comma-separated file extensions to ignore.",
    )
    parser.add_argument(
        "--ignore-folders", default="", help="Comma-separated folder names to ignore.",
    )
    parser.add_argument(
        "--notarise",
        "--notarize",
        action=argparse.BooleanOptionalAction,
        default=True,
        dest="notarise",
        help="Run OpenTimestamps notarisation after the audit (default: enabled).",
    )
    parser.add_argument(
        "--export",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Export the Obsidian knowledge graph after the audit (default: enabled).",
    )
    parser.add_argument(
        "--license",
        default=None,
        metavar="JWT",
        help="Sovraan Pro license JWT (unlocks processing beyond the 2000-file freemium cap).",
    )
    args = parser.parse_args(argv)

    for label, value in (("source", args.source), ("destination", args.destination), ("db", args.db)):
        if value is not None and not Path(value).expanduser().is_absolute():
            parser.error(f"--{label} must be an absolute path (got: {value!r})")

    headless = args.target is not None or args.source is not None or args.headless
    if not headless:
        app = SovraanApp()
        app.root.mainloop()
        return

    if args.target is None and args.source is None:
        parser.error("Headless mode requires --source or a positional source directory.")

    sys.exit(run_headless(args))


if __name__ == "__main__":
    main()
