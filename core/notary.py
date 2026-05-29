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

    #: Public OpenTimestamps calendar pool used to aggregate proofs.
    CALENDAR_URLS = (
        "https://a.pool.opentimestamps.org",
        "https://b.pool.opentimestamps.org",
        "https://a.pool.eternitywall.com",
        "https://ots.btc.catallaxy.com",
    )
    #: Per-calendar network timeout, in seconds.
    CALENDAR_TIMEOUT = 10

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
                from opentimestamps.core.op import OpSHA256
                from opentimestamps.core.timestamp import Timestamp, DetachedTimestampFile
                from opentimestamps.core.serialize import BytesSerializationContext
                from opentimestamps.calendar import RemoteCalendar
            except ImportError as exc:  # pragma: no cover - depends on runtime install state
                print(f"[NOTARY] OpenTimestamps import failed: {exc}", file=sys.stderr)
                return

            def _build_proof(hash_bytes: bytes) -> bytes:
                """Stamp a pre-computed SHA-256 digest and return the OTS proof.

                Builds a detached timestamp from the raw 32-byte digest, submits
                it to the public calendar pool (merging every successful reply),
                and serialises the resulting proof. Raises if no calendar accepts
                the timestamp so the caller can keep the hash ``PENDING``.
                """
                detached = DetachedTimestampFile(OpSHA256(), Timestamp(hash_bytes))
                submitted = False
                for url in self.CALENDAR_URLS:
                    try:
                        calendar = RemoteCalendar(url)
                        calendar_timestamp = calendar.submit(
                            detached.timestamp.msg, timeout=self.CALENDAR_TIMEOUT
                        )
                        detached.timestamp.merge(calendar_timestamp)
                        submitted = True
                    except Exception as cal_exc:
                        print(f"[NOTARY] Calendar {url} unavailable: {cal_exc}", file=sys.stderr)
                        continue
                if not submitted:
                    raise RuntimeError("no calendar accepted the timestamp")
                ctx = BytesSerializationContext()
                detached.serialize(ctx)
                return ctx.getbytes()

            for file_hash in target_hashes:
                try:
                    hash_bytes = bytes.fromhex(file_hash)
                    proof_blob = _build_proof(hash_bytes)

                    # Never persist a blank proof under a 'SUBMITTED' status; an
                    # empty serialisation means the timestamp carries no calendar
                    # attestations and would be useless for later verification.
                    if not proof_blob:
                        raise ValueError("empty OpenTimestamps proof; refusing to persist")

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
