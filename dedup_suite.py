from __future__ import annotations

import argparse
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
import tkinter as tk
from tkinter import messagebox, filedialog
import concurrent.futures
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union
from check_db_v2 import ensure_blockchain_schema
from core.notary import DedupNotary
from core.markdown_translator import MarkdownTranslator, UniqueFileRecord
from network.notary_bridge import CloudNotaryBridge
import ingest_kernel
from pipeline import run_pipeline

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
except ImportError:
    print("Missing dependencies. Run: pip install pillow opencv-python-headless imagehash")
    sys.exit(1)

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

class ConfigManager:
    """Load and persist application settings as JSON beside the executable or script."""

    def __init__(self, filename: str = "settings.json") -> None:
        # Determine if running as a script or frozen exe
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))
        self.filename = os.path.join(base_path, filename)
        self.defaults: Dict[str, Any] = {
            "last_source": "", "last_dest": "", "scan_mode": "Exact Match (Fast)",
            "threshold": 0, "threads": 4, "ignore_exts": "", "ignore_folders": "",
            "theme": "light", "merge_master": "", "merge_incoming": ""
        }

    def load(self) -> Dict[str, Any]:
        if not os.path.exists(self.filename):
            return self.defaults.copy()
        try:
            with open(self.filename, "r", encoding="utf-8") as f:
                config = self.defaults.copy()
                config.update(json.load(f))
                return config
        except (OSError, json.JSONDecodeError, TypeError):
            # AUDIT-REVIEW: Replace bare except with explicit I/O and JSON decode errors.
            return self.defaults.copy()

    def save(self, data: Dict[str, Any]) -> None:
        try:
            with open(self.filename, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except OSError:
            # AUDIT-REVIEW: Log settings persistence failures silently but safely.
            pass

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
                        shutil.move(str(dupe), str(target))
                except (OSError, shutil.Error) as e:
                    self.log(f"Error processing {dupe.name}: {e}")
            self.log(f"  {'Deleted' if self.delete else 'Moved'}: {dupe.name}")

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
            shutil.move(str(p), str(dest))
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
            if self.mode == "move": shutil.move(str(p), str(dest))
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
        f_top = ctk.CTkFrame(self.top)
        f_top.pack(fill="x", padx=20, pady=(20, 10))
        
        ctk.CTkLabel(f_top, text="Filter by Type:").pack(side="left", padx=10, pady=10)
        self.filter_var = tk.StringVar()
        ext_list = sorted(list(self.extensions))
        self.cb_filter = ctk.CTkComboBox(f_top, variable=self.filter_var, values=ext_list, width=100, command=lambda e: self.apply_filter())
        self.cb_filter.pack(side="left", padx=5, pady=10)
        
        ctk.CTkButton(f_top, text="Clear", image=self.icons['close'], compound="left", fg_color="gray", command=self.clear_filter, width=80).pack(side="left", padx=5, pady=10)
        ctk.CTkButton(f_top, text="Delete All Shown", image=self.icons['trash'], compound="left", fg_color="#C92C2C", hover_color="#992222", command=self.delete_all_shown).pack(side="left", padx=5, pady=10)
        ctk.CTkButton(f_top, text="Move All Shown", image=self.icons['arrow'], compound="left", command=self.move_all_shown).pack(side="left", padx=5, pady=10)
        
        self.lbl_stats = ctk.CTkLabel(f_top, text=f"Total Duplicates: {len(self.pairs)}")
        self.lbl_stats.pack(side="right", padx=20, pady=10)
        
        # Images
        self.f_content = ctk.CTkFrame(self.top, fg_color="transparent")
        self.f_content.pack(fill="both", expand=True, padx=20, pady=10)
        
        self.f_img = ctk.CTkFrame(self.f_content, fg_color="transparent")
        
        # Left (Original)
        f_left = ctk.CTkFrame(self.f_img)
        f_left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        ctk.CTkLabel(f_left, text="Original (Keep)", font=("Segoe UI", 14, "bold")).pack(pady=5)
        self.lbl_orig = ctk.CTkLabel(f_left, text="Loading Preview...")
        self.lbl_orig.pack(expand=True, pady=10)
        self.lbl_orig_path = ctk.CTkLabel(f_left, text="", wraplength=450, justify="center", font=("Segoe UI", 11))
        self.lbl_orig_path.pack(fill="x", pady=10, padx=10)

        # Right (Duplicate)
        f_right = ctk.CTkFrame(self.f_img)
        f_right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        ctk.CTkLabel(f_right, text="Duplicate (Delete)", font=("Segoe UI", 14, "bold"), text_color="#ff6666").pack(pady=5)
        self.lbl_dupe = ctk.CTkLabel(f_right, text="Loading Preview...")
        self.lbl_dupe.pack(expand=True, pady=10)
        self.lbl_dupe_path = ctk.CTkLabel(f_right, text="", wraplength=450, justify="center", font=("Segoe UI", 11))
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
        ctk.CTkButton(f_c_left, text="Smart Select", image=self.icons['check'], compound="left", command=self.smart_select, width=120).pack(side="left", padx=5, pady=10, anchor="center")
        ctk.CTkButton(f_c_left, text="Find Similar", image=self.icons['search'], compound="left", command=self.find_similar, width=120).pack(side="left", padx=5, pady=10, anchor="center")
        if HAS_REPORTLAB: ctk.CTkButton(f_c_left, text="PDF", image=self.icons['save'], compound="left", command=self.export_pdf, width=80).pack(side="left", padx=5, pady=10, anchor="center")
        ctk.CTkButton(f_c_left, text="CSV", image=self.icons['save'], compound="left", command=self.export_csv, width=80).pack(side="left", padx=5, pady=10, anchor="center")
        
        # Center controls (Move)
        f_c_center = ctk.CTkFrame(f_ctrl, fg_color="transparent")
        f_c_center.pack(side="left", padx=20)
        
        target_frame = ctk.CTkFrame(f_c_center, fg_color="transparent")
        target_frame.pack(side="left", padx=10, pady=0, anchor="center")
        ctk.CTkLabel(target_frame, text="Target Archive Folder", font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w")
        self.cb_targets = ctk.CTkComboBox(target_frame, variable=self.target_var, values=self.move_targets, width=150, state="readonly")
        self.cb_targets.grid(row=1, column=0, sticky="ew")
        
        def browse_target():
            d = filedialog.askdirectory()
            if d: 
                self.target_var.set(d)
                self._save_target()
            
        ctk.CTkButton(f_c_center, text="Browse", image=self.icons['folder'], compound="left", command=browse_target, width=80).pack(side="left", padx=5, pady=10, anchor="center")
        ctk.CTkButton(f_c_center, text="Move", image=self.icons['arrow'], compound="left", command=self.move_dupe, width=80).pack(side="left", pady=10, anchor="center")
        
        # Right controls
        f_c_right = ctk.CTkFrame(f_ctrl, fg_color="transparent")
        f_c_right.pack(side="right")
        ctk.CTkButton(f_c_right, text="Undo", image=self.icons['refresh'], compound="left", fg_color="gray", command=self.undo_last, width=80).pack(side="right", padx=5, pady=10, anchor="center")
        ctk.CTkButton(f_c_right, text="Skip >", image=self.icons['arrow'], compound="right", command=self.next_pair, width=80).pack(side="right", padx=5, pady=10, anchor="center")
        ctk.CTkButton(f_c_right, text="DELETE", image=self.icons['trash'], compound="left", fg_color="#C92C2C", hover_color="#992222", command=self.delete_dupe, width=100).pack(side="right", padx=10, pady=10, anchor="center")
        
        self.lbl_prog = ctk.CTkLabel(f_ctrl, text="0/0", font=("Segoe UI", 12, "bold"))
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
                shutil.move(str(dupe), str(tmp))
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
            messagebox.showwarning("No Destination", "Please select a valid destination folder from the 'Move to:' dropdown first.")
            return

        if not messagebox.askyesno("Move All", f"Are you sure you want to move all {len(self.pairs)} duplicates currently listed to:\n\n{target_dir}?"): return

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
                shutil.move(str(dupe), str(dest_file))
                operations.append((dest_file, dupe))
                count += 1
            except Exception as e: print(f"Error moving {dupe}: {e}")
        
        if operations:
            self.undo_stack.append((operations, restore_index))
        self.current_index = len(self.pairs)
        self._load_pair()
        messagebox.showinfo("Success", f"Moved {count} files to {target_dir}.")

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
            
            lbl_details = ctk.CTkLabel(f_row, text="", justify="left")
            lbl_details.grid(row=1, column=0, sticky="w", padx=10, pady=(5,10))
            
            btn_open = ctk.CTkButton(f_row, text="Open File Location", width=130)
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
                
            color = "#2CC985" if is_golden else "#ff6666"
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
            shutil.move(str(self.dupe), str(tmp))
            operations = [(tmp, self.dupe)]
            self.undo_stack.append((operations, self.current_index))
            self.next_pair()
        except Exception as e: messagebox.showerror("Error", str(e))

    def undo_last(self):
        if self.undo_stack:
            operations, idx = self.undo_stack.pop()
            for src, dest in operations:
                shutil.move(str(src), str(dest))
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
            shutil.move(str(self.dupe), str(dest))
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
        
        txt = ctk.CTkTextbox(f)
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
                entry = ctk.CTkEntry(f, width=300)
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
COLOR_SAFE = "#2CC985"
COLOR_SAFE_HOVER = "#229966"
COLOR_CAUTION = "#E5A00D"
COLOR_CAUTION_HOVER = "#B37D0A"
COLOR_DANGER = "#C92C2C"
COLOR_DANGER_HOVER = "#992222"
# Brand cyan sampled from the DedupSuite 2.0 logo (#66FCF1). Because the fill is
# bright, on-cyan text/icons use a near-black foreground for accessible contrast.
COLOR_INFO = "#66FCF1"
COLOR_INFO_HOVER = "#45CFC4"
COLOR_ON_INFO = "#0A0A0D"
COLOR_NEUTRAL = "#4A4D50"
COLOR_NEUTRAL_HOVER = "#393C3E"
COLOR_HINT = "#9A9A9A"
BRAND_BLACK = "#0B0B0D"

FONT_TITLE = ("Segoe UI", 20, "bold")
FONT_HEADER = ("Segoe UI", 15, "bold")
FONT_BODY = ("Segoe UI", 12)
FONT_HINT = ("Segoe UI", 11)
FONT_CALM_STEP = ("Segoe UI", 14, "bold")
FONT_CALM_BODY = ("Segoe UI", 12)
FONT_CALM_SMALL = ("Segoe UI", 10)
JOURNEY_PADY = 0

CALM_STEP1_TITLE = "Map the Swamp"
CALM_STEP2_TITLE = "Secure the Gold"
CALM_STEP3_TITLE = "Ignite Your Mind"


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
            self._tip, text=self.text, justify="left", bg="#2B2B2B", fg="#E6E6E6",
            relief="solid", borderwidth=1, font=("Segoe UI", 10), padx=8, pady=5,
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


class DedupApp:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("DedupSuite — Deduplication & Cryptographic Notary")
        self.root.minsize(1100, 700)
        self.root.after(100, lambda: self.root.state('zoomed'))
        self._center_window(1100, 720)
        self.review_dialog = None

        # Set application window icon (brand mark) via the lazy asset cache.
        self._set_window_icon()

        self.cfg = ConfigManager()
        self.db_manager = DatabaseManager()
        self.db_path = self.db_manager.db_path
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.settings = self.cfg.load()
        
        # Theme Setup
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        
        self.icons = IconFactory.create_icons()
        # Dark-foreground icon variant for use on bright (brand cyan) fills.
        self.icons_dark = IconFactory.create_icons(color=COLOR_ON_INFO)

        self._init_header()

        self.nb = ctk.CTkTabview(self.root)
        self.nb.pack(fill="both", expand=True)
        
        self.t_audit = self.nb.add("Your Journey")
        self.t_merge = self.nb.add("Merge Folders")
        self.t_settings = self.nb.add("Expert Studio")
        self.t_datamine = self.nb.add("The Vault Index")
        
        f_log = ctk.CTkFrame(self.root, fg_color="transparent")
        f_log.pack(fill="x", padx=20, pady=(10, 5))
        ctk.CTkLabel(f_log, text="Background notes:").pack(side="left", padx=5)
        ctk.CTkButton(f_log, text="Clear Log", image=self.icons['trash'], compound="left", fg_color="gray", command=self.clear_log, width=100).pack(side="right")
        self.btn_save_log = ctk.CTkButton(
            f_log, text="Save Log", image=self.icons['save'], compound="left",
            fg_color="gray", command=self.save_log, width=100,
        )
        self.btn_save_log.pack(side="right", padx=10)
        
        self.log_area = ctk.CTkTextbox(self.root, height=150)
        self.log_area.pack(fill="x", padx=20, pady=(0, 10))
        self.pbar = ctk.CTkProgressBar(self.root)
        self.pbar.pack(fill="x", padx=20, pady=(0, 20))
        self.pbar.set(0)

        self.journey_export_mode = tk.StringVar(value="standard")

        self._init_audit_tab()
        self._init_merge_tab()
        self._init_settings_tab()
        self._init_datamine_tab()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Thread-safe log buffer drained onto the widget every 100ms on the
        # main thread. Worker threads enqueue lines (never touch the widget).
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self.root.after(100, self._process_log_queue)

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

    def _init_header(self) -> None:
        """Build the branded header bar shown above the tab view.

        Prefers a pre-rendered wordmark lockup at ``assets/wordmark.png`` (drop
        the official ``DEDUP SUITE 2.0`` lockup there and it is used verbatim).
        If absent, a faithful lockup is composed from the brand icon plus the
        wordmark text and a cyan version badge. CustomTkinter needs images
        wrapped in :class:`CTkImage`, so references are retained on ``self`` to
        prevent garbage collection.
        """
        header = ctk.CTkFrame(self.root, fg_color=BRAND_BLACK, corner_radius=0, height=66)
        header.pack(side="top", fill="x")
        header.pack_propagate(False)

        # Right-aligned background-task status indicator (click for an info
        # pop-up). Created first so the wordmark early-return cannot skip it.
        self.header_status = ctk.CTkLabel(
            header, text="", font=("Segoe UI", 11), text_color=COLOR_INFO, cursor="hand2",
        )
        self.header_status.pack(side="right", padx=18)
        self.header_status.bind("<Button-1>", lambda _e: self._show_rationalise_info())

        # Preferred: official wordmark lockup image (SVG master or raster),
        # rendered once and cached at a header-friendly height.
        wordmark_source = self._resolve_brand_source("wordmark.svg", "wordmark.png")
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
                ctk.CTkLabel(header, image=self._wordmark_img, text="").pack(
                    side="left", padx=18, pady=10
                )
                return
            except Exception:
                pass  # fall through to composed lockup

        # Fallback: brand icon + wordmark text + cyan version badge.
        icon_source = self._resolve_brand_source("icon.svg", "icon.png")
        if icon_source:
            try:
                self._brand_icon_img = self.get_branded_image(icon_source, 42)
                ctk.CTkLabel(header, image=self._brand_icon_img, text="").pack(
                    side="left", padx=(18, 12), pady=12
                )
            except Exception:
                pass
        ctk.CTkLabel(
            header, text="DEDUP SUITE", font=("Segoe UI", 22, "bold"), text_color="#FFFFFF",
        ).pack(side="left", pady=12)
        ctk.CTkLabel(
            header, text="2.0", font=("Segoe UI", 13, "bold"), text_color=COLOR_INFO,
        ).pack(side="left", padx=(8, 0), pady=(16, 0), anchor="n")

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
        default_name = f"{time.strftime('%Y-%m-%d')}_DedupSuite_Log.txt"
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
                frame, text=message, bg="#1C1C1E", fg="#E6E6E6",
                font=("Segoe UI", 10), padx=14, pady=8,
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
            "Identifies and categorises redundant vs. golden file versions "
            "across your data mine, cryptographically linking legacy files to "
            "their source.\n\n"
            "This runs in the background — you remain free to use DedupSuite "
            "while it works.",
        )

    def progress(self, cur, tot, msg=""):
        self.root.after(0, lambda: self._progress_ui(cur, tot, msg))

    def _progress_ui(self, cur, tot, msg):
        if tot > 0: self.pbar.set(cur/tot)
        self.root.title(f"Dedup Suite - {msg}")

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
        card = ctk.CTkFrame(parent)
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

    def _pick_source_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose the folder to rescue")
        if chosen:
            self.src_var.set(chosen)

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
                text="Your rescued masters will appear here as the Identity Scanner runs.",
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

    def _select_journey_export_mode(self, mode: str) -> None:
        self.journey_export_mode.set(mode)
        self._refresh_journey_mode_cards()

    def _refresh_journey_mode_cards(self) -> None:
        active = self.journey_export_mode.get()
        standard_border = 2 if active == "standard" else 0
        intel_border = 2 if active == "intelligence" else 0
        self.card_standard.configure(border_width=standard_border, border_color=COLOR_INFO)
        self.card_intelligence.configure(border_width=intel_border, border_color=COLOR_INFO)

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
        card = ctk.CTkFrame(parent, corner_radius=8)
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

    def _init_audit_tab(self) -> None:
        """Calm 3-step journey — tight vertical spacing, no scroll."""
        self._rescue_matrix_cells: list[Any] = []
        journey = ctk.CTkFrame(self.t_audit, fg_color="transparent")
        journey.pack(side="top", fill="x", anchor="n", padx=2, pady=0)
        journey.grid_columnconfigure(0, weight=1)
        for row in range(5):
            journey.grid_rowconfigure(row, weight=0)

        step1 = self._calm_step_frame(
            journey,
            1,
            CALM_STEP1_TITLE,
            "Point DedupSuite at the folder or drive that feels overwhelming. "
            "We will walk it gently — no cloud uploads.",
            grid_row=0,
        )
        self.src_var = tk.StringVar(value=self.settings.get("last_source", ""))
        self.drop_zone = ctk.CTkFrame(
            step1, height=72, corner_radius=14,
            border_width=2, border_color=COLOR_NEUTRAL,
            fg_color=("gray90", "#1E1E22"),
        )
        self.drop_zone.pack(fill="x", pady=JOURNEY_PADY)
        self.drop_zone.pack_propagate(False)
        ctk.CTkLabel(
            self.drop_zone,
            text="Click or drag a folder here",
            font=FONT_CALM_BODY,
        ).pack(expand=True)
        ctk.CTkLabel(
            self.drop_zone,
            text="Choose the swamp you want to map",
            font=FONT_CALM_SMALL,
            text_color=COLOR_HINT,
        ).pack(pady=(0, 4))
        self._bind_drop_target(self.drop_zone)
        path_row = ctk.CTkFrame(step1, fg_color="transparent")
        path_row.pack(fill="x", pady=JOURNEY_PADY)
        ctk.CTkEntry(
            path_row, textvariable=self.src_var,
            placeholder_text="Selected folder path…",
            height=26, font=FONT_CALM_SMALL,
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(
            path_row, text="Browse", image=self.icons["folder"], compound="left",
            width=90, height=26, command=self._pick_source_folder,
        ).pack(side="left")

        step2 = self._calm_step_frame(
            journey,
            2,
            CALM_STEP2_TITLE,
            "Identity Scanner distills clutter into pristine master copies.",
            grid_row=1,
        )
        self.lbl_rescue_progress = ctk.CTkLabel(
            step2,
            text="Waiting to begin your rescue mission…",
            font=FONT_CALM_SMALL,
            text_color=COLOR_INFO,
            anchor="w",
            justify="left",
            wraplength=820,
        )
        self.lbl_rescue_progress.pack(fill="x", pady=JOURNEY_PADY)
        self.f_rescue_matrix = ctk.CTkFrame(step2, fg_color="transparent", height=28)
        self.f_rescue_matrix.pack(fill="x", pady=JOURNEY_PADY)
        self.f_rescue_matrix.pack_propagate(False)
        self._populate_rescue_matrix([])

        step3 = self._calm_step_frame(
            journey,
            3,
            CALM_STEP3_TITLE,
            "Pick how rescued masters are organised.",
            grid_row=2,
        )
        modes_row = ctk.CTkFrame(step3, fg_color="transparent")
        modes_row.pack(fill="x", pady=JOURNEY_PADY)
        modes_row.columnconfigure(0, weight=1)
        modes_row.columnconfigure(1, weight=1)

        self.card_standard = ctk.CTkFrame(
            modes_row, corner_radius=6, border_width=2, border_color=COLOR_INFO,
            cursor="hand2", height=44,
        )
        self.card_standard.grid(row=0, column=0, sticky="ew", padx=(0, 3), pady=JOURNEY_PADY)
        self.card_standard.grid_propagate(False)
        ctk.CTkLabel(
            self.card_standard,
            text="Standard Mode — chronological folders",
            font=FONT_CALM_SMALL,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=6, pady=JOURNEY_PADY)

        self.card_intelligence = ctk.CTkFrame(
            modes_row, corner_radius=6, border_width=0, border_color=COLOR_INFO,
            cursor="hand2", height=44,
        )
        self.card_intelligence.grid(row=0, column=1, sticky="ew", padx=(3, 0), pady=JOURNEY_PADY)
        self.card_intelligence.grid_propagate(False)
        ctk.CTkLabel(
            self.card_intelligence,
            text="Intelligence Mode (PLM) — private off-grid AI prep",
            font=FONT_CALM_SMALL,
            text_color=COLOR_HINT,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=6, pady=JOURNEY_PADY)

        def _bind_mode_card(card: ctk.CTkFrame, mode: str) -> None:
            card.bind("<Button-1>", lambda _e, m=mode: self._select_journey_export_mode(m))
            for child in card.winfo_children():
                child.bind("<Button-1>", lambda _e, m=mode: self._select_journey_export_mode(m))

        _bind_mode_card(self.card_standard, "standard")
        _bind_mode_card(self.card_intelligence, "intelligence")
        self._refresh_journey_mode_cards()

        self.mode_var = tk.StringVar(value="Exact")
        self.review_var = tk.BooleanVar(value=True)
        self.notarise_var = tk.BooleanVar(value=True)

        sec_run = ctk.CTkFrame(journey, fg_color="transparent")
        sec_run.grid(row=3, column=0, sticky="ew", padx=2, pady=JOURNEY_PADY)
        self.btn_start = ctk.CTkButton(
            sec_run,
            text="Begin Rescue",
            image=self.icons["play"],
            compound="left",
            fg_color=COLOR_SAFE,
            hover_color=COLOR_SAFE_HOVER,
            command=self.start_audit,
            width=140,
            height=28,
            font=FONT_CALM_SMALL,
        )
        self.btn_start.pack(side="left", padx=(0, 4))
        self.btn_pause = ctk.CTkButton(
            sec_run, text="Pause", image=self.icons["pause"], compound="left",
            fg_color=COLOR_CAUTION, hover_color=COLOR_CAUTION_HOVER,
            command=self.toggle_pause, state="disabled", width=80, height=28,
            font=FONT_CALM_SMALL,
        )
        self.btn_pause.pack(side="left", padx=(0, 4))
        self.btn_stop = ctk.CTkButton(
            sec_run, text="Stop", image=self.icons["stop"], compound="left",
            fg_color=COLOR_DANGER, hover_color=COLOR_DANGER_HOVER,
            command=self.stop_scan, state="disabled", width=80, height=28,
            font=FONT_CALM_SMALL,
        )
        self.btn_stop.pack(side="left")

    def _init_merge_tab(self):
        self.m_master = tk.StringVar(value=self.settings["merge_master"])
        self.m_inc = tk.StringVar(value=self.settings["merge_incoming"])

        sec = self._section(
            self.t_merge, "Merge folders",
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
            command=lambda: self.m_master.set(filedialog.askdirectory() or self.m_master.get()),
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
            command=lambda: self.m_inc.set(filedialog.askdirectory() or self.m_inc.get()),
        ).pack(side="left")

        self.m_dry = tk.BooleanVar(value=True)
        dry_cb = ctk.CTkCheckBox(
            sec, text="Dry Run (simulate only — no files moved)",
            variable=self.m_dry, font=FONT_BODY,
        )
        dry_cb.pack(anchor="w", pady=(6, 12))
        Tooltip(
            dry_cb,
            "Preview exactly what would be merged without touching any files. "
            "Turn off only once you're satisfied with the simulation.",
        )

        ctk.CTkButton(
            sec, text="Start Merge", image=self.icons['play'], compound="left",
            fg_color=COLOR_SAFE, hover_color=COLOR_SAFE_HOVER, command=self.start_merge,
            width=160, height=40, font=FONT_BODY,
        ).pack(anchor="w")

    def _init_settings_tab(self):
        # Scan parameters.
        f = self._section(
            self.t_settings, "Scan parameters",
            hint="Tune how the audit engine reads and filters your files.",
        )
        f.columnconfigure(1, weight=1)

        lbl_threshold = ctk.CTkLabel(f, text="Visual similarity threshold (0–20):", font=FONT_BODY)
        lbl_threshold.grid(row=0, column=0, sticky="w", padx=(0, 12), pady=6)
        self.threshold_var = tk.IntVar(value=self.settings.get('threshold', 0))
        ent_threshold = ctk.CTkEntry(f, textvariable=self.threshold_var, width=120)
        ent_threshold.grid(row=0, column=1, sticky="w", pady=6)
        Tooltip(
            lbl_threshold,
            "Only used in Visual/Video mode. 0 = identical perceptual hash; "
            "higher values match looser near-duplicates (8–12 is typical).",
        )

        ctk.CTkLabel(f, text="Processing threads:", font=FONT_BODY).grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=6
        )
        self.threads_var = tk.IntVar(value=self.settings.get('threads', 4))
        ctk.CTkEntry(f, textvariable=self.threads_var, width=120).grid(
            row=1, column=1, sticky="w", pady=6
        )

        ctk.CTkLabel(f, text="Ignore extensions (e.g. .txt,.log):", font=FONT_BODY).grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=6
        )
        self.ignore_exts_var = tk.StringVar(value=self.settings.get('ignore_exts', ''))
        ctk.CTkEntry(f, textvariable=self.ignore_exts_var).grid(
            row=2, column=1, sticky="ew", pady=6
        )

        ctk.CTkLabel(f, text="Ignore folders (e.g. .git,cache):", font=FONT_BODY).grid(
            row=3, column=0, sticky="w", padx=(0, 12), pady=6
        )
        self.ignore_folders_var = tk.StringVar(value=self.settings.get('ignore_folders', ''))
        ctk.CTkEntry(f, textvariable=self.ignore_folders_var).grid(
            row=3, column=1, sticky="ew", pady=6
        )

        # Persist / reset.
        f_actions = self._section(self.t_settings, "Configuration")
        btn_row = ctk.CTkFrame(f_actions, fg_color="transparent")
        btn_row.pack(fill="x")
        ctk.CTkButton(
            btn_row, text="Save Settings", image=self.icons['save'], compound="left",
            fg_color=COLOR_SAFE, hover_color=COLOR_SAFE_HOVER, command=self.save_settings,
            height=38, font=FONT_BODY,
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(
            btn_row, text="Reset to Defaults", image=self.icons['refresh'], compound="left",
            fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, command=self.reset_settings,
            height=38, font=FONT_BODY,
        ).pack(side="left", expand=True, fill="x", padx=(6, 0))

        # Application utilities.
        f_extras = self._section(self.t_settings, "Application")
        extras_row = ctk.CTkFrame(f_extras, fg_color="transparent")
        extras_row.pack(fill="x")
        for label, cmd in (
            ("Check for Updates", self.check_updates),
            ("Create Shortcut", self.create_shortcut),
            ("Report Bug", self.report_bug),
        ):
            ctk.CTkButton(
                extras_row, text=label, fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER,
                command=cmd, height=36, font=FONT_BODY,
            ).pack(side="left", expand=True, fill="x", padx=4)

    def _init_datamine_tab(self):
        # Danger / maintenance zone pinned to the bottom (built first so it
        # reserves space before the fill region above it).
        self.f_danger_zone = ctk.CTkFrame(self.t_datamine)
        self.f_danger_zone.pack(side='bottom', fill='x', padx=20, pady=(0, 15))

        ctk.CTkLabel(
            self.f_danger_zone, text='Recovery & Maintenance',
            font=("Segoe UI", 12, "bold"), text_color=COLOR_CAUTION,
        ).pack(anchor='w', padx=15, pady=(10, 4))

        danger_row = ctk.CTkFrame(self.f_danger_zone, fg_color="transparent")
        danger_row.pack(fill="x", padx=15, pady=(0, 12))
        self.btn_revert = ctk.CTkButton(
            danger_row, text='Revert Last Archive', command=self.revert_last_archive,
            fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, height=36, font=FONT_BODY,
        )
        self.btn_revert.pack(side='left', padx=(0, 8))
        Tooltip(self.btn_revert, "Undo the most recent bulk archive operation, restoring moved files to their original locations.")
        self.btn_clear_log = ctk.CTkButton(
            danger_row, text='Clear Transaction Log', command=self.clear_archive_history,
            fg_color=COLOR_NEUTRAL, hover_color=COLOR_NEUTRAL_HOVER, height=36, font=FONT_BODY,
        )
        self.btn_clear_log.pack(side='left')
        Tooltip(self.btn_clear_log, "Permanently delete the archive transaction history. Revert will no longer be possible afterwards.")

        # Summary card (top).
        f_card = ctk.CTkFrame(self.t_datamine)
        f_card.pack(side='top', fill="x", padx=20, pady=(15, 0))

        ctk.CTkLabel(f_card, text="Data Mine Summary", font=FONT_TITLE).pack(
            anchor="w", padx=15, pady=(14, 8)
        )

        stats = ctk.CTkFrame(f_card, fg_color="transparent")
        stats.pack(fill="x", padx=15, pady=(0, 10))
        self.lbl_tot_files = ctk.CTkLabel(stats, text="Total Files Indexed: 0", font=FONT_BODY)
        self.lbl_tot_files.pack(anchor="w", pady=2)
        self.lbl_golden = ctk.CTkLabel(stats, text="Total Unique (Golden) Files: 0", font=FONT_BODY)
        self.lbl_golden.pack(anchor="w", pady=2)
        self.lbl_storage = ctk.CTkLabel(stats, text="Total Storage Used: 0 B", font=FONT_BODY)
        self.lbl_storage.pack(anchor="w", pady=2)

        self.btn_rationalize = ctk.CTkButton(
            f_card, text="Rationalize", fg_color=COLOR_INFO, hover_color=COLOR_INFO_HOVER,
            text_color=COLOR_ON_INFO, command=self.rationalize_mine,
            image=self.icons_dark['refresh'], compound="left", height=38, font=FONT_BODY,
        )
        self.btn_rationalize.pack(anchor="w", padx=15, pady=(0, 14))
        Tooltip(self.btn_rationalize, "Recompute which copy of each file is the 'golden' (kept) version and refresh the summary above.")

        # Archive action card.
        f_archive = self._section(
            self.t_datamine, "Bulk archive",
            hint="Move every non-golden duplicate out to your archive location in one pass.",
        )
        self.archive_dry_run_var = tk.BooleanVar(value=True)
        archive_dry_cb = ctk.CTkCheckBox(
            f_archive, text="Dry Run (simulation mode — nothing is moved)",
            variable=self.archive_dry_run_var, font=FONT_BODY,
        )
        archive_dry_cb.pack(anchor="w", pady=(0, 10))
        Tooltip(archive_dry_cb, "Preview the archive plan without moving any files. Disable only when you're ready to commit.")
        ctk.CTkButton(
            f_archive, text="Launch Bulk Archive (Safety First)", font=FONT_HEADER,
            fg_color=COLOR_CAUTION, hover_color=COLOR_CAUTION_HOVER, command=self.execute_bulk_archive,
            height=42,
        ).pack(anchor="w")

        # Recent golden files (fills remaining space).
        ctk.CTkLabel(self.t_datamine, text="Recent Golden Files", font=FONT_HEADER).pack(
            side='top', anchor="w", padx=20, pady=(15, 4)
        )
        self.txt_golden = ctk.CTkTextbox(self.t_datamine)
        self.txt_golden.pack(side='top', fill="both", expand=True, padx=20, pady=(0, 15))
        self.txt_golden.configure(state="disabled")

        self.update_datamine_stats()

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
                        f"Rationalising… classified {done:,}/{total:,} duplicate groups"
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
        self.log("Rationalisation complete. Data Mine Summary updated.")

    def update_datamine_stats(self):
        stats = self.db_manager.get_mine_stats()
        self.lbl_tot_files.configure(text=f"Total Files Indexed: {stats['total_files']}")
        self.lbl_golden.configure(text=f"Total Unique (Golden) Files: {stats['golden_files']}")

        self.lbl_storage.configure(
            text=f"Total Storage Used: {self._human_size(stats['total_storage'])}"
        )
        
        recent = self.db_manager.get_recent_golden_files(50)
        self.txt_golden.configure(state="normal")
        self.txt_golden.delete(1.0, tk.END)
        for p in recent:
            self.txt_golden.insert(tk.END, p + "\n")
        self.txt_golden.configure(state="disabled")

    def execute_bulk_archive(self):
        if getattr(self, 'current_session_id', None) is None:
            messagebox.showwarning("No Session", "Please run an Audit first to establish a session for archiving.")
            return
            
        # 1. Ensure status column exists
        with self.db_manager.conn:
            cur = self.db_manager.conn.cursor()
            cur.execute("PRAGMA table_info(file_index)")
            columns = [info[1] for info in cur.fetchall()]
            if 'status' not in columns:
                self.db_manager.conn.execute("ALTER TABLE file_index ADD COLUMN status TEXT DEFAULT 'active'")
                
        # 2. Get target directory
        target_dir = filedialog.askdirectory(title="Select Target Archive Folder")
        if not target_dir:
            return
            
        target_path = Path(target_dir)
        
        # Strict Scope Lock: Get the current search directory
        active_path = self.src_var.get()
        if not active_path or not os.path.isdir(active_path):
            messagebox.showwarning("No Source", "Please select a valid Source directory in the 'Audit / Dedup' tab to limit the archive scope.")
            return
            
        
        # 3. Query DB for duplicates
        with self.db_manager.conn:
            cur = self.db_manager.conn.cursor()
            
            cur.execute("SELECT COUNT(*) FROM file_index WHERE is_golden = 0 AND last_session_id = ?", (self.current_session_id,))
            popup_count = cur.fetchone()[0]

            sql = "SELECT rowid, full_path, file_size, modified_time FROM file_index WHERE is_golden = 0 AND last_session_id = ? AND (status != 'archived' OR status IS NULL)"
            cur.execute(sql, (self.current_session_id,))
            records = cur.fetchall()
            
        if popup_count == 0 or not records:
            messagebox.showinfo("Info", f"Found 0 files to archive within [{Path(active_path).name}] for this session.")
            return
            
        # 3. Pre-flight check
        total_size = sum(r[2] or 0 for r in records)
        free_space = shutil.disk_usage(target_path).free
        
        if free_space < total_size:
            req_gb = total_size / (1024**3)
            free_gb = free_space / (1024**3)
            messagebox.showerror("Error", f"Insufficient disk space!\n\nRequired: {req_gb:.2f} GB\nAvailable: {free_gb:.2f} GB")
            return
            
        is_dry_run = self.archive_dry_run_var.get()
        action_word = "simulate archiving" if is_dry_run else "archive"
        
        current_folder = Path(active_path).name
        size_mb = total_size / (1024 * 1024)
        msg = f"Found {popup_count} duplicates within [{current_folder}]. Total size: {size_mb:.2f} MB.\n\nReady to {action_word}. Proceed?"
        if not messagebox.askyesno("Confirm Archive", msg):
            return
            
        # 4. UI Progress Setup
        archive_win = ctk.CTkToplevel(self.root)
        archive_win.title("Bulk Archiving")
        archive_win.geometry("400x200")
        archive_win.transient(self.root)
        archive_win.grab_set()
        
        ctk.CTkLabel(archive_win, text="Moving files to archive...", font=("Segoe UI", 14, "bold")).pack(pady=(20, 10))
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
            
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            report_path = target_path / f"Archive_Manifest_{timestamp}.csv"
            transaction_id = timestamp

            try:
                for i, (rowid, fp_str, size, mtime) in enumerate(records):
                    if stop_archive.is_set(): break
                    src_path = Path(fp_str)
                    if not src_path.exists(): continue
                    try:
                        mtime_val = mtime if mtime else os.path.getmtime(src_path)
                        date_str = time.strftime('%Y-%m-%d', time.localtime(mtime_val))
                        dest_folder = target_path / f"{date_str}_Archive"
                        
                        if not is_dry_run:
                            dest_folder.mkdir(parents=True, exist_ok=True)
                        
                        dest_file = dest_folder / src_path.name
                        counter = 2
                        while dest_file.exists() or str(dest_file) in simulated_moves:
                            dest_file = dest_folder / f"{src_path.stem}_v{counter}{src_path.suffix}"
                            counter += 1
                            
                        status_str = "SIMULATED" if is_dry_run else "MOVED"
                        
                        if is_dry_run:
                            simulated_moves.add(str(dest_file))
                        else:
                            shutil.move(str(src_path), str(dest_file))
                            with self.db_manager.conn:
                                self.db_manager.conn.execute("UPDATE file_index SET full_path = ?, status = 'archived', pre_archive_path = ?, archive_transaction_id = ? WHERE rowid = ?", (str(dest_file), str(src_path), transaction_id, rowid))
                        
                        size_mb = f"{(size or 0) / (1024 * 1024):.2f}"
                        csv_entries.append([status_str, src_path.name, str(src_path), str(dest_file), time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime_val)), size_mb, transaction_id, self.current_session_id])
                        moved_count += 1
                        moved_size += (size or 0)
                        status_verb = "Simulated" if is_dry_run else "Moved"
                        
                        if moved_count % 1000 == 0:
                            self.log(f"Bulk Archive: {status_verb} {moved_count} files...")
                            
                        self.root.after(0, lambda p=(i+1)/len(records), c=moved_count, t=len(records), v=status_verb: (pbar.set(p), lbl_status.configure(text=f"{v} {c} of {t} files...")))
                    except Exception as e: self.log(f"Archive Error on {src_path.name}: {e}")
            finally:
                if csv_entries:
                    try:
                        with open(report_path, "w", newline="", encoding="utf-8") as f:
                            writer = csv.writer(f)
                            writer.writerow(["Status", "Filename", "Original Path", "New Path", "Modified Date", "File Size (MB)", "Transaction ID", "Session ID"])
                            writer.writerows(csv_entries)
                    except Exception as e:
                        self.log(f"Could not create CSV manifest: {e}")
                    
            action_done = "simulated" if is_dry_run else "archived"
            msg = f"Successfully {action_done} {moved_count} files (Totaling {moved_size / (1024**3):.2f} GB)."
            if report_path.exists():
                msg += f"\n\nReport saved to: {report_path.name}"
                try:
                    if platform.system() == 'Windows': os.startfile(report_path)
                    elif platform.system() == 'Darwin': subprocess.call(['open', str(report_path)])
                    else: subprocess.call(['xdg-open', str(report_path)])
                except: pass
            
            self.root.after(0, lambda m=msg: [archive_win.destroy(), messagebox.showinfo("Archive Complete", m), self.update_datamine_stats()])
        threading.Thread(target=archive_task, daemon=True).start()

    def revert_last_archive(self):
        with self.db_manager.conn:
            cur = self.db_manager.conn.cursor()
            cur.execute("SELECT archive_transaction_id FROM file_index WHERE archive_transaction_id IS NOT NULL ORDER BY archive_transaction_id DESC LIMIT 1")
            res = cur.fetchone()
            if not res:
                messagebox.showinfo("Info", "No recent archives found to revert.")
                return
            last_tx_id = res[0]
            cur.execute("SELECT rowid, full_path, pre_archive_path FROM file_index WHERE archive_transaction_id = ?", (last_tx_id,))
            records = cur.fetchall()

        if not records:
            messagebox.showinfo("Info", "No files found in the last archive transaction.")
            return

        if not messagebox.askyesno("Confirm Revert", f"Are you sure you want to revert {len(records)} files from the previous archive?"):
            return

        revert_win = ctk.CTkToplevel(self.root)
        revert_win.title("Reverting Archive")
        revert_win.geometry("400x200")
        revert_win.transient(self.root)
        revert_win.grab_set()
        
        ctk.CTkLabel(revert_win, text="Moving files back...", font=("Segoe UI", 14, "bold")).pack(pady=(20, 10))
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
                        
                        shutil.move(str(curr_path), str(dest_file))
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
        """Permanently clears the transaction log to save space and finalize archives."""
        if not messagebox.askyesno("Confirm Clear", 
            "This will permanently forget where archived files came from.\n\n"
            "The 'Revert' function will no longer work for past moves. Proceed?"):
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

    def start_audit(self):
        self.stop_event.clear()
        self.pause_event.set()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_pause.configure(state="normal", text="Pause")
        
        self.current_session_id = str(uuid.uuid4())
        
        cls = VideoFileAuditor if self.mode_var.get() == "Visual/Video" else FileAuditor

        ignore_exts = [e.strip() for e in self.settings.get('ignore_exts', '').split(',') if e.strip()]
        ignore_folders = [f.strip() for f in self.settings.get('ignore_folders', '').split(',') if f.strip()]

        auditor = cls(self.src_var.get(), log_callback=self.log, progress_callback=self.progress,
                      review_mode=self.review_var.get(), stop_event=self.stop_event, pause_event=self.pause_event, threshold=self.settings.get('threshold', 0),
                      threads=self.settings.get('threads', 4), ignore_exts=ignore_exts, ignore_folders=ignore_folders, db_manager=self.db_manager, session_id=self.current_session_id)
        def run():
            try:
                auditor.run()
                
                stats = self.db_manager.identify_golden_versions(session_id=self.current_session_id)
                
                active_path = self.src_var.get()
                folder_name = os.path.basename(os.path.normpath(active_path))
                with self.db_manager.conn:
                    cur = self.db_manager.conn.cursor()
                    cur.execute("SELECT COUNT(*) FROM file_index WHERE is_golden = 0 AND full_path LIKE ?", (f"%{folder_name}%",))
                    local_count = cur.fetchone()[0]
                
                if self.notarise_var.get():
                    try:
                        notary = DedupNotary(self.db_path)
                        notary_thread = threading.Thread(target=notary.batch_submit_unnotarised, daemon=True)
                        notary_thread.start()
                    except Exception as e:
                        import sys
                        print(f"Failed to initialize notary background thread: {e}", file=sys.stderr)
                else:
                    self.log("Notarisation skipped (disabled in audit options).")

                # Weave unique files into the Obsidian knowledge graph and route
                # anchoring through the cloud gateway. Best-effort and isolated:
                # any failure here must never abort the audit conclusion.
                try:
                    kg_thread = threading.Thread(
                        target=self.export_knowledge_graph,
                        args=(self.current_session_id, active_path),
                        daemon=True,
                    )
                    kg_thread.start()
                except Exception as e:
                    self.log(f"Failed to start knowledge graph export: {e}")

                self.root.after(0, self.update_datamine_stats)
                
                if self.review_var.get():
                    groups, total = self.db_manager.get_duplicate_groups(scan_mode=self.mode_var.get(), threshold=self.settings.get('threshold', 0), limit=100, offset=0, session_id=self.current_session_id)
                    if local_count > 0:
                        msg = f"Audit Complete. {local_count} duplicates found in this session. You can now Review manually or Launch Bulk Archive in the Data Mine tab."
                        self.root.after(0, lambda m=msg: messagebox.showinfo("Audit Complete", m))
                    else:
                        self.log("Scan complete. No session duplicates found.")
                        self.root.after(0, lambda: messagebox.showinfo("Scan Complete", "No duplicates were found for this session."))
            except Exception as e:
                self.log(f"Error during scan: {e}")
            finally:
                self.root.after(0, self.reset_scan_buttons)
        threading.Thread(target=run, daemon=True).start()

    def export_knowledge_graph(self, session_id: str, source_path: str) -> None:
        """Export this session's unique files to Obsidian and cloud-anchor them.

        Thin GUI wrapper that delegates to :func:`export_knowledge_graph_core`
        so the same logic is shared with the headless CLI entry point.

        Args:
            session_id: The audit session whose golden files should be exported.
            source_path: The scanned source directory (export is written here).
        """
        export_knowledge_graph_core(
            self.db_manager, self.db_path, session_id, source_path, log=self.log
        )

    def _show_review(self, groups, total, hash_cache):
        self.review_dialog = ReviewDialog(self.root, groups, total, self.db_manager, self.mode_var.get(), precomputed_hashes=hash_cache, 
                                          threshold=self.settings.get('threshold', 5))

    def start_merge(self):
        merger = FolderMerger(self.m_master.get(), self.m_inc.get(), log_callback=self.log, progress_callback=self.progress, dry_run=self.m_dry.get(), db_manager=self.db_manager)
        def run_merger():
            merger.run()
            self.root.after(0, self.update_datamine_stats)
        threading.Thread(target=run_merger, daemon=True).start()

    def on_close(self):
        self.stop_event.set() # Signal any running threads to stop
        self.pause_event.set() # Unpause to allow threads to exit
        self.settings.update({"last_source": self.src_var.get(), "merge_master": self.m_master.get(), "merge_incoming": self.m_inc.get()})
        self.cfg.save(self.settings)
        self.db_manager.close()
        self.root.destroy()
        os._exit(0) # Forcefully and safely release the terminal prompt back to the user

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
        self.settings['threshold'] = self.threshold_var.get()
        self.settings['threads'] = self.threads_var.get()
        self.settings['ignore_exts'] = self.ignore_exts_var.get()
        self.settings['ignore_folders'] = self.ignore_folders_var.get()
        self.cfg.save(self.settings)
        messagebox.showinfo("Settings", "Settings saved successfully.")

    def check_updates(self):
        # Placeholder for update logic
        messagebox.showinfo("Updates", "You are running the latest version (v1.1.1).")

    def create_shortcut(self):
        try:
            desktop = os.path.join(os.environ['USERPROFILE'], 'Desktop')
            lnk_path = os.path.join(desktop, "Dedup Suite.lnk")
            
            if getattr(sys, 'frozen', False):
                target = sys.executable
                args = ""
                wdir = os.path.dirname(sys.executable)
            else:
                target = sys.executable.replace("python.exe", "pythonw.exe")
                args = f'"{os.path.abspath(__file__)}"'
                wdir = os.path.dirname(os.path.abspath(__file__))

            vbs = f'Set oWS = WScript.CreateObject("WScript.Shell")\n' \
                  f'Set oLink = oWS.CreateShortcut("{lnk_path}")\n' \
                  f'oLink.TargetPath = "{target}"\n' \
                  f'oLink.Arguments = "{args}"\n' \
                  f'oLink.WorkingDirectory = "{wdir}"\n' \
                  f'oLink.Save'
            
            vbs_file = Path(tempfile.gettempdir()) / "mk_shortcut.vbs"
            vbs_file.write_text(vbs)
            subprocess.run(['cscript', '/nologo', str(vbs_file)], check=True)
            vbs_file.unlink()
            messagebox.showinfo("Success", "Shortcut created on Desktop!")
        except Exception as e: messagebox.showerror("Error", f"Could not create shortcut: {e}")

    def report_bug(self):
        messagebox.showinfo("Report Bug", "Please report any issues to support@example.com")

    def reset_settings(self):
        if messagebox.askyesno("Reset Settings", "Are you sure you want to reset all settings to their defaults?"):
            self.settings = self.cfg.defaults.copy()
            self.threshold_var.set(self.settings['threshold'])
            self.threads_var.set(self.settings['threads'])
            self.ignore_exts_var.set(self.settings['ignore_exts'])
            self.ignore_folders_var.set(self.settings['ignore_folders'])
            messagebox.showinfo("Settings", "Settings reset to defaults. Click 'Save Settings' to persist changes.")

def export_knowledge_graph_core(
    db_manager: DatabaseManager,
    db_path: str,
    session_id: str,
    source_path: str,
    log: Callable[[str], None] = print,
    *,
    export_root: Optional[Union[str, Path]] = None,
    export_dirname: str = "Obsidian_Export",
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
        export_dirname: Subfolder under ``export_root``; use ``""`` to write notes
            directly into the vault root.
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
            root = str(Path(export_root).resolve())
        elif source_path and os.path.isdir(source_path):
            root = source_path
        else:
            root = os.path.dirname(db_path)
        translator = MarkdownTranslator(root, export_dirname=export_dirname)
        result = translator.translate(records)
        log(
            f"Knowledge graph: wrote {len(result.note_paths)} notes and "
            f"{len(result.index_paths)} folder indexes to {result.export_dir}."
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
        print(f"[DEDUP] {message}", flush=True)

    source = getattr(args, "source", None) or args.target
    target = os.path.abspath(source)
    if not os.path.isdir(target):
        print(f"[DEDUP] ERROR: source is not a directory: {target}", file=sys.stderr, flush=True)
        return 2

    if args.db:
        db_manager = DatabaseManager(db_path=Path(args.db).expanduser().resolve())
    else:
        db_manager = DatabaseManager()
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
    except Exception as exc:
        print(f"[DEDUP] FATAL: headless run failed: {exc}", file=sys.stderr, flush=True)
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
    parser = argparse.ArgumentParser(
        prog="dedup_suite",
        description="DedupSuite deduplication engine (GUI by default, headless when given a target).",
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
    args = parser.parse_args(argv)

    for label, value in (("source", args.source), ("destination", args.destination), ("db", args.db)):
        if value is not None and not Path(value).expanduser().is_absolute():
            parser.error(f"--{label} must be an absolute path (got: {value!r})")

    headless = args.target is not None or args.source is not None or args.headless
    if not headless:
        app = DedupApp()
        app.root.mainloop()
        return

    if args.target is None and args.source is None:
        parser.error("Headless mode requires --source or a positional source directory.")

    sys.exit(run_headless(args))


if __name__ == "__main__":
    main()
