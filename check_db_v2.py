"""Database schema extension helpers for blockchain proof tracking."""

from __future__ import annotations

import sqlite3
import time
from typing import Optional


def ensure_blockchain_schema(db_path: str, *, retries: int = 3, retry_delay: float = 0.5) -> bool:
    """Ensure the blockchain_proofs table exists in the target SQLite database.

    Returns True when schema creation succeeds, otherwise False.
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
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(file_hash) REFERENCES files(hash)
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
