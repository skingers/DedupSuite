"""Database schema extension helpers for blockchain proof tracking."""

from __future__ import annotations

import sqlite3
import time
from typing import Optional


def ensure_blockchain_schema(db_path: str, *, retries: int = 3, retry_delay: float = 0.5) -> bool:
    """Ensure the ``blockchain_proofs`` table exists in the target database.

    Opens a short-lived connection, enables foreign keys, and creates the
    table if absent. Transient ``database is locked``/``busy`` errors are
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
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blockchain_proofs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_hash TEXT UNIQUE NOT NULL,
                    ots_proof_blob BLOB,
                    status TEXT DEFAULT 'PENDING',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
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
