from __future__ import annotations

import os
import sys
import sqlite3
import shutil
import hashlib
import time
import threading
import json
import csv
import tempfile
import uuid
import platform
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import concurrent.futures
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, DefaultDict, Dict, List, Optional, Set, Tuple, Union
from check_db_v2 import ensure_blockchain_schema
from core.notary import DedupNotary

try:
    import customtkinter as ctk
except ImportError:
    print("Missing dependency. Run: pip install customtkinter")
    sys.exit(1)

# --- External Dependencies ---
try:
    from PIL import Image, ImageTk, ImageDraw
    import cv2
    import imagehash
    import numpy as np
except ImportError:
    print("Missing dependencies. Run: pip install pillow opencv-python-headless imagehash")
    sys.exit(1)

try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
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

    def __init__(self, db_name: str = "data_mine.db") -> None:
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))
        self.db_path = os.path.join(base_path, db_name)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON;")
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
        self.files_scanned = 0
        self.duplicates_found = 0
        self.bytes_saved = 0

    def get_partial_hash(self, filepath: Path) -> Optional[str]:
        try:
            with open(filepath, 'rb') as f:
                return hashlib.sha256(f.read(4096)).hexdigest()
        except OSError:
            # AUDIT-REVIEW: Handle unreadable files explicitly instead of bare except.
            return None

    def get_file_hash(self, filepath: Path, chunk_size: int = 1048576) -> Optional[str]:
        hasher = hashlib.sha256()
        try:
            with open(filepath, 'rb') as f:
                while chunk := f.read(chunk_size):
                    self.pause_event.wait()
                    hasher.update(chunk)
            return hasher.hexdigest()
        except OSError:
            # AUDIT-REVIEW: Handle unreadable files explicitly instead of bare except.
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

        # Force every file into the hashing executor to ensure the session_id is saved!
        full_tasks = [(fp, s) for s, paths in size_map.items() for fp in paths]
        processed_groups = defaultdict(lambda: defaultdict(list))

        # Phase 2: Full Hashing
        if full_tasks and not self.stop_event.is_set():
            self.update_progress(0, len(full_tasks), "Hashing content...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.threads) as executor:
                future_to_file = {executor.submit(self.get_file_hash, fp): (fp, s) for fp, s in full_tasks}
                completed = 0
                for future in concurrent.futures.as_completed(future_to_file):
                    self.pause_event.wait()
                    if self.stop_event.is_set(): break
                    fp, s = future_to_file[future]
                    h = future.result()
                    if h: 
                        processed_groups[s][h].append(fp)
                        if self.db_manager:
                            try:
                                mtime = fp.stat().st_mtime
                            except Exception:
                                mtime = 0.0
                                
                            with self.db_manager.conn:
                                cur = self.db_manager.conn.cursor()
                                cur.execute('''
                                    UPDATE file_index 
                                    SET sha256_hash = ?, file_size = ?, modified_time = ?, last_session_id = ? 
                                    WHERE full_path = ?
                                ''', (h, s, mtime, self.session_id, str(fp)))
                                
                                if cur.rowcount == 0:
                                    cur.execute('''
                                        INSERT INTO file_index (sha256_hash, phash, file_name, file_size, modified_time, full_path, device_id, last_session_id)
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    ''', (h, None, fp.name, s, mtime, str(fp), self.device_id, self.session_id))
                    completed += 1
                    self.update_progress(completed, len(full_tasks), f"Hashing: {completed}/{len(full_tasks)}")

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

class DedupApp:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("File Deduplicator Suite")
        self._center_window(1000, 600)
        self.review_dialog = None

        # Set application icon
        try:
            if getattr(sys, 'frozen', False):
                # If running as a bundled exe, the icon is in the temp folder
                base = sys._MEIPASS
            else:
                # If running as a script, the icon is next to the script
                base = os.path.dirname(os.path.abspath(__file__))
            icon_path = os.path.join(base, "app.ico")
            if os.path.exists(icon_path): self.root.iconbitmap(icon_path)
        except: pass

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
        
        self.nb = ctk.CTkTabview(self.root)
        self.nb.pack(fill="both", expand=True)
        
        self.t_audit = self.nb.add("Audit / Dedup")
        self.t_merge = self.nb.add("Merge Folders")
        self.t_settings = self.nb.add("Settings")
        self.t_datamine = self.nb.add("Data Mine")
        
        f_log = ctk.CTkFrame(self.root, fg_color="transparent")
        f_log.pack(fill="x", padx=20, pady=(10, 5))
        ctk.CTkLabel(f_log, text="Activity Log:").pack(side="left", padx=5)
        ctk.CTkButton(f_log, text="Clear Log", image=self.icons['trash'], compound="left", fg_color="gray", command=self.clear_log, width=100).pack(side="right")
        ctk.CTkButton(f_log, text="Save Log", image=self.icons['save'], compound="left", fg_color="gray", command=self.save_log, width=100).pack(side="right", padx=10)
        
        self.log_area = ctk.CTkTextbox(self.root, height=150)
        self.log_area.pack(fill="x", padx=20, pady=(0, 10))
        self.pbar = ctk.CTkProgressBar(self.root)
        self.pbar.pack(fill="x", padx=20, pady=(0, 20))
        self.pbar.set(0)
        
        self._init_audit_tab()
        self._init_merge_tab()
        self._init_settings_tab()
        self._init_datamine_tab()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _center_window(self, width, height):
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        self.root.geometry(f'{width}x{height}+{x}+{y}')

    def log(self, msg):
        self.root.after(0, lambda: self._log_ui(msg))

    def _log_ui(self, msg):
        self.log_area.insert(tk.END, msg + "\n"); self.log_area.see(tk.END)

    def clear_log(self):
        self.log_area.delete(1.0, tk.END)

    def save_log(self):
        f = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")])
        if f:
            try:
                with open(f, "w") as file: file.write(self.log_area.get(1.0, tk.END))
            except Exception as e: messagebox.showerror("Error", f"Could not save log: {e}")

    def progress(self, cur, tot, msg=""):
        self.root.after(0, lambda: self._progress_ui(cur, tot, msg))

    def _progress_ui(self, cur, tot, msg):
        if tot > 0: self.pbar.set(cur/tot)
        self.root.title(f"Dedup Suite - {msg}")

    def _init_audit_tab(self):
        f = ctk.CTkFrame(self.t_audit)
        f.pack(fill="x", padx=20, pady=20)
        
        ctk.CTkLabel(f, text="Source:").pack(side="left", padx=10, pady=10)
        self.src_var = tk.StringVar(value=self.settings["last_source"])
        ctk.CTkEntry(f, textvariable=self.src_var).pack(side="left", fill="x", expand=True, padx=10, pady=10)
        ctk.CTkButton(f, text="Browse", image=self.icons['folder'], compound="left", command=lambda: self.src_var.set(filedialog.askdirectory())).pack(side="left", padx=10, pady=10)

        f2 = ctk.CTkFrame(self.t_audit)
        f2.pack(fill="x", padx=20, pady=0)
        
        self.mode_var = tk.StringVar(value="Exact")
        ctk.CTkOptionMenu(f2, variable=self.mode_var, values=["Exact", "Visual/Video"]).pack(side="left", padx=10, pady=10)
        self.review_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(f2, text="Review Mode", variable=self.review_var).pack(side="left", padx=10, pady=10)

        f_buttons = ctk.CTkFrame(f2, fg_color="transparent")
        f_buttons.pack(side="right", padx=10, pady=10)
        
        self.btn_start = ctk.CTkButton(f_buttons, text="Start Scan", image=self.icons['play'], compound="left", fg_color="#2CC985", hover_color="#229966", command=self.start_audit)
        self.btn_start.pack(side="left", padx=5)
        self.btn_pause = ctk.CTkButton(f_buttons, text="Pause", image=self.icons['pause'], compound="left", fg_color="#E5A00D", hover_color="#B37D0A", command=self.toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=5)
        self.btn_stop = ctk.CTkButton(f_buttons, text="Stop", image=self.icons['stop'], compound="left", fg_color="#C92C2C", hover_color="#992222", command=self.stop_scan, state="disabled")
        self.btn_stop.pack(side="left", padx=5)

    def _init_merge_tab(self):
        f = ctk.CTkFrame(self.t_merge)
        f.pack(fill="x", padx=20, pady=20)
        
        self.m_master = tk.StringVar(value=self.settings["merge_master"])
        self.m_inc = tk.StringVar(value=self.settings["merge_incoming"])
        
        ctk.CTkLabel(f, text="Master Folder (Destination):").pack(anchor="w", padx=10, pady=(10,0))
        ctk.CTkEntry(f, textvariable=self.m_master).pack(fill="x", padx=10, pady=5)
        
        ctk.CTkLabel(f, text="Incoming Folder (Source):").pack(anchor="w", padx=10, pady=(10,0))
        ctk.CTkEntry(f, textvariable=self.m_inc).pack(fill="x", padx=10, pady=5)
        
        self.m_dry = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(f, text="Dry Run (Simulate only)", variable=self.m_dry).pack(pady=10)
        
        ctk.CTkButton(f, text="Start Merge", image=self.icons['play'], compound="left", fg_color="#2CC985", hover_color="#229966", command=self.start_merge).pack(pady=20)

    def _init_settings_tab(self):
        # Titled Frame for Settings
        f_container = ctk.CTkFrame(self.t_settings)
        f_container.pack(fill="x", padx=20, pady=20)
        
        ctk.CTkLabel(f_container, text="Global Settings", font=("Segoe UI", 16, "bold")).pack(anchor="w", padx=15, pady=(15, 5))
        
        f = ctk.CTkFrame(f_container, fg_color="transparent")
        f.pack(fill="x", padx=10, pady=10)
        
        # Grid Layout: 2 Columns
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

        # Threshold
        ctk.CTkLabel(f, text="Visual Similarity Threshold (0-20):").grid(row=0, column=0, sticky="w", padx=10, pady=5)
        self.threshold_var = tk.IntVar(value=self.settings.get('threshold', 0))
        ctk.CTkEntry(f, textvariable=self.threshold_var).grid(row=0, column=1, sticky="ew", padx=10, pady=5)

        # Threads
        ctk.CTkLabel(f, text="Processing Threads:").grid(row=1, column=0, sticky="w", padx=10, pady=5)
        self.threads_var = tk.IntVar(value=self.settings.get('threads', 4))
        ctk.CTkEntry(f, textvariable=self.threads_var).grid(row=1, column=1, sticky="ew", padx=10, pady=5)

        # Ignore Extensions
        ctk.CTkLabel(f, text="Ignore Extensions (e.g. .txt,.log):").grid(row=2, column=0, sticky="w", padx=10, pady=5)
        self.ignore_exts_var = tk.StringVar(value=self.settings.get('ignore_exts', ''))
        ctk.CTkEntry(f, textvariable=self.ignore_exts_var).grid(row=2, column=1, sticky="ew", padx=10, pady=5)

        # Ignore Folders
        ctk.CTkLabel(f, text="Ignore Folders (e.g. .git,cache):").grid(row=3, column=0, sticky="w", padx=10, pady=5)
        self.ignore_folders_var = tk.StringVar(value=self.settings.get('ignore_folders', ''))
        ctk.CTkEntry(f, textvariable=self.ignore_folders_var).grid(row=3, column=1, sticky="ew", padx=10, pady=5)
        
        # Action Buttons
        f_actions = ctk.CTkFrame(self.t_settings, fg_color="transparent")
        f_actions.pack(fill="x", padx=20, pady=10)
        
        ctk.CTkButton(f_actions, text="Save Settings", image=self.icons['save'], compound="left", fg_color="gray", command=self.save_settings).pack(fill="x", pady=5)
        ctk.CTkButton(f_actions, text="Reset to Defaults", image=self.icons['refresh'], compound="left", fg_color="gray", command=self.reset_settings).pack(fill="x", pady=5)
        
        f_extras = ctk.CTkFrame(self.t_settings, fg_color="transparent")
        f_extras.pack(fill="x", padx=20, pady=10)
        ctk.CTkButton(f_extras, text="Check for Updates", command=self.check_updates).pack(side="left", expand=True, padx=5)
        ctk.CTkButton(f_extras, text="Create Shortcut", command=self.create_shortcut).pack(side="left", expand=True, padx=5)
        ctk.CTkButton(f_extras, text="Report Bug", command=self.report_bug).pack(side="left", expand=True, padx=5)

    def _init_datamine_tab(self):
        # 1. Danger Zone (Bottom - Priority 1)
        self.f_danger_zone = ctk.CTkFrame(self.t_datamine, fg_color='transparent')
        self.f_danger_zone.pack(side='bottom', fill='x', padx=20, pady=20)

        ctk.CTkLabel(self.f_danger_zone, text='Recovery & Maintenance', font=('Segoe UI', 12, 'bold')).pack(anchor='w')

        self.btn_revert = ctk.CTkButton(self.f_danger_zone, text='Revert Last Archive', command=self.revert_last_archive, fg_color='#757575')
        self.btn_revert.pack(side='left', padx=5, pady=5)

        self.btn_clear_log = ctk.CTkButton(self.f_danger_zone, text='Clear Transaction Log', command=self.clear_archive_history, fg_color='#757575')
        self.btn_clear_log.pack(side='left', padx=5, pady=5)

        # 2. Summary (Top - Priority 2)
        f_card = ctk.CTkFrame(self.t_datamine)
        f_card.pack(side='top', fill="x", padx=20, pady=10)
        
        lbl_title = ctk.CTkLabel(f_card, text="Data Mine Summary", font=("Segoe UI", 18, "bold"), text_color="#212121")
        lbl_title.pack(anchor="w", padx=15, pady=(15, 5))
        
        self.lbl_tot_files = ctk.CTkLabel(f_card, text="Total Files Indexed: 0")
        self.lbl_tot_files.pack(anchor="w", padx=15, pady=2)
        
        self.lbl_golden = ctk.CTkLabel(f_card, text="Total Unique (Golden) Files: 0")
        self.lbl_golden.pack(anchor="w", padx=15, pady=2)
        
        self.lbl_storage = ctk.CTkLabel(f_card, text="Total Storage Used: 0 B")
        self.lbl_storage.pack(anchor="w", padx=15, pady=(2, 10))
        
        btn_rationalize = ctk.CTkButton(f_card, text="Rationalize", fg_color="#009688", hover_color="#00796B", command=self.rationalize_mine)
        btn_rationalize.pack(anchor="w", padx=15, pady=(0, 15))
        
        # 3. Archive Button (Middle-Top)
        self.archive_dry_run_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(self.t_datamine, text="Dry Run (Simulation Mode)", variable=self.archive_dry_run_var).pack(side='top', pady=10)
        
        btn_archive = ctk.CTkButton(self.t_datamine, text="Launch Bulk Archive (Safety First)", font=("Segoe UI", 14, "bold"), fg_color="#E5A00D", hover_color="#B37D0A", command=self.execute_bulk_archive)
        btn_archive.pack(side='top', pady=10)
        
        # 4. Results (Middle-Fill)
        ctk.CTkLabel(self.t_datamine, text="Recent Golden Files", font=("Segoe UI", 14, "bold")).pack(side='top', anchor="w", padx=20, pady=(10, 0))
        self.txt_golden = ctk.CTkTextbox(self.t_datamine)
        self.txt_golden.pack(side='top', fill="both", expand=True, padx=20, pady=(5, 20))
        self.txt_golden.configure(state="disabled")
        
        self.update_datamine_stats()

    def rationalize_mine(self):
        self.db_manager.identify_golden_versions()
        self.update_datamine_stats()
        self.log("Rationalization complete. Data Mine Summary updated.")

    def update_datamine_stats(self):
        stats = self.db_manager.get_mine_stats()
        self.lbl_tot_files.configure(text=f"Total Files Indexed: {stats['total_files']}")
        self.lbl_golden.configure(text=f"Total Unique (Golden) Files: {stats['golden_files']}")
        
        s = stats['total_storage']
        storage_str = f"{s} B"
        for u in ['B','KB','MB','GB']:
            if s < 1024:
                storage_str = f"{s:.2f} {u}"
                break
            s /= 1024
        else:
            storage_str = f"{s:.2f} TB"
            
        self.lbl_storage.configure(text=f"Total Storage Used: {storage_str}")
        
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
                
                try:
                    notary = DedupNotary(self.db_path)
                    notary_thread = threading.Thread(target=notary.batch_submit_unnotarised, daemon=True)
                    notary_thread.start()
                except Exception as e:
                    import sys
                    print(f"Failed to initialize notary background thread: {e}", file=sys.stderr)

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

if __name__ == "__main__":
    app = DedupApp()
    app.root.mainloop()
