"""SQLite batch ingest for the sovraan concurrent pipeline.

Optimized bulk writes (WAL, deferred indexing, batched ``executemany``) aligned
with the production ``file_index`` schema used by :class:`~sovraan_core.DatabaseManager`.

Production vault export (hierarchical dated folders, synchronous cloud notary)
lives in :func:`export_golden_vault` and related helpers in this module.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Literal, Optional, Sequence, Tuple

from core.ots_proof import build_opentimestamps_proof

ExportMode = Literal["standard", "plm", "obsidian"]
EXPORT_MODES: tuple[str, ...] = ("standard", "plm", "obsidian")

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


TRIAL_GOLDEN_LIMIT = 1000

_FILE_INDEX_COLUMN_MIGRATIONS: tuple[str, ...] = (
    "last_session_id TEXT",
    "is_golden INTEGER DEFAULT 0",
    "classification TEXT",
    "status TEXT DEFAULT 'active'",
    "pre_archive_path TEXT",
    "archive_transaction_id INTEGER",
    "ots_proof BLOB",
)


class TrialLimitExceededError(Exception):
    """Raised when the Play-then-Pay trial cap on golden files is reached."""

    def __init__(
        self,
        message: str = "Trial limit of 1000 Golden Files reached.",
        *,
        inserted: int = 0,
        collected: Optional[List[Row]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.inserted = inserted
        self.collected = collected or []


def get_golden_file_count(db_conn: sqlite3.Connection) -> int:
    """Fast count of golden-classified rows in ``file_index``."""
    ensure_file_index_schema(db_conn)
    row = db_conn.execute(
        "SELECT COUNT(*) FROM file_index WHERE classification = 'golden'"
    ).fetchone()
    return int(row[0] if row else 0)


def assert_trial_capacity(
    db_conn: sqlite3.Connection,
    limit: int = TRIAL_GOLDEN_LIMIT,
) -> None:
    """No-op: golden-file trial cap removed (unlimited ingest)."""
    del db_conn, limit


def _classify_ingested_row(
    db_conn: sqlite3.Connection,
    full_path: str,
    file_hash: str,
) -> None:
    """Assign ``golden`` or ``legacy`` for a row just written during trial ingest."""
    has_golden = db_conn.execute(
        """
        SELECT 1 FROM file_index
        WHERE sha256_hash = ?
          AND classification = 'golden'
          AND full_path != ?
        LIMIT 1
        """,
        (file_hash, full_path),
    ).fetchone()
    if has_golden:
        db_conn.execute(
            """
            UPDATE file_index
            SET classification = 'legacy', is_golden = 0
            WHERE full_path = ?
            """,
            (full_path,),
        )
    else:
        db_conn.execute(
            """
            UPDATE file_index
            SET classification = 'golden', is_golden = 1
            WHERE full_path = ?
            """,
            (full_path,),
        )


def insert_batch_with_trial(
    conn: sqlite3.Connection,
    records: Sequence[Row],
    *,
    device_id: Optional[str],
    session_id: Optional[str],
    trial_limit: int = TRIAL_GOLDEN_LIMIT,
) -> int:
    """Upsert records one-by-one, classifying golden/legacy until the trial cap."""
    inserted = 0
    for path, record in records:
        if record.get("status") != "success":
            continue
        meta = record["metadata"]
        file_hash = record["hash"]
        full_path = str(path)
        cur = conn.execute(
            UPDATE_SQL,
            (
                file_hash,
                meta["size"],
                meta["mtime"],
                session_id,
                full_path,
            ),
        )
        if cur.rowcount == 0:
            conn.execute(
                INSERT_SQL,
                (
                    file_hash,
                    path.name,
                    meta["size"],
                    meta["mtime"],
                    full_path,
                    device_id,
                    session_id,
                ),
            )
        inserted += 1
        _classify_ingested_row(conn, full_path, file_hash)
    return inserted


def normalize_export_mode(mode: str) -> ExportMode:
    """Validate CLI / API export mode strings."""
    normalized = (mode or "standard").strip().lower()
    if normalized not in EXPORT_MODES:
        raise ValueError(
            f"export_mode must be one of {', '.join(EXPORT_MODES)} (got {mode!r})"
        )
    return normalized  # type: ignore[return-value]


def export_mode_writes_sidecars(mode: ExportMode) -> bool:
    """Return True when markdown sidecars should be written to the vault."""
    return mode in ("plm", "obsidian")


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
            columns.add(col)
    if "classification" in columns:
        conn.execute(
            """
            UPDATE file_index
            SET classification = 'golden'
            WHERE is_golden = 1
              AND (classification IS NULL OR classification = '')
            """
        )
        conn.execute(
            """
            UPDATE file_index
            SET classification = 'legacy'
            WHERE (is_golden = 0 OR is_golden IS NULL)
              AND sha256_hash IS NOT NULL
              AND (classification IS NULL OR classification = '')
            """
        )


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
class OtsStampResult:
    """Outcome of an OpenTimestamps stamp for one golden file."""

    proof_blob: Optional[bytes]
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.proof_blob)


@dataclass
class VaultExportResult:
    """Summary returned by :func:`export_golden_vault`."""

    export_mode: ExportMode = "standard"
    notes_written: int = 0
    assets_copied: int = 0
    proofs_persisted: int = 0
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


def resolve_notary_status(ots: OtsStampResult) -> str:
    """Map OTS stamp outcome to vault frontmatter status."""
    return "ANCHORED" if ots.ok else "PENDING"


def stamp_ots_proof(file_hash: str) -> OtsStampResult:
    """Build an OpenTimestamps proof for ``file_hash`` (never written to disk)."""
    proof_blob, error = build_opentimestamps_proof(file_hash)
    return OtsStampResult(proof_blob=proof_blob, error=error)


def persist_file_index_ots_proof(
    conn: sqlite3.Connection,
    row: GoldenExportRow,
    proof_blob: Optional[bytes],
) -> None:
    """Store the raw OTS proof bytes on the golden ``file_index`` row."""
    conn.execute(
        """
        UPDATE file_index
        SET ots_proof = ?
        WHERE full_path = ?
          AND sha256_hash = ?
          AND is_golden = 1
        """,
        (sqlite3.Binary(proof_blob) if proof_blob else None, str(row.full_path), row.file_hash),
    )


def render_vault_note(
    row: GoldenExportRow,
    *,
    ots: OtsStampResult,
    created_iso: str,
    embed_name: str = "",
) -> str:
    """Build markdown sidecar for PLM/Obsidian export (proof lives in SQLite only)."""
    status = resolve_notary_status(ots)
    proof_bytes = len(ots.proof_blob) if ots.proof_blob else 0
    frontmatter = (
        "---\n"
        f'file_hash: "{row.file_hash}"\n'
        f'original_path: "{_yaml_escape(str(row.full_path))}"\n'
        f'original_name: "{_yaml_escape(row.file_name)}"\n'
        f'created: "{created_iso}"\n'
        f'notary_status: "{status}"\n'
        f"ots_proof_bytes: {proof_bytes}\n"
        f'ots_proof_storage: "file_index.ots_proof"\n'
        "---\n"
    )
    body = (
        f"\n# {row.file_name}\n\n"
        f"- **SHA-256:** `{row.file_hash}`\n"
        f"- **Notary:** {status}\n"
        f"- **OTS proof:** stored in forensic ledger ({proof_bytes} bytes)\n"
    )
    if ots.error and not ots.ok:
        body += f"- **OTS error:** `{_yaml_escape(ots.error)}`\n"
    if embed_name:
        body += f"\n![[{embed_name}]]\n"
    return frontmatter + body


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


def export_golden_file(
    export_paths: ExportPaths,
    row: GoldenExportRow,
    *,
    export_mode: ExportMode,
    persist_ots: Callable[[GoldenExportRow, Optional[bytes]], None],
    log: Callable[[str], None] = print,
) -> tuple[OtsStampResult, bool, bool]:
    """Stamp OTS into SQLite, copy the source asset, optionally write a sidecar."""
    day = extract_creation_date(row.full_path, row.modified_time)
    if day is not None:
        created_iso = datetime.datetime.combine(
            day, datetime.time.min, tzinfo=datetime.timezone.utc
        ).isoformat()
    else:
        created_iso = "unknown"

    ots = stamp_ots_proof(row.file_hash)
    persist_ots(row, ots.proof_blob)

    asset_copied = False
    if export_paths.extension and export_paths.embed_name:
        try:
            copy_source_asset(row.full_path, export_paths.asset_path)
            asset_copied = True
        except OSError as exc:
            log(f"Vault export: asset copy failed for {row.full_path}: {exc}")

    note_written = False
    if export_mode_writes_sidecars(export_mode) and export_paths.extension:
        export_paths.note_path.parent.mkdir(parents=True, exist_ok=True)
        export_paths.note_path.write_text(
            render_vault_note(
                row,
                ots=ots,
                created_iso=created_iso,
                embed_name=export_paths.embed_name if asset_copied else "",
            ),
            encoding="utf-8",
        )
        note_written = True

    return ots, asset_copied, note_written


def export_golden_vault(
    db_path: Path,
    session_id: str,
    vault_root: Path,
    *,
    hierarchical: bool = True,
    export_mode: ExportMode | str = "standard",
    log: Callable[[str], None] = print,
) -> VaultExportResult:
    """Export golden files into the vault (assets only or assets + sidecars).

    OpenTimestamps proofs are stamped during export and persisted exclusively
    in ``file_index.ots_proof`` — no ``.ots`` files are written to the vault.
    """
    mode = normalize_export_mode(export_mode) if isinstance(export_mode, str) else export_mode

    vault_root = vault_root.resolve()
    vault_root.mkdir(parents=True, exist_ok=True)
    result = VaultExportResult(export_mode=mode)

    conn = sqlite3.connect(db_path)
    try:
        configure_connection(conn)
        rows = fetch_golden_rows(conn, session_id)
        if not rows:
            log("Vault export: no golden files for this session.")
            return result

        layout = "hierarchical dated tree" if hierarchical else "flat vault root"
        log(
            f"Vault export ({mode}): {len(rows)} golden file(s) → {vault_root} ({layout})"
        )

        def _persist_ots(row: GoldenExportRow, proof_blob: Optional[bytes]) -> None:
            persist_file_index_ots_proof(conn, row, proof_blob)
            conn.commit()
            if proof_blob:
                result.proofs_persisted += 1

        for row in rows:
            paths = resolve_export_paths(vault_root, row, hierarchical=hierarchical)
            paths = _disambiguate_export_paths(paths, row.file_hash)
            ots, asset_copied, note_written = export_golden_file(
                paths,
                row,
                export_mode=mode,
                persist_ots=_persist_ots,
                log=log,
            )
            if note_written:
                result.notes_written += 1
                result.note_paths.append(paths.note_path)
            if asset_copied:
                result.assets_copied += 1
                result.asset_paths.append(paths.asset_path)
            if ots.ok:
                result.anchored += 1
            else:
                result.pending += 1

        log(
            f"Vault export: mode={mode}, assets={result.assets_copied}, "
            f"sidecars={result.notes_written}, ots_in_db={result.proofs_persisted}; "
            f"ANCHORED={result.anchored}, PENDING={result.pending}"
        )
    finally:
        conn.close()

    return result
