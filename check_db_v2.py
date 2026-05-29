"""Database schema extension helpers for blockchain proof tracking."""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

_BLOCKCHAIN_PROOFS_DDL = """
    CREATE TABLE IF NOT EXISTS blockchain_proofs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_hash TEXT UNIQUE NOT NULL,
        ots_proof_blob BLOB,
        status TEXT DEFAULT 'PENDING',
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
"""


def _migrate_legacy_blockchain_proofs(conn: sqlite3.Connection) -> None:
    """Rebuild a legacy ``blockchain_proofs`` table that still references ``files``.

    Early builds created ``blockchain_proofs`` with
    ``FOREIGN KEY(file_hash) REFERENCES files(hash)``. The ``files`` table was
    subsequently dropped in favour of ``file_index``; with foreign keys
    enforced, any insert then fails with ``no such table: main.files``. This
    rebuilds the table without the dangling constraint while preserving every
    existing row, and is a no-op on the current (FK-free) schema or a fresh
    database.

    The connection must be in autocommit mode (``isolation_level = None``) so
    the ``PRAGMA`` toggles take effect outside an implicit transaction.

    Args:
        conn: Open SQLite connection to the target database.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='blockchain_proofs'"
    ).fetchone()
    if not exists:
        return
    if not conn.execute("PRAGMA foreign_key_list(blockchain_proofs)").fetchall():
        return  # already on the current, FK-free schema

    conn.execute("PRAGMA foreign_keys = OFF;")
    conn.execute("BEGIN;")
    try:
        conn.execute(
            """
            CREATE TABLE blockchain_proofs_migrated (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash TEXT UNIQUE NOT NULL,
                ots_proof_blob BLOB,
                status TEXT DEFAULT 'PENDING',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO blockchain_proofs_migrated
                (id, file_hash, ots_proof_blob, status, updated_at)
            SELECT id, file_hash, ots_proof_blob, status, updated_at
            FROM blockchain_proofs
            """
        )
        conn.execute("DROP TABLE blockchain_proofs")
        conn.execute("ALTER TABLE blockchain_proofs_migrated RENAME TO blockchain_proofs")
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise


def ensure_blockchain_schema(db_path: str, *, retries: int = 3, retry_delay: float = 0.5) -> bool:
    """Ensure the ``blockchain_proofs`` table exists in the target database.

    Opens a short-lived connection, migrates any legacy table that still
    references the removed ``files`` table, enables foreign keys, and creates
    the table if absent. Transient ``database is locked``/``busy`` errors are
    retried with a fixed delay; other operational errors fail gracefully.

    Args:
        db_path: Path to the SQLite database file.
        retries: Maximum number of attempts when the database is locked/busy.
        retry_delay: Seconds to wait between retry attempts.

    Returns:
        ``True`` if the schema is present after the call, ``False`` if every
        attempt failed.
    """
    conn: Optional[sqlite3.Connection] = None

    for attempt in range(1, retries + 1):
        try:
            conn = sqlite3.connect(db_path, timeout=10.0)
            # Autocommit mode so PRAGMA toggles and the rebuild DDL apply cleanly.
            conn.isolation_level = None
            _migrate_legacy_blockchain_proofs(conn)
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute(_BLOCKCHAIN_PROOFS_DDL)
            return True
        except sqlite3.OperationalError as exc:
            # Busy/locked DBs are retried; other operational failures are surfaced as False.
            message = str(exc).lower()
            if ("database is locked" in message or "database is busy" in message) and attempt < retries:
                time.sleep(retry_delay)
                continue
            return False
        finally:
            if conn is not None:
                conn.close()
                conn = None

    return False
