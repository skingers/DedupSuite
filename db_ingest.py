"""SQLite batch ingest for the DedupSuite concurrent pipeline.

Optimized bulk writes (WAL, deferred indexing, batched ``executemany``) aligned
with the production ``file_index`` schema used by :class:`~dedup_suite.DatabaseManager`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

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


def ensure_signatures_schema(conn: sqlite3.Connection) -> None:
    """Ensure the Ed25519 batch signature ledger table exists."""
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
    """Apply ingest-oriented PRAGMAs on a dedicated writer connection."""
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    ensure_signatures_schema(conn)


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
