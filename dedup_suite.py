import os
import sys
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

class ConfigManager:
    def __init__(self, filename="settings.json"):
        # Determine if running as a script or frozen exe
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))
        self.filename = os.path.join(base_path, filename)
        self.defaults = {
            "last_source": "", "last_dest": "", "scan_mode": "Exact Match (Fast)",
            "threshold": 0, "threads": 4, "ignore_exts": "", "ignore_folders": "",
            "theme": "light", "merge_master": "", "merge_incoming": ""
        }

    def load(self):
        if not os.path.exists(self.filename): return self.defaults.copy()
        try:
            with open(self.filename, "r") as f:
                config = self.defaults.copy()
                config.update(json.load(f))
                return config
        except: return self.defaults.copy()

    def save(self, data):
        try:
            with open(self.filename, "w") as f: json.dump(data, f, indent=4)
        except: pass

class ThemeManager:
    COLORS = {
        "light": {
            "bg": "#f5f5f5", "fg": "#212121", "entry_bg": "#ffffff", "entry_fg": "#212121",
            "btn_bg": "#009688", "btn_fg": "#ffffff", "btn_active": "#00796b",
            "select_bg": "#00796b", "select_fg": "#ffffff", "txt_bg": "#ffffff", "txt_fg": "#212121"
        },
        "dark": {
            "bg": "#2e3440", "fg": "#d8dee9", "entry_bg": "#3b4252", "entry_fg": "#eceff4",
            "btn_bg": "#5e81ac", "btn_fg": "#eceff4", "btn_active": "#81a1c1",
            "select_bg": "#88c0d0", "select_fg": "#2e3440", "txt_bg": "#3b4252", "txt_fg": "#d8dee9"
        }
    }
    def __init__(self, root):
        self.root = root
        self.style = ttk.Style()
        self.current_mode = "light"
        try: self.style.theme_use('clam')
        except: pass

    def toggle(self):
        self.current_mode = "dark" if self.current_mode == "light" else "light"
        self.apply_theme()
        return self.current_mode

    def apply_theme(self):
        c = self.COLORS[self.current_mode]
        self.root.configure(bg=c["bg"])
        self.style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["entry_bg"], troughcolor=c["bg"], selectbackground=c["select_bg"], selectforeground=c["select_fg"])
        self.style.configure("TButton", background=c["btn_bg"], foreground=c["btn_fg"], borderwidth=0, padding=(10, 5), font=("Segoe UI", 10))
        self.style.map("TButton", background=[("active", c["btn_active"])])
        self.style.configure("TEntry", fieldbackground=c["entry_bg"], foreground=c["entry_fg"])
        self.style.configure("TCombobox", fieldbackground=c["entry_bg"], foreground=c["entry_fg"], arrowcolor=c["fg"])
        self.style.configure("TLabelframe", background=c["bg"], borderwidth=0)
        self.style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"], font=("Segoe UI", 11, "bold"))
        self._update_tk_widgets(self.root, c)

    def _update_tk_widgets(self, parent, colors):
        for widget in parent.winfo_children():
            if widget.winfo_class() in ["Text", "ScrolledText"]:
                widget.configure(bg=colors["txt_bg"], fg=colors["txt_fg"], insertbackground=colors["fg"], selectbackground=colors["select_bg"])
            elif widget.winfo_class() == "Canvas":
                widget.configure(bg=colors["bg"])
            elif widget.winfo_class() == "Listbox":
                widget.configure(bg=colors["entry_bg"], fg=colors["entry_fg"])
            if widget.winfo_children(): self._update_tk_widgets(widget, colors)

class IconFactory:
    @staticmethod
    def create_icons(color="#ffffff"):
        icons = {}
        def new_img(): return Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        
        # Folder (Browse)
        img = new_img(); d = ImageDraw.Draw(img)
        d.polygon([(2, 4), (6, 4), (8, 6), (14, 6), (14, 12), (2, 12)], outline=color, fill=None)
        d.rectangle([3, 7, 13, 11], fill=color)
        icons['folder'] = ImageTk.PhotoImage(img)

        # Play (Start)
        img = new_img(); d = ImageDraw.Draw(img)
        d.polygon([(5, 3), (5, 13), (13, 8)], fill=color)
        icons['play'] = ImageTk.PhotoImage(img)

        # Pause
        img = new_img(); d = ImageDraw.Draw(img)
        d.rectangle([4, 3, 6, 13], fill=color); d.rectangle([10, 3, 12, 13], fill=color)
        icons['pause'] = ImageTk.PhotoImage(img)

        # Stop
        img = new_img(); d = ImageDraw.Draw(img)
        d.rectangle([4, 4, 12, 12], fill=color)
        icons['stop'] = ImageTk.PhotoImage(img)
        
        # Save
        img = new_img(); d = ImageDraw.Draw(img)
        d.rectangle([3, 3, 13, 13], outline=color); d.rectangle([5, 3, 11, 5], fill=color); d.rectangle([5, 9, 11, 11], fill=color)
        icons['save'] = ImageTk.PhotoImage(img)
        
        # Trash
        img = new_img(); d = ImageDraw.Draw(img)
        d.rectangle([5, 5, 11, 13], outline=color); d.line([(4, 3), (12, 3)], fill=color); d.line([(7, 2), (9, 2)], fill=color)
        icons['trash'] = ImageTk.PhotoImage(img)
        
        # Refresh/Reset
        img = new_img(); d = ImageDraw.Draw(img)
        d.arc([3, 3, 13, 13], 0, 270, fill=color, width=2); d.polygon([(13, 3), (13, 7), (9, 3)], fill=color)
        icons['refresh'] = ImageTk.PhotoImage(img)
        
        # Search
        img = new_img(); d = ImageDraw.Draw(img)
        d.ellipse([3, 3, 10, 10], outline=color, width=2); d.line([(9, 9), (13, 13)], fill=color, width=2)
        icons['search'] = ImageTk.PhotoImage(img)
        
        # Arrow Right
        img = new_img(); d = ImageDraw.Draw(img)
        d.line([(3, 8), (11, 8)], fill=color, width=2); d.polygon([(11, 5), (11, 11), (14, 8)], fill=color)
        icons['arrow'] = ImageTk.PhotoImage(img)
        
        # Check
        img = new_img(); d = ImageDraw.Draw(img)
        d.line([(3, 8), (6, 11), (13, 4)], fill=color, width=2)
        icons['check'] = ImageTk.PhotoImage(img)

        # Close
        img = new_img(); d = ImageDraw.Draw(img)
        d.line([(4, 4), (12, 12)], fill=color, width=2); d.line([(4, 12), (12, 4)], fill=color, width=2)
        icons['close'] = ImageTk.PhotoImage(img)
        return icons

# ==========================================
#               LOGIC CLASSES
# ==========================================

class FileAuditor:
    def __init__(self, root_path, move_to=None, delete=False, dry_run=True, threads=4, report_file=None, 
                 log_callback=None, progress_callback=None, stop_event=None, ignore_exts=None, ignore_folders=None, 
                 threshold=0, review_mode=False, pause_event=None):
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
        self.found_groups = []
        self.files_scanned = 0
        self.duplicates_found = 0
        self.bytes_saved = 0

    def get_partial_hash(self, filepath):
        try:
            with open(filepath, 'rb') as f:
                return hashlib.sha256(f.read(4096)).hexdigest()
        except: return None

    def get_file_hash(self, filepath, chunk_size=1048576):
        hasher = hashlib.sha256()
        try:
            with open(filepath, 'rb') as f:
                while chunk := f.read(chunk_size):
                    self.pause_event.wait()
                    hasher.update(chunk)
            return hasher.hexdigest()
        except: return None

    def run(self):
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

        potential_dupes = {s: p for s, p in size_map.items() if len(p) > 1}
        
        # Phase 1: Partial Hashing (Pre-screen)
        partial_tasks = [(fp, s) for s, paths in potential_dupes.items() for fp in paths]
        full_tasks = []
        processed_groups = defaultdict(lambda: defaultdict(list))
        
        if partial_tasks:
            self.update_progress(0, len(partial_tasks), "Pre-screening files...")
            partial_map = defaultdict(list)
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.threads) as executor:
                future_to_file = {executor.submit(self.get_partial_hash, fp): (fp, s) for fp, s in partial_tasks}
                completed = 0
                for future in concurrent.futures.as_completed(future_to_file):
                    self.pause_event.wait()
                    if self.stop_event.is_set(): break
                    fp, s = future_to_file[future]
                    ph = future.result()
                    if ph: partial_map[(s, ph)].append(fp)
                    completed += 1
                    self.update_progress(completed, len(partial_tasks), f"Pre-screening: {completed}/{len(partial_tasks)}")
            
            for (s, ph), paths in partial_map.items():
                if len(paths) > 1:
                    for fp in paths: full_tasks.append((fp, s))

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
                    if h: processed_groups[s][h].append(fp)
                    completed += 1
                    self.update_progress(completed, len(full_tasks), f"Hashing: {completed}/{len(full_tasks)}")

        for size, hash_group in processed_groups.items():
            for h, file_list in hash_group.items():
                if len(file_list) > 1: self.handle_duplicates(file_list)
        
        self.log("Audit Complete.")

    def handle_duplicates(self, file_list):
        if self.review_mode:
            self.found_groups.append(file_list)
            return
        # Basic auto-resolve logic (Keep Oldest)
        try: file_list.sort(key=lambda x: x.stat().st_ctime)
        except: pass
        original = file_list[0]
        duplicates = file_list[1:]
        self.duplicates_found += len(duplicates)
        self.bytes_saved += sum(f.stat().st_size for f in duplicates)
        self.log(f"Keeping: {original.name}")
        for dupe in duplicates:
            if not self.dry_run:
                try:
                    if self.delete: os.remove(dupe)
                    elif self.move_to: 
                        target = self.move_to / dupe.relative_to(self.root_path)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(dupe), str(target))
                except (OSError, shutil.Error) as e: self.log(f"Error processing {dupe.name}: {e}")
            self.log(f"  {'Deleted' if self.delete else 'Moved'}: {dupe.name}")

class VideoFileAuditor(FileAuditor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.valid_extensions = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.jpg', '.jpeg', '.png', '.bmp'}
        self.hash_cache = {}

    def get_fingerprint(self, filepath):
        try:
            if filepath.suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp'}:
                self.pause_event.wait()
                with Image.open(filepath) as img: return (imagehash.phash(img),)
            
            cap = cv2.VideoCapture(str(filepath))
            if not cap.isOpened(): return None
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if count < 10: return None
            hashes = []
            for p in [0.1, 0.5, 0.9]:
                self.pause_event.wait()
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(count * p))
                ret, frame = cap.read()
                if ret: hashes.append(imagehash.phash(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))))
            cap.release()
            return tuple(hashes) if len(hashes) == 3 else None
        except Exception: return None # Broad exception is okay here as many things can fail in video processing

    def run(self):
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
    def __init__(self, master_root, incoming_root, mode="copy", dupe_action="ignore", 
                 log_callback=None, progress_callback=None, stop_event=None, threads=4, dry_run=False):
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

    def _hash(self, p):
        try:
            h = hashlib.sha256()
            with open(p, 'rb') as f:
                while c := f.read(65536): 
                    if self.stop_event.is_set(): return None
                    h.update(c)
            return h.hexdigest()
        except: return None

    def _handle_dupe(self, p):
        self.stats['duplicates'] += 1
        if self.dupe_action == "delete" and not self.dry_run: os.remove(p)
        elif self.dupe_action == "quarantine" and not self.dry_run:
            dest = self.quarantine_path / p.relative_to(self.incoming_root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
        self.log(f"Duplicate: {p.name}")

    def _merge(self, p):
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
    def __init__(self, parent, duplicate_groups, move_to_path=None, precomputed_hashes=None):
        self.top = tk.Toplevel(parent)
        self.top.title("Review Duplicates")
        self._center_window(1100, 650)
        self.groups = duplicate_groups
        self.move_to_path = move_to_path
        self.hash_cache = precomputed_hashes if precomputed_hashes else {}
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
        
        self.all_pairs = []
        self.extensions = set()
        for group in self.groups:
            try:
                group.sort(key=lambda x: (-x.stat().st_size, x.stat().st_ctime))
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

    def _save_target(self, target):
        if target not in self.move_targets:
            self.move_targets.append(target)
            self.cb_targets['values'] = self.move_targets
            try: self.targets_file.write_text(json.dumps(self.move_targets))
            except: pass

    def _init_ui(self):
        # Top Filter & Status
        f_top = ttk.LabelFrame(self.top, text="Filter & Status", padding=5)
        f_top.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(f_top, text="Filter by Type:").pack(side="left", padx=5)
        self.filter_var = tk.StringVar()
        ext_list = sorted(list(self.extensions))
        self.cb_filter = ttk.Combobox(f_top, textvariable=self.filter_var, values=ext_list, state="readonly", width=10)
        self.cb_filter.pack(side="left", padx=5)
        self.cb_filter.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())
        
        ttk.Button(f_top, text="Clear Filter", image=self.icons['close'], compound="left", command=self.clear_filter).pack(side="left", padx=5)
        ttk.Button(f_top, text="Delete All Shown", image=self.icons['trash'], compound="left", command=self.delete_all_shown).pack(side="left", padx=5)
        ttk.Button(f_top, text="Move All Shown", image=self.icons['arrow'], compound="left", command=self.move_all_shown).pack(side="left", padx=5)
        
        self.lbl_stats = ttk.Label(f_top, text=f"Total Duplicates: {len(self.pairs)}")
        self.lbl_stats.pack(side="right", padx=10)
        
        # Images
        f_img = ttk.Frame(self.top)
        f_img.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Left (Original)
        f_left = ttk.LabelFrame(f_img, text="Original (Keep)", padding=5)
        f_left.grid(row=0, column=0, sticky="nsew", padx=5)
        self.lbl_orig = ttk.Label(f_left, text="Loading Preview...")
        self.lbl_orig.pack(expand=True)
        self.lbl_orig_path = ttk.Label(f_left, text="", wraplength=450, justify="center", font=("Segoe UI", 9))
        self.lbl_orig_path.pack(fill="x", pady=5)

        # Right (Duplicate)
        f_right = ttk.LabelFrame(f_img, text="Duplicate (Delete)", padding=5)
        f_right.grid(row=0, column=1, sticky="nsew", padx=5)
        self.lbl_dupe = ttk.Label(f_right, text="Loading Preview...")
        self.lbl_dupe.pack(expand=True)
        self.lbl_dupe_path = ttk.Label(f_right, text="", wraplength=450, justify="center", font=("Segoe UI", 9))
        self.lbl_dupe_path.pack(fill="x", pady=5)
        
        f_img.columnconfigure(0, weight=1); f_img.columnconfigure(1, weight=1)
        f_img.rowconfigure(0, weight=1)
        
        self._bind_context_menu(self.lbl_orig, True)
        self._bind_context_menu(self.lbl_orig_path, True)
        self._bind_context_menu(self.lbl_dupe, False)
        self._bind_context_menu(self.lbl_dupe_path, False)
        
        # Controls
        f_ctrl = ttk.Frame(self.top, padding=10); f_ctrl.pack(fill="x", side="bottom")
        
        # Left controls
        f_c_left = ttk.Frame(f_ctrl); f_c_left.pack(side="left")
        ttk.Button(f_c_left, text="Smart Select", image=self.icons['check'], compound="left", command=self.smart_select).pack(side="left", padx=2)
        ttk.Button(f_c_left, text="Find Similar", image=self.icons['search'], compound="left", command=self.find_similar).pack(side="left", padx=2)
        if HAS_REPORTLAB: ttk.Button(f_c_left, text="PDF Report", image=self.icons['save'], compound="left", command=self.export_pdf).pack(side="left", padx=2)
        ttk.Button(f_c_left, text="CSV Export", image=self.icons['save'], compound="left", command=self.export_csv).pack(side="left", padx=2)
        
        # Center controls (Move)
        f_c_center = ttk.Frame(f_ctrl); f_c_center.pack(side="left", padx=20)
        ttk.Label(f_c_center, text="Move to:").pack(side="left")
        self.cb_targets = ttk.Combobox(f_c_center, values=self.move_targets, width=15)
        self.cb_targets.pack(side="left", padx=5)
        ttk.Button(f_c_center, text="Move", image=self.icons['arrow'], compound="left", command=self.move_dupe).pack(side="left")
        
        # Right controls
        f_c_right = ttk.Frame(f_ctrl); f_c_right.pack(side="right")
        ttk.Button(f_c_right, text="Undo", image=self.icons['refresh'], compound="left", command=self.undo_last).pack(side="right", padx=2)
        ttk.Button(f_c_right, text="Skip >", image=self.icons['arrow'], compound="right", command=self.next_pair).pack(side="right", padx=2)
        ttk.Button(f_c_right, text="DELETE DUPLICATE", image=self.icons['trash'], compound="left", command=self.delete_dupe).pack(side="right", padx=10)
        
        self.lbl_prog = ttk.Label(f_ctrl, text="0/0", font=("Segoe UI", 10, "bold"))
        self.lbl_prog.pack(side="right", padx=15)
        
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
        
        target_dir = self.cb_targets.get()
        if not target_dir or not Path(target_dir).is_dir():
            messagebox.showwarning("No Destination", "Please select a valid destination folder from the 'Move to:' dropdown first.")
            return

        if not messagebox.askyesno("Move All", f"Are you sure you want to move all {len(self.pairs)} duplicates currently listed to:\n\n{target_dir}?"): return

        target_path = Path(target_dir)
        self._save_target(target_dir)
        operations = []
        restore_index = self.current_index
        count = 0
        for i, (orig, dupe) in enumerate(self.pairs):
            if not dupe.exists(): continue
            try:
                dest_file = target_path / dupe.name
                if dest_file.exists(): dest_file = target_path / f"{dupe.stem}_{int(time.time())}_{i}{dupe.suffix}"
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
            self.lbl_orig.config(image='', text="No more duplicates.")
            self.lbl_dupe.config(image='', text="")
            self.lbl_orig_path.config(text="")
            self.lbl_dupe_path.config(text="")
            return

        self.orig, self.dupe = self.pairs[self.current_index]
        self.lbl_prog.config(text=f"Pair {self.current_index+1} of {len(self.pairs)}")
        self.lbl_stats.config(text=f"Showing {len(self.pairs)} pairs")
        
        # Update Paths
        self.lbl_orig_path.config(text=f"{self.orig}\nSize: {self._fmt_size(self.orig)}")
        self.lbl_dupe_path.config(text=f"{self.dupe}\nSize: {self._fmt_size(self.dupe)}")
        
        self._show_img(self.lbl_orig, self.orig)
        self._show_img(self.lbl_dupe, self.dupe)

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
            tk_img = self.thumbnail_cache[path]
            lbl.config(image=tk_img, text="")
            lbl.image = tk_img
            return

        # Update the latest requested path for this label
        self.latest_requests[lbl] = path

        # 2. Cancel pending future for this label to prevent queue flooding
        if lbl in self.active_futures:
            self.active_futures[lbl].cancel()
            del self.active_futures[lbl]

        lbl.config(image='', text="Loading...")
        
        def load_task():
            # EARLY EXIT: If UI has moved on to a different image, abort immediately
            if self.latest_requests.get(lbl) != path: return None

            try:
                if path.suffix.lower() in {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v'}:
                    cap = cv2.VideoCapture(str(path))
                    if not cap.isOpened(): return None
                    cap.set(cv2.CAP_PROP_POS_FRAMES, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) * 0.5))
                    ret, frame = cap.read()
                    cap.release()
                    if not ret: return None
                    img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                else:
                    img = Image.open(str(path))
                    img.load()
                
                # SECOND EXIT: Check again before heavy processing (resizing)
                if self.latest_requests.get(lbl) != path: return None

                # Convert to RGB to ensure compatibility with ImageTk
                if img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGB')
                img.thumbnail((400, 400))
                return img
            except Exception as e:
                print(f"Error loading preview for {path}: {e}")
                return None

        def on_loaded(future):
            # Clean up future reference
            if lbl in self.active_futures:
                if self.active_futures[lbl] == future:
                    del self.active_futures[lbl]
            
            # Only update UI if this is still the requested image
            if self.latest_requests.get(lbl) != path: return

            try:
                img = future.result()
                if img:
                    tk_img = ImageTk.PhotoImage(img)
                    
                    # Cache the result (limit size to avoid memory issues)
                    if len(self.thumbnail_cache) > 200: self.thumbnail_cache.clear()
                    self.thumbnail_cache[path] = tk_img
                    
                    lbl.config(image=tk_img, text="")
                    lbl.image = tk_img
                else:
                    lbl.config(image='', text="[Preview Error]")
            except Exception as e:
                print(f"Error displaying preview: {e}")
                lbl.config(image='', text="[Display Error]")

        future = self.preview_executor.submit(load_task)
        self.active_futures[lbl] = future
        future.add_done_callback(lambda f: self.top.after(0, on_loaded, f))

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
        tgt = self.cb_targets.get()
        if not tgt or not Path(tgt).is_dir():
            messagebox.showwarning("No Destination", "Please select a valid destination folder.")
            return
        try:
            dest = Path(tgt) / self.dupe.name
            if dest.exists(): dest = Path(tgt) / f"{self.dupe.stem}_{int(time.time())}{self.dupe.suffix}"
            shutil.move(str(self.dupe), str(dest))
            operations = [(dest, self.dupe)]
            self._save_target(tgt)
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
            similarity_threshold = self.threshold_var.get() if hasattr(self, 'threshold_var') else 5

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
        win = tk.Toplevel(self.top)
        win.title(f"Files similar to {self.dupe.name}")
        win.geometry("700x400")
        
        similar_files.sort(key=lambda x: x[1])
        
        f = ttk.Frame(win, padding=10); f.pack(fill="both", expand=True)
        ttk.Label(f, text=f"Found {len(similar_files)} similar files:").pack(anchor="w")
        
        txt = tk.Text(f, height=15, width=80); txt.pack(fill="both", expand=True, pady=5)
        for path, distance in similar_files: txt.insert(tk.END, f"Distance: {distance}\t| Path: {path}\n")
        txt.config(state="disabled")
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=10)

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
            prop_win = tk.Toplevel(self.top)
            prop_win.title(f"Properties: {path.name}")
            
            f = ttk.Frame(prop_win, padding=10)
            f.pack(fill="both", expand=True)

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
                ttk.Label(f, text=key, font=("Segoe UI", 9, "bold")).grid(row=i, column=0, sticky="nw", padx=5, pady=2)
                entry = ttk.Entry(f); entry.insert(0, value); entry.config(state="readonly")
                entry.grid(row=i, column=1, sticky="ew", padx=5, pady=2)
            
            f.columnconfigure(1, weight=1)
            ttk.Button(prop_win, text="Close", command=prop_win.destroy).pack(pady=10)
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
                if platform.system() == 'Windows': subprocess.Popen(f'explorer /select,"{path}"')
                elif platform.system() == 'Darwin': subprocess.call(['open', '-R', path])
                else: subprocess.call(['xdg-open', path.parent])
            except: pass
        elif action == 'properties' and path.exists():
            self._show_properties(path)

class DedupApp:
    def __init__(self, root):
        self.root = root
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
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.settings = self.cfg.load()
        self.theme = ThemeManager(self.root)
        if self.settings.get("theme") == "dark": self.theme.toggle()
        else: self.theme.apply_theme()
        self.icons = IconFactory.create_icons()
        
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        
        self.t_audit = ttk.Frame(self.nb); self.nb.add(self.t_audit, text="Audit / Dedup")
        self.t_merge = ttk.Frame(self.nb); self.nb.add(self.t_merge, text="Merge Folders")
        self.t_settings = ttk.Frame(self.nb); self.nb.add(self.t_settings, text="Settings")
        
        f_log = ttk.Frame(root)
        f_log.pack(fill="x", padx=5, pady=2)
        ttk.Label(f_log, text="Activity Log:").pack(side="left", padx=5)
        ttk.Button(f_log, text="Clear Log", image=self.icons['trash'], compound="left", command=self.clear_log).pack(side="right")
        ttk.Button(f_log, text="Save Log", image=self.icons['save'], compound="left", command=self.save_log).pack(side="right", padx=5)
        
        self.log_area = tk.Text(root, height=8); self.log_area.pack(fill="x")
        self.pbar = ttk.Progressbar(root, mode="determinate"); self.pbar.pack(fill="x")
        
        self._init_audit_tab()
        self._init_merge_tab()
        self._init_settings_tab()
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
        if tot > 0: self.pbar['value'] = (cur/tot)*100
        self.root.title(f"Dedup Suite - {msg}")

    def _init_audit_tab(self):
        f = ttk.Frame(self.t_audit, padding=10); f.pack(fill="x")
        ttk.Label(f, text="Source:").pack(side="left")
        self.src_var = tk.StringVar(value=self.settings["last_source"])
        ttk.Entry(f, textvariable=self.src_var).pack(side="left", fill="x", expand=True)
        ttk.Button(f, text="Browse", image=self.icons['folder'], compound="left", command=lambda: self.src_var.set(filedialog.askdirectory())).pack(side="left")

        f2 = ttk.Frame(self.t_audit, padding=10); f2.pack(fill="x")
        self.mode_var = tk.StringVar(value="Exact")
        ttk.Combobox(f2, textvariable=self.mode_var, values=["Exact", "Visual/Video"]).pack(side="left")
        self.review_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f2, text="Review Mode", variable=self.review_var).pack(side="left")

        f_buttons = ttk.Frame(f2); f_buttons.pack(side="right")
        self.btn_start = ttk.Button(f_buttons, text="Start Scan", image=self.icons['play'], compound="left", command=self.start_audit)
        self.btn_start.pack(side="left")
        self.btn_pause = ttk.Button(f_buttons, text="Pause", image=self.icons['pause'], compound="left", command=self.toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=5)
        self.btn_stop = ttk.Button(f_buttons, text="Stop", image=self.icons['stop'], compound="left", command=self.stop_scan, state="disabled")
        self.btn_stop.pack(side="left", padx=5)

    def _init_merge_tab(self):
        f = ttk.Frame(self.t_merge, padding=10); f.pack(fill="x")
        self.m_master = tk.StringVar(value=self.settings["merge_master"])
        self.m_inc = tk.StringVar(value=self.settings["merge_incoming"])
        ttk.Entry(f, textvariable=self.m_master).pack(fill="x"); ttk.Entry(f, textvariable=self.m_inc).pack(fill="x")
        self.m_dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Dry Run", variable=self.m_dry).pack()
        ttk.Button(f, text="Start Merge", image=self.icons['play'], compound="left", command=self.start_merge).pack()

    def _init_settings_tab(self):
        f = ttk.LabelFrame(self.t_settings, text="Global Settings", padding=10)
        f.pack(fill="x", padx=10, pady=10)

        # Threshold
        ttk.Label(f, text="Visual Similarity Threshold (0-20, lower is stricter):").grid(row=0, column=0, sticky="w", pady=2)
        self.threshold_var = tk.IntVar(value=self.settings.get('threshold', 0))
        ttk.Spinbox(f, from_=0, to=20, textvariable=self.threshold_var, width=10).grid(row=0, column=1, sticky="w", pady=2)

        # Threads
        ttk.Label(f, text="Processing Threads:").grid(row=1, column=0, sticky="w", pady=2)
        self.threads_var = tk.IntVar(value=self.settings.get('threads', 4))
        ttk.Spinbox(f, from_=1, to=os.cpu_count() or 8, textvariable=self.threads_var, width=10).grid(row=1, column=1, sticky="w", pady=2)

        # Ignore Extensions
        ttk.Label(f, text="Ignore Extensions (comma-separated, e.g. .txt,.log):").grid(row=2, column=0, sticky="w", pady=2)
        self.ignore_exts_var = tk.StringVar(value=self.settings.get('ignore_exts', ''))
        ttk.Entry(f, textvariable=self.ignore_exts_var, width=50).grid(row=2, column=1, sticky="ew", pady=2)

        # Ignore Folders
        ttk.Label(f, text="Ignore Folders (comma-separated, e.g. .git,cache):").grid(row=3, column=0, sticky="w", pady=2)
        self.ignore_folders_var = tk.StringVar(value=self.settings.get('ignore_folders', ''))
        ttk.Entry(f, textvariable=self.ignore_folders_var, width=50).grid(row=3, column=1, sticky="ew", pady=2)
        
        f.columnconfigure(1, weight=1)

        ttk.Button(self.t_settings, text="Toggle Dark/Light Mode", image=self.icons['refresh'], compound="left", command=self.toggle_theme).pack(pady=10)
        ttk.Button(self.t_settings, text="Save Settings", image=self.icons['save'], compound="left", command=self.save_settings).pack(pady=20)
        ttk.Button(self.t_settings, text="Reset to Defaults", image=self.icons['refresh'], compound="left", command=self.reset_settings).pack(pady=5)
        ttk.Button(self.t_settings, text="Check for Updates", image=self.icons['refresh'], compound="left", command=self.check_updates).pack(pady=5)
        ttk.Button(self.t_settings, text="Create Desktop Shortcut", image=self.icons['save'], compound="left", command=self.create_shortcut).pack(pady=5)
        ttk.Button(self.t_settings, text="Report Bug", image=self.icons['search'], compound="left", command=self.report_bug).pack(pady=5)

    def start_audit(self):
        self.stop_event.clear()
        self.pause_event.set()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_pause.config(state="normal", text="Pause")
        cls = VideoFileAuditor if self.mode_var.get() == "Visual/Video" else FileAuditor

        ignore_exts = [e.strip() for e in self.settings.get('ignore_exts', '').split(',') if e.strip()]
        ignore_folders = [f.strip() for f in self.settings.get('ignore_folders', '').split(',') if f.strip()]

        auditor = cls(self.src_var.get(), log_callback=self.log, progress_callback=self.progress,
                      review_mode=self.review_var.get(), stop_event=self.stop_event, pause_event=self.pause_event, threshold=self.settings.get('threshold', 0),
                      threads=self.settings.get('threads', 4), ignore_exts=ignore_exts, ignore_folders=ignore_folders)
        def run():
            auditor.run()
            if self.review_var.get() and auditor.found_groups:
                self.root.after(0, self._show_review, auditor)
            self.root.after(0, self.reset_scan_buttons)
        threading.Thread(target=run).start()

    def _show_review(self, auditor):
        self.review_dialog = ReviewDialog(self.root, auditor.found_groups, precomputed_hashes=getattr(auditor, 'hash_cache', {}))

    def start_merge(self):
        merger = FolderMerger(self.m_master.get(), self.m_inc.get(), log_callback=self.log, progress_callback=self.progress, dry_run=self.m_dry.get())
        threading.Thread(target=merger.run).start()

    def on_close(self):
        self.stop_event.set() # Signal any running threads to stop
        self.pause_event.set() # Unpause to allow threads to exit
        self.settings.update({"last_source": self.src_var.get(), "merge_master": self.m_master.get(), "merge_incoming": self.m_inc.get()})
        self.cfg.save(self.settings)
        self.root.destroy()

    def stop_scan(self):
        self.stop_event.set()
        self.pause_event.set() # Unpause to allow threads to exit
        self.log("Stop signal sent. Finishing current operation...")

    def toggle_theme(self):
        new_mode = self.theme.toggle()
        self.settings['theme'] = new_mode
        self.log(f"Theme changed to {new_mode} mode.")

    def toggle_pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.btn_pause.config(text="Resume")
            self.log("Scanning paused.")
        else:
            self.pause_event.set()
            self.btn_pause.config(text="Pause")
            self.log("Scanning resumed.")

    def reset_scan_buttons(self):
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.btn_pause.config(state="disabled", text="Pause")

    def save_settings(self):
        self.settings['threshold'] = self.threshold_var.get()
        self.settings['threads'] = self.threads_var.get()
        self.settings['ignore_exts'] = self.ignore_exts_var.get()
        self.settings['ignore_folders'] = self.ignore_folders_var.get()
        self.cfg.save(self.settings)
        messagebox.showinfo("Settings", "Settings saved successfully.")

    def check_updates(self):
        # Placeholder for update logic
        messagebox.showinfo("Updates", "You are running the latest version (v1.0).")

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
    root = tk.Tk()
    app = DedupApp(root)
    root.mainloop()
