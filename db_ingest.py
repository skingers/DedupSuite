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


def configure_connection(conn: sqlite3.Connection) -> None:
    """Apply ingest-oriented PRAGMAs on a dedicated writer connection."""
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")


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
