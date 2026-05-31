# DedupSuite 2.0

DedupSuite is a production-ready, desktop-first deduplication and audit platform that combines high-throughput local scanning with cryptographic integrity guarantees and optional cloud notary anchoring. The application analyses file estates, indexes results in SQLite, identifies duplicate content, and supports controlled archival workflows through a calm, operator-focused GUI built with CustomTkinter.

## Core Capabilities

- **Exact deduplication** — SHA-256 content hashing for byte-identical duplicate detection.
- **Visual / video matching** — Perceptual hashing for media-oriented review paths.
- **Session-aware indexing** — SQLite ledger with golden/legacy classification across audit sessions.
- **Cryptographic integrity** — Ed25519-signed batch manifests for every ingest commit.
- **Cloud notary anchoring** — Background OpenTimestamps submission via `core/notary.py`.
- **Unrestricted production ingest** — No trial caps on golden-file classification or discovery; the concurrent pipeline processes entire directory trees.

---

## Calm Journey Interface (GUI)

DedupSuite 2.0 ships a **Calm Journey** minimalist workflow in `dedup_suite.py`, designed for long scanning sessions on Windows hosts with display scaling:

| Tab | Purpose |
|---|---|
| **Your Journey** | Three-step guided audit: map source folder, monitor rescue progress, choose export mode, and run **Begin Rescue** / Pause / Stop. |
| **Merge Folders** | Consolidate incoming trees into a master archive. |
| **Expert Studio** | Advanced scanner tuning (exact vs. visual/video, thresholds, ignore rules). |
| **The Vault Index** | Golden/legacy statistics, rationalisation, and bulk archive controls. |

**Step 3 — Ignite Your Mind** uses a native `CTkSegmentedButton` bound to `journey_export_mode` with values `Standard Mode` and `Intelligence Mode`. This replaces earlier custom-drawn mode cards for stable layout management.

**Window architecture**

- Minimum size: **1100 × 700** pixels — preserves taskbar clearance on scaled Windows displays.
- After UI construction, maximization is applied via deferred OS state: `root.after(100, lambda: root.state('zoomed'))`.

The journey layout uses tight vertical spacing constants (`JOURNEY_PADY`, `STEP3_RUN_GAP`) so controls, the background activity log, and progress bar remain visible when maximized.

---

## Quick Start

```bash
# 1. Clone and enter the repository
git clone https://gitlab.com/skingers/DedupSuite.git
cd DedupSuite

# 2. Install dependencies
pip install -r requirements.txt

# 3. Launch the GUI
python dedup_suite.py
```

In the application, open **Your Journey**, choose a source folder in Step 1, select **Standard Mode** or **Intelligence Mode** in Step 3, and click **Begin Rescue**. Configure exact vs. visual/video scanning under **Expert Studio** when needed. Results are written to `data_mine.db` in the application directory. Runtime artefacts (`data_mine.db`, `logs/`, `settings.json`) are gitignored and created on first use.

### Headless pipeline smoke test

For a non-GUI ingest and integrity verification against a directory tree:

```python
import sqlite3
from pathlib import Path

from db_ingest import configure_connection
from integrity_check import IntegrityCheck
from pipeline import run_pipeline

DATA_DIR = Path("/path/to/your/files")
DB_PATH = Path("stress_ingest.db")

# Bootstrap production schema (devices FK + file_index)
conn = sqlite3.connect(DB_PATH)
configure_connection(conn)
conn.executescript("""
    CREATE TABLE IF NOT EXISTS devices (
        device_id TEXT PRIMARY KEY,
        device_name TEXT,
        last_seen DATETIME
    );
    CREATE TABLE IF NOT EXISTS file_index (
        sha256_hash TEXT,
        phash TEXT,
        file_name TEXT,
        file_size INTEGER,
        modified_time REAL,
        full_path TEXT,
        is_golden_version INTEGER DEFAULT 0,
        device_id TEXT,
        last_session_id TEXT,
        FOREIGN KEY(device_id) REFERENCES devices(device_id)
    );
    INSERT OR IGNORE INTO devices (device_id, device_name) VALUES ('stress-device', 'stress-device');
""")
conn.commit()
conn.close()

paths = [p for p in DATA_DIR.rglob("*") if p.is_file() and not p.name.startswith(".")]
duration, inserted, collected = run_pipeline(
    paths,
    DB_PATH,
    device_id="stress-device",
    session_id="stress-session",
)
print(f"Ingested {inserted} files in {duration:.2f}s")

checker = IntegrityCheck(DB_PATH)
valid, total, failed = checker.verify_all()
print(f"Integrity: {valid}/{total} batches valid; failed indices: {failed}")
assert checker.scan(), "Integrity scan failed"
```

---

## The Sovereign Stack

DedupSuite 2.0 is built around a **Sovereign Stack** — local-first processing where trust, throughput, and auditability remain under operator control.

### Layer 1 — Concurrent Pipeline (Producer / Consumer)

The ingest path (`pipeline.py` + `ingest_kernel.py`) separates CPU-bound hashing from I/O-bound database writes:

```
┌─────────────────────────────────────────────────────────────┐
│  Producer (ThreadPoolExecutor, 8 workers)                   │
│  • Walks file list                                          │
│  • Streams SHA-256 via readinto() — flat memory footprint   │
│  • Enqueues batches of 64 (path, record) tuples             │
└──────────────────────────┬──────────────────────────────────┘
                           │ bounded Queue (max 32)
┌──────────────────────────▼──────────────────────────────────┐
│  Consumer (dedicated SQLite writer connection)              │
│  • Builds Ed25519 batch manifest                            │
│  • Upserts rows into file_index                             │
│  • Persists signature into batch_signatures                 │
│  • Single commit per batch (WAL-safe)                       │
└─────────────────────────────────────────────────────────────┘
```

**Design goals:** maximise throughput on multi-core hosts, keep the GUI responsive during large audits, and ensure every database commit is atomic with its cryptographic proof.

**Production ingest policy:** `run_pipeline()` defaults to `trial_golden_limit=None`, routing all database-backed audits through the concurrent producer/consumer path with `insert_batch`. There is no artificial stop at 1,000 golden files and no discovery testing cap — `FileAuditor` and `VideoFileAuditor` walk the full target tree (subject only to user Stop/Pause and ignore rules).

#### Production vault export, forensic ledger & export profiles

`run_production.py` ingests source files, classifies golden copies, stamps OpenTimestamps proofs, and projects assets into the destination vault. The SQLite database (`data_mine.db` or a path passed via `--db`) is the **definitive forensic registry**: Ed25519 batch manifests, golden/legacy classification, and per-file **`file_index.ots_proof`** BLOB columns. **No loose `.ots` files are written to the vault** — binary proofs live only in the database.

With `--tree-mode` / `--hierarchical` (default), trustworthy creation metadata produces a dated folder tree (`YYYY/YYYY-MM-DD/`) and filenames:

`YYYY-MM-DD-[Original_Name].[ext]`

**Export mode** (`--export-mode`) selects the vault surface area:

| Mode | CLI | Vault contents |
|---|---|---|
| **Standard** (default) | `--export-mode standard` | Physical source assets only (`.png`, `.pdf`, `.docx`, …) in the chronological tree. **No** Markdown sidecars or other text clutter. |
| **PLM / Obsidian** | `--export-mode plm` or `--export-mode obsidian` | Same asset tree **plus** clean Markdown sidecars with YAML frontmatter and Obsidian `![[asset]]` embeds, ready for LLM tokenisation or knowledge-graph use. |

```bash
python run_production.py \
  --source "D:\Archive\Raw" \
  --destination "D:\Obsidian\MyVault" \
  --db "D:\DedupSuite\data_mine.db" \
  --tree-mode \
  --export-mode standard
```

##### Chronological Naming Fallback Protocol

When ingest metadata is **missing or corrupted** (common on legacy optical media, truncated EXIF, zeroed timestamps, or pre-1980 epoch values), DedupSuite **must not invent a calendar date**. The pipeline switches to a deterministic, content-anchored fallback stem:

`[Folder Depth Level]_[Unique Content SHA-256 Hash]`

| Token | Meaning |
|---|---|
| **Folder Depth Level** | Non-negative index into the source folder graph (`0`, `01`, `1`, …). `0` is the scan root; each additional path segment increments depth. |
| **Unique Content SHA-256 Hash** | Short, stable prefix of the file's SHA-256 digest (e.g. `0ef1fdf7`), guaranteeing identity even when display names collide. |

**Example fallback basename:** `0_0ef1fdf7` → a golden file at scan-root depth whose hash prefix is `0ef1fdf7`.

**Why this exists**

- **Prevents false timestamping** — no synthetic `YYYY-MM-DD` labels that would misfile assets in the vault timeline.
- **Safeguards data** — every object remains addressable, deduplicable, and notarisable by content hash.
- **Preserves auditability** — operators can distinguish metadata-blackout exports from true chronological exports at a glance.

**Implementation note:** `db_ingest.build_note_basename()` emits vault-safe stems as `Depth_{hash8}-[Original_Name]` when `extract_creation_date()` returns `None`. The `Depth_` prefix and hyphenated original stem are filesystem/Obsidian normalisation; the depth index and hash tokens above remain the canonical logical identity. See `docs/TDA.md` for the formal resolution framework.

### Layer 2 — Ed25519 Cryptographic Integrity Layer

After each batch is hashed, the consumer signs a canonical JSON manifest (`crypto_gate.py`):

| Manifest field | Purpose |
|---|---|
| `signed_at` | Unix timestamp of signing |
| `entries[].file_hash` | SHA-256 hex digest |
| `entries[].timestamp` | Source file mtime |
| `entries[].full_path` | Absolute path at ingest time |

**Key management:** Ed25519 keypairs are generated once and stored in the OS credential vault via `keyring` (service namespace `DedupSuite`). Private seeds never touch disk or version control.

**Persistence:** Each signature is stored in the `batch_signatures` table alongside the canonical manifest JSON, raw signature bytes, public key, row count, and creation timestamp — all within the same SQLite transaction as the file rows.

---

## How to Audit

Use `integrity_check.py` to verify that stored batch signatures match the Ed25519 key in your OS vault.

### CLI verification

```bash
python -c "
from pathlib import Path
from integrity_check import IntegrityCheck

db = Path('data_mine.db')
checker = IntegrityCheck(db)
valid, total, failed = checker.verify_all()
print(f'Valid batches: {valid}/{total}')
if failed:
    print(f'Failed batch indices: {failed}')
print('PASS' if checker.scan() else 'FAIL')
"
```

### What the audit checks

1. **Signature validity** — Each row in `batch_signatures` is re-verified against its stored manifest using Ed25519.
2. **Key consistency** — Signatures signed with a different public key are verified against the stored `public_key` blob.
3. **Completeness** — Compare `SUM(row_count)` across `batch_signatures` with the count of indexed rows in `file_index` for the session.

A passing scan (`IntegrityCheck.scan() == True`) confirms that every committed ingest batch has a valid, untampered cryptographic proof.

### Vault verification notes

- Keys live in the OS credential manager under service name **`DedupSuite`**.
- Rotating keys requires re-signing; historical batches retain the signing public key in `batch_signatures.public_key`.
- If verification fails, inspect `failed_batch_indices` and compare manifest entries against live filesystem state.

---

## Repository Layout

| Path | Role |
|---|---|
| `dedup_suite.py` | Calm Journey GUI, `FileAuditor` / `VideoFileAuditor`, and orchestration |
| `pipeline.py` | Producer/consumer concurrent ingest |
| `ingest_kernel.py` | Per-file streaming SHA-256 worker |
| `db_ingest.py` | SQLite batch writes, vault export, and naming fallback |
| `run_production.py` | Production ingest, `--export-mode`, hierarchical vault export |
| `docs/TDA.md` | Technical Design Architecture (naming, OTS BLOB, export profiles) |
| `crypto_gate.py` | Ed25519 signing gate (OS vault backed) |
| `integrity_check.py` | On-demand signature verification |
| `core/ots_proof.py` | OpenTimestamps proof generation (in-memory → SQLite) |
| `core/` | Notary worker, Markdown export, Obsidian bridge sources |
| `obsidian-plugin/` | DedupSuite 2.0 Obsidian integration (TypeScript + built `main.js`) |
| `production_config.py` | Absolute-path validation for production CLI |
| `network/` | Remote notary bridge |
| `tests/` | Unit and integration tests |

See `TECHNICAL_ARCHITECTURE.md` for deployment topology, presentation-layer constraints, security boundaries, and operational protocols.

---

## Deployment Overview

DedupSuite uses a hybrid local-plus-cloud flow:

- **Local host** runs `dedup_suite.py`, performs scanning/hashing, and writes to `data_mine.db`.
- **Background notary worker** (`DedupNotary`) submits pending hashes asynchronously after audit completion.
- **Remote Cloud Notary** endpoint: `http://34.13.47.2:5000/api/v1/anchor`

The GUI remains responsive while background anchoring continues.

---

## Repository Notes

- Keep runtime artefacts untracked (`data_mine.db`, logs, cache files).
- Use `main` as the production branch.
- Do not commit local secrets, private manifests, or host-specific scratch assets.
- Build a standalone executable with `build_exe.bat` (PyInstaller spec: `dedup_suite.spec`).
