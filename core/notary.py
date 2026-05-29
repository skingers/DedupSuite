"""Production OpenTimestamps batch notary processor."""

from __future__ import annotations

import sqlite3
import sys
from typing import List


class DedupNotary:
    """Submit pending file hashes to OpenTimestamps and persist their proofs.

    Attributes:
        db_path: Filesystem path to the SQLite ledger holding the active
            ``file_index`` and ``blockchain_proofs`` tables.
    """

    def __init__(self, db_path: str) -> None:
        """Initialise the notary.

        Args:
            db_path: Path to the SQLite database to read hashes from and write
                proofs back into.
        """
        self.db_path = db_path

    def batch_submit_unnotarised(self) -> None:
        """Notarise every file hash that is missing or still ``PENDING``.

        Selects distinct ``sha256_hash`` values from the active ``file_index``
        table that have no row in ``blockchain_proofs`` or whose status is
        ``PENDING``, computes an OpenTimestamps proof for each, and upserts the
        serialized proof blob with status ``SUBMITTED``. Per-hash failures are
        logged and skipped so a single bad asset cannot abort the batch.

        Returns:
            None. Progress and failures are reported to standard error.
        """
        conn: sqlite3.Connection | None = None

        try:
            conn = sqlite3.connect(self.db_path, timeout=15.0)
            conn.execute("PRAGMA foreign_keys = ON;")

            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT DISTINCT fi.sha256_hash
                FROM file_index AS fi
                LEFT JOIN blockchain_proofs AS bp ON bp.file_hash = fi.sha256_hash
                WHERE fi.sha256_hash IS NOT NULL
                  AND (bp.file_hash IS NULL OR bp.status = 'PENDING')
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
                except Exception as exc:
                    # Keep batch processing resilient: one failing hash (network
                    # timeout, calendar error, serialisation issue) must not stop
                    # the loop. ``Exception`` already covers ``TimeoutError``.
                    print(f"[NOTARY] Failed for {file_hash}: {exc}", file=sys.stderr)
                    continue
        except sqlite3.OperationalError as exc:
            print(f"[NOTARY] SQLite operational error: {exc}", file=sys.stderr)
        finally:
            if conn is not None:
                conn.close()
