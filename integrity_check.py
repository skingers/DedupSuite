"""Integrity verification for signed ingest batches in sovraan."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

from crypto_gate import CryptoGate, get_crypto_gate

BatchRow = Tuple[int, str, bytes, bytes, int, float]


class IntegrityCheck:
    """Verify SQLite ``batch_signatures`` rows against Ed25519 manifests."""

    def __init__(
        self,
        db_path: Path,
        *,
        crypto_gate: Optional[CryptoGate] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._gate = crypto_gate if crypto_gate is not None else get_crypto_gate()

    def fetch_signatures(self, conn: sqlite3.Connection) -> List[BatchRow]:
        """Load all stored batch signatures ordered by ``batch_index``."""
        cur = conn.execute(
            """
            SELECT batch_index, manifest_json, signature, public_key,
                   row_count, created_at
            FROM batch_signatures
            ORDER BY batch_index ASC
            """
        )
        return list(cur.fetchall())

    def verify_batch_row(self, row: BatchRow) -> bool:
        """Validate one ``batch_signatures`` row."""
        _batch_index, manifest_json, signature, public_key, _row_count, _created = row
        manifest = json.loads(manifest_json)
        verify_key = self._gate.verify_key
        if public_key and bytes(public_key) != self._gate.public_key_bytes:
            from nacl.signing import VerifyKey

            verify_key = VerifyKey(bytes(public_key))
        return self._gate.verify_manifest(manifest, bytes(signature), verify_key=verify_key)

    def verify_all(self) -> Tuple[int, int, List[int]]:
        """Verify every signature in the database.

        Returns:
            ``(valid_count, total_count, failed_batch_indices)``
        """
        if not self.db_path.exists():
            return 0, 0, []

        conn = sqlite3.connect(self.db_path)
        try:
            rows = self.fetch_signatures(conn)
        except sqlite3.OperationalError:
            return 0, 0, []
        finally:
            conn.close()

        failed: List[int] = []
        valid = 0
        for row in rows:
            if self.verify_batch_row(row):
                valid += 1
            else:
                failed.append(row[0])

        return valid, len(rows), failed

    def scan(self) -> bool:
        """Run a full integrity scan; return True only if all batches verify."""
        valid, total, failed = self.verify_all()
        if total == 0:
            return True
        return valid == total and not failed
