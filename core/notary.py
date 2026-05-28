"""Production OpenTimestamps batch notary processor."""

from __future__ import annotations

import sqlite3
import sys
from typing import List


class DedupNotary:
    """Submit pending file hashes to OpenTimestamps and persist proofs."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def batch_submit_unnotarised(self) -> None:
        """Submit unnotarised or pending hashes and store serialized OTS proofs."""
        conn: sqlite3.Connection | None = None

        try:
            conn = sqlite3.connect(self.db_path, timeout=15.0)
            conn.execute("PRAGMA foreign_keys = ON;")

            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT DISTINCT f.hash
                FROM files AS f
                LEFT JOIN blockchain_proofs AS bp ON bp.file_hash = f.hash
                WHERE bp.file_hash IS NULL OR bp.status = 'PENDING'
                """
            )
            target_hashes: List[str] = [row[0] for row in cursor.fetchall() if row and row[0]]

            if not target_hashes:
                return

            try:
                import opentimestamps
                import opentimestamps.timestamp as ots_timestamp
            except Exception as exc:  # pragma: no cover - depends on runtime install state
                print(f"[NOTARY] OpenTimestamps import failed: {exc}", file=sys.stderr)
                return

            for file_hash in target_hashes:
                try:
                    hash_bytes = bytes.fromhex(file_hash)
                    timestamp = ots_timestamp.Timestamp.from_hash(hash_bytes)
                    opentimestamps.create_timestamp(timestamp, open_services=True)
                    proof_blob = timestamp.serialize()

                    with conn:
                        conn.execute(
                            """
                            INSERT INTO blockchain_proofs (
                                file_hash,
                                ots_proof_blob,
                                status,
                                updated_at
                            )
                            VALUES (?, ?, 'SUBMITTED', CURRENT_TIMESTAMP)
                            ON CONFLICT(file_hash) DO UPDATE SET
                                ots_proof_blob = excluded.ots_proof_blob,
                                status = 'SUBMITTED',
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (file_hash, sqlite3.Binary(proof_blob)),
                        )

                    print(f"[NOTARY] SUBMITTED proof for {file_hash}", file=sys.stderr)
                except (TimeoutError, Exception) as exc:
                    # Keep batch processing resilient: one failing hash must not stop the loop.
                    print(f"[NOTARY] Failed for {file_hash}: {exc}", file=sys.stderr)
                    continue
        except sqlite3.OperationalError as exc:
            print(f"[NOTARY] SQLite operational error: {exc}", file=sys.stderr)
        finally:
            if conn is not None:
                conn.close()
