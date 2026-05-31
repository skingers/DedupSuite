"""SQLite batch ingest for the DedupSuite concurrent pipeline.

Optimized bulk writes (WAL, deferred indexing, batched ``executemany``) aligned
with the production ``file_index`` schema used by :class:`~dedup_suite.DatabaseManager`.

Production vault export (hierarchical dated folders, synchronous cloud notary)
lives in :func:`export_golden_vault` and related helpers in this module.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

from network.notary_bridge import AnchorResult, CloudNotaryBridge

Row = Tuple[Path, dict]

UPDATE_SQL = """
    UPDATE file_index
    SET sha256_hash = ?, file_size = ?, modified_time = ?, last_session_id = ?
    WHERE full_path = ?
"""

INSERT_SQL = """
    INSERT INTO file_index (
        sha256_hash, phash, file_name, file_size, modified_time,
        full_path, device_id, last_session_id
    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
"""

INSERT_SIGNATURE_SQL = """
    INSERT INTO batch_signatures (
        batch_index, manifest_json, signature, public_key, row_count, created_at
    ) VALUES (?, ?, ?, ?, ?, ?)
"""


_FILE_INDEX_COLUMN_MIGRATIONS: tuple[str, ...] = (
    "last_session_id TEXT",
    "is_golden INTEGER DEFAULT 0",
    "status TEXT DEFAULT 'active'",
    "pre_archive_path TEXT",
    "archive_transaction_id INTEGER",
)


def ensure_file_index_schema(conn: sqlite3.Connection) -> None:
    """Ensure production ``devices`` + ``file_index`` tables exist (empty-safe)."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            device_name TEXT,
            last_seen DATETIME
        )
        """
    )
    conn.execute(
        """
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
        """
    )
    cur = conn.execute("PRAGMA table_info(file_index)")
    columns = {row[1] for row in cur.fetchall()}
    for spec in _FILE_INDEX_COLUMN_MIGRATIONS:
        col = spec.split()[0]
        if col not in columns:
            conn.execute(f"ALTER TABLE file_index ADD COLUMN {spec}")


def ensure_signatures_schema(conn: sqlite3.Connection) -> None:
    """Ensure the Ed25519 batch signature ledger (``batch_signatures``) exists."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batch_signatures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_index INTEGER NOT NULL,
            manifest_json TEXT NOT NULL,
            signature BLOB NOT NULL,
            public_key BLOB NOT NULL,
            row_count INTEGER NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )


def configure_connection(conn: sqlite3.Connection) -> None:
    """Apply ingest PRAGMAs and bootstrap an empty production schema if needed."""
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    ensure_file_index_schema(conn)
    ensure_signatures_schema(conn)
    conn.commit()


def register_device(
    conn: sqlite3.Connection,
    device_id: str,
    device_name: Optional[str] = None,
) -> None:
    """Ensure ``device_id`` exists in ``devices`` (required for ``file_index`` FK writes)."""
    if not device_id:
        return
    conn.execute(
        "INSERT OR IGNORE INTO devices (device_id, device_name, last_seen) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)",
        (device_id, device_name or device_id),
    )


def initialize_database(db_path: Path) -> None:
    """Create or open ``db_path`` and ensure a fresh, empty production schema."""
    conn = sqlite3.connect(db_path)
    try:
        configure_connection(conn)
    finally:
        conn.close()


def finalize_index(conn: sqlite3.Connection) -> None:
    """Ensure the sha256 lookup index exists after bulk insert."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_index_sha256 ON file_index (sha256_hash)"
    )
    conn.commit()


def insert_batch(
    conn: sqlite3.Connection,
    records: Sequence[Row],
    *,
    device_id: Optional[str],
    session_id: Optional[str],
) -> int:
    """Upsert a batch of kernel records into ``file_index``."""
    inserted = 0
    pending: list[tuple[Any, ...]] = []

    for path, record in records:
        if record.get("status") != "success":
            continue
        meta = record["metadata"]
        cur = conn.execute(
            UPDATE_SQL,
            (
                record["hash"],
                meta["size"],
                meta["mtime"],
                session_id,
                str(path),
            ),
        )
        if cur.rowcount == 0:
            pending.append(
                (
                    record["hash"],
                    path.name,
                    meta["size"],
                    meta["mtime"],
                    str(path),
                    device_id,
                    session_id,
                )
            )
        else:
            inserted += 1

    if pending:
        conn.executemany(INSERT_SQL, pending)
        inserted += len(pending)

    return inserted


def insert_batch_signature(
    conn: sqlite3.Connection,
    *,
    batch_index: int,
    manifest_json: str,
    signature: bytes,
    public_key: bytes,
    row_count: int,
    created_at: float,
) -> None:
    """Persist an Ed25519 signature for a committed ingest batch."""
    conn.execute(
        INSERT_SIGNATURE_SQL,
        (
            batch_index,
            manifest_json,
            sqlite3.Binary(signature),
            sqlite3.Binary(public_key),
            row_count,
            created_at,
        ),
    )


# ---------------------------------------------------------------------------
# Production vault export (hierarchical layout + synchronous notary)
# ---------------------------------------------------------------------------

_SAFE_STEM = re.compile(r"[^0-9A-Za-z._-]+")
_MIN_VALID_YEAR = 1980


@dataclass
class GoldenExportRow:
    """One golden ``file_index`` row ready for vault export."""

    full_path: Path
    file_hash: str
    file_name: str
    modified_time: Optional[float]

    @property
    def original_path(self) -> Path:
        """Source asset path (``file_index.full_path``)."""
        return self.full_path


@dataclass
class VaultExportResult:
    """Summary returned by :func:`export_golden_vault`."""

    notes_written: int = 0
    assets_copied: int = 0
    anchored: int = 0
    pending: int = 0
    note_paths: List[Path] = field(default_factory=list)
    asset_paths: List[Path] = field(default_factory=list)


@dataclass
class ExportPaths:
    """Resolved destination paths for a golden file export."""

    folder: Path
    chronological_basename: str
    extension: str
    note_path: Path
    asset_path: Path
    embed_name: str


def _sanitise_stem(name: str) -> str:
    cleaned = _SAFE_STEM.sub("_", name).strip("._")
    return cleaned or "untitled"


def _short_hash(text: str, length: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def extract_creation_date(
    source_path: Path,
    modified_time: Optional[float],
) -> Optional[datetime.date]:
    """Derive a calendar date from ingest metadata or filesystem timestamps.

    Returns ``None`` when metadata is missing or clearly invalid/corrupt.
    """
    candidates: List[float] = []
    if modified_time is not None:
        try:
            candidates.append(float(modified_time))
        except (TypeError, ValueError):
            pass

    if source_path.exists():
        try:
            stat = source_path.stat()
            candidates.extend(
                (
                    getattr(stat, "st_birthtime", stat.st_ctime),
                    stat.st_ctime,
                    stat.st_mtime,
                )
            )
        except OSError:
            pass

    today = datetime.date.today()
    for ts in candidates:
        try:
            day = datetime.datetime.fromtimestamp(ts).date()
        except (OSError, OverflowError, ValueError):
            continue
        if day.year < _MIN_VALID_YEAR or day > today + datetime.timedelta(days=1):
            continue
        return day
    return None


def hierarchical_folder(vault_root: Path, day: datetime.date) -> Path:
    """``vault_root/YYYY/YYYY-MM-DD/`` dated directory."""
    year = f"{day.year:04d}"
    dated = day.strftime("%Y-%m-%d")
    return vault_root / year / dated


def build_note_basename(
    original_stem: str,
    *,
    day: Optional[datetime.date],
    file_hash: str,
) -> str:
    """``YYYY-MM-DD-[Original_Name]`` or ``Depth_Hash-[Original_Name]`` fallback."""
    stem = _sanitise_stem(original_stem)
    if day is not None:
        return f"{day.strftime('%Y-%m-%d')}-{stem}"
    depth_hash = _short_hash(file_hash)
    return f"Depth_{depth_hash}-{stem}"


def extract_original_extension(row: GoldenExportRow) -> str:
    """Extension from ``file_index.full_path`` (original asset), including the dot."""
    for candidate in (row.full_path, Path(row.file_name)):
        suffix = candidate.suffix
        if suffix:
            return suffix.lower()
    return ""


def resolve_export_folder(
    vault_root: Path,
    row: GoldenExportRow,
    *,
    hierarchical: bool,
    day: Optional[datetime.date],
) -> Path:
    if hierarchical and day is not None:
        return hierarchical_folder(vault_root, day)
    return vault_root


def resolve_export_paths(
    vault_root: Path,
    row: GoldenExportRow,
    *,
    hierarchical: bool,
) -> ExportPaths:
    """Compute paired ``.md`` and raw asset paths sharing one chronological basename."""
    day = extract_creation_date(row.full_path, row.modified_time)
    chronological_basename = build_note_basename(
        row.full_path.stem or Path(row.file_name).stem,
        day=day,
        file_hash=row.file_hash,
    )
    extension = extract_original_extension(row)
    folder = resolve_export_folder(vault_root, row, hierarchical=hierarchical, day=day)
    asset_filename = f"{chronological_basename}{extension}" if extension else ""
    return ExportPaths(
        folder=folder,
        chronological_basename=chronological_basename,
        extension=extension,
        note_path=folder / f"{chronological_basename}.md",
        asset_path=folder / asset_filename if asset_filename else folder / chronological_basename,
        embed_name=asset_filename,
    )


def resolve_note_path(
    vault_root: Path,
    row: GoldenExportRow,
    *,
    hierarchical: bool,
) -> Path:
    """Compute the final ``.md`` path under the vault (dated tree or flat root)."""
    return resolve_export_paths(vault_root, row, hierarchical=hierarchical).note_path


def _disambiguate_export_paths(paths: ExportPaths, file_hash: str) -> ExportPaths:
    """Avoid collisions by suffixing both note and asset when either path exists."""
    if not paths.note_path.exists() and not paths.asset_path.exists():
        return paths
    suffix = _short_hash(f"{paths.note_path}|{paths.asset_path}|{file_hash}")
    basename = f"{paths.chronological_basename}_{suffix}"
    extension = paths.extension
    asset_filename = f"{basename}{extension}" if extension else basename
    return ExportPaths(
        folder=paths.folder,
        chronological_basename=basename,
        extension=extension,
        note_path=paths.folder / f"{basename}.md",
        asset_path=paths.folder / asset_filename,
        embed_name=asset_filename if extension else "",
    )


def copy_source_asset(source: Path, destination: Path) -> None:
    """Copy the golden source binary into the vault dated folder."""
    if not source.is_file():
        raise FileNotFoundError(f"Source asset was not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def resolve_notary_status(outcome: AnchorResult) -> str:
    """Map gateway outcome to vault frontmatter status."""
    if outcome.ok:
        return "ANCHORED"
    return "PENDING"


def _format_receipt_yaml(outcome: AnchorResult) -> str:
    receipt = {
        "gateway_status": outcome.status,
        "attempts": outcome.attempts,
        "payload": outcome.payload,
        "response": outcome.response,
        "error": outcome.error,
    }
    lines = ["notary_receipt:"]
    for key, value in receipt.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            encoded = json.dumps(value, sort_keys=True)
            lines.append(f"  {key}: {encoded}")
        else:
            lines.append(f'  {key}: "{_yaml_escape(str(value))}"')
    return "\n".join(lines)


def render_vault_note(
    row: GoldenExportRow,
    *,
    outcome: AnchorResult,
    created_iso: str,
    embed_name: str = "",
) -> str:
    """Build markdown with resolved notary status and gateway receipt block."""
    status = resolve_notary_status(outcome)
    frontmatter = (
        "---\n"
        f'file_hash: "{row.file_hash}"\n'
        f'original_path: "{_yaml_escape(str(row.full_path))}"\n'
        f'original_name: "{_yaml_escape(row.file_name)}"\n'
        f'created: "{created_iso}"\n'
        f'notary_status: "{status}"\n'
        f"{_format_receipt_yaml(outcome)}\n"
        "---\n"
    )
    body = (
        f"\n# {row.file_name}\n\n"
        f"- **SHA-256:** `{row.file_hash}`\n"
        f"- **Notary:** {status}\n"
    )
    if outcome.ok and outcome.response is not None:
        body += f"- **Gateway response:** `{json.dumps(outcome.response, sort_keys=True)}`\n"
    if embed_name:
        body += f"\n![[{embed_name}]]\n"
    return frontmatter + body


def anchor_hash_synchronously(
    file_hash: str,
    *,
    bridge: Optional[CloudNotaryBridge] = None,
    client_timestamp: Optional[str] = None,
) -> AnchorResult:
    """Await a single Cloud Notary gateway receipt before writing the note."""
    gate = bridge if bridge is not None else CloudNotaryBridge()
    return gate.anchor(file_hash, client_timestamp=client_timestamp)


def _persist_blockchain_proof(
    conn: sqlite3.Connection,
    file_hash: str,
    *,
    status: str,
) -> None:
    conn.execute(
        """
        INSERT INTO blockchain_proofs (file_hash, status, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(file_hash) DO UPDATE SET
            status = excluded.status,
            updated_at = CURRENT_TIMESTAMP
        """,
        (file_hash, status),
    )


def fetch_golden_rows(
    conn: sqlite3.Connection,
    session_id: str,
) -> List[GoldenExportRow]:
    """Load golden files for a session from ``file_index``."""
    cur = conn.execute(
        """
        SELECT full_path, sha256_hash, file_name, modified_time
        FROM file_index
        WHERE is_golden = 1
          AND sha256_hash IS NOT NULL
          AND last_session_id = ?
        ORDER BY full_path ASC
        """,
        (session_id,),
    )
    rows: List[GoldenExportRow] = []
    for full_path, sha256_hash, file_name, modified_time in cur.fetchall():
        rows.append(
            GoldenExportRow(
                full_path=Path(full_path),
                file_hash=str(sha256_hash),
                file_name=str(file_name or Path(full_path).name),
                modified_time=modified_time,
            )
        )
    return rows


def write_vault_note(
    export_paths: ExportPaths,
    row: GoldenExportRow,
    *,
    bridge: Optional[CloudNotaryBridge] = None,
    persist_proof: Optional[Callable[[str, str], None]] = None,
    log: Callable[[str], None] = print,
) -> tuple[AnchorResult, bool]:
    """Anchor via gateway, copy the source asset, then write the markdown sidecar."""
    day = extract_creation_date(row.full_path, row.modified_time)
    if day is not None:
        created_iso = datetime.datetime.combine(
            day, datetime.time.min, tzinfo=datetime.timezone.utc
        ).isoformat()
    else:
        created_iso = "unknown"

    outcome = anchor_hash_synchronously(
        row.file_hash,
        bridge=bridge,
        client_timestamp=created_iso if created_iso != "unknown" else None,
    )

    asset_copied = False
    if export_paths.extension and export_paths.embed_name:
        try:
            copy_source_asset(row.full_path, export_paths.asset_path)
            asset_copied = True
        except OSError as exc:
            log(f"Vault export: asset copy failed for {row.full_path}: {exc}")

    export_paths.note_path.parent.mkdir(parents=True, exist_ok=True)
    export_paths.note_path.write_text(
        render_vault_note(
            row,
            outcome=outcome,
            created_iso=created_iso,
            embed_name=export_paths.embed_name if asset_copied else "",
        ),
        encoding="utf-8",
    )
    proof_status = "SUBMITTED" if outcome.ok else "PENDING"
    if persist_proof is not None:
        persist_proof(row.file_hash, proof_status)
    return outcome, asset_copied


def export_golden_vault(
    db_path: Path,
    session_id: str,
    vault_root: Path,
    *,
    hierarchical: bool = True,
    log: Callable[[str], None] = print,
    bridge: Optional[CloudNotaryBridge] = None,
) -> VaultExportResult:
    """Export golden files into the vault with optional dated folders.

    Each note is written only after the Cloud Notary gateway returns (or
    exhausts retries). Frontmatter ``notary_status`` is ``ANCHORED`` on success
    or ``PENDING`` when anchoring fails.
    """
    from check_db_v2 import ensure_blockchain_schema

    vault_root = vault_root.resolve()
    vault_root.mkdir(parents=True, exist_ok=True)
    ensure_blockchain_schema(str(db_path))

    gate = bridge if bridge is not None else CloudNotaryBridge(logger=log)
    result = VaultExportResult()

    conn = sqlite3.connect(db_path)
    try:
        rows = fetch_golden_rows(conn, session_id)
        if not rows:
            log("Vault export: no golden files for this session.")
            return result

        layout = "hierarchical dated tree" if hierarchical else "flat vault root"
        log(f"Vault export: {len(rows)} golden file(s) → {vault_root} ({layout})")

        def _persist(hash_value: str, status: str) -> None:
            _persist_blockchain_proof(conn, hash_value, status=status)
            conn.commit()

        for row in rows:
            paths = resolve_export_paths(vault_root, row, hierarchical=hierarchical)
            paths = _disambiguate_export_paths(paths, row.file_hash)
            outcome, asset_copied = write_vault_note(
                paths,
                row,
                bridge=gate,
                persist_proof=_persist,
                log=log,
            )
            result.notes_written += 1
            result.note_paths.append(paths.note_path)
            if asset_copied:
                result.assets_copied += 1
                result.asset_paths.append(paths.asset_path)
            if outcome.ok:
                result.anchored += 1
            else:
                result.pending += 1

        log(
            f"Vault export: wrote {result.notes_written} note(s), "
            f"copied {result.assets_copied} asset(s); "
            f"ANCHORED={result.anchored}, PENDING={result.pending}"
        )
    finally:
        conn.close()

    return result
