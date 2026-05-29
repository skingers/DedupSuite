"""Mock-network tests for the notary submission flow and schema retry logic.

These tests exercise the real network-facing behaviour that exists in the
codebase today:

* ``DedupNotary.batch_submit_unnotarised`` against a mocked OpenTimestamps
  backend (selection of unnotarised/pending hashes + resilience when the
  remote aggregation is unreachable / times out), and
* ``ensure_blockchain_schema`` retry/back-off when the database is locked.
"""

from __future__ import annotations

import sqlite3
import types
from pathlib import Path
from typing import Callable, List

import pytest

import check_db_v2
from check_db_v2 import ensure_blockchain_schema
from core.notary import DedupNotary

VALID_HASH_A = "a" * 64
VALID_HASH_B = "b" * 64
VALID_HASH_C = "c" * 64


def _build_db(path: Path, files: List[str]) -> None:
    """Create the active ``file_index`` + ``blockchain_proofs`` tables and seed hashes."""
    assert ensure_blockchain_schema(str(path)) is True
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS file_index (sha256_hash TEXT, full_path TEXT)"
        )
        conn.executemany(
            "INSERT INTO file_index (sha256_hash, full_path) VALUES (?, ?)",
            [(h, f"/seed/{h}.bin") for h in files],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_proof(path: Path, file_hash: str, status: str) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO blockchain_proofs (file_hash, status) VALUES (?, ?)",
            (file_hash, status),
        )
        conn.commit()
    finally:
        conn.close()


def _proof_status(path: Path, file_hash: str) -> str | None:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT status FROM blockchain_proofs WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


@pytest.fixture()
def fake_opentimestamps(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable], None]:
    """Install a fake ``opentimestamps`` package mirroring the real API surface.

    Mocks ``opentimestamps.core.op.OpSHA256``,
    ``opentimestamps.core.timestamp.{Timestamp,DetachedTimestampFile}``,
    ``opentimestamps.core.serialize.BytesSerializationContext`` and
    ``opentimestamps.calendar.RemoteCalendar``. Returns a setter for the
    calendar-submit behaviour; the behaviour callable receives the raw digest
    bytes and may raise to simulate an unreachable calendar / network timeout.
    """
    import sys

    def _set_behaviour(behaviour: Callable[[bytes], None]) -> None:
        class _OpSHA256:
            DIGEST_LENGTH = 32

        class _Timestamp:
            def __init__(self, msg: bytes) -> None:
                self.msg = msg

            def merge(self, other: "_Timestamp") -> None:
                # Real Timestamp merges calendar attestations; no-op for tests.
                pass

        class _DetachedTimestampFile:
            def __init__(self, file_hash_op, timestamp: _Timestamp) -> None:
                self.file_hash_op = file_hash_op
                self.timestamp = timestamp

            def serialize(self, ctx) -> None:
                ctx.write_bytes(b"OTS:" + self.timestamp.msg)

        class _BytesSerializationContext:
            def __init__(self) -> None:
                self._buf = bytearray()

            def write_bytes(self, data: bytes) -> None:
                self._buf.extend(data)

            def getbytes(self) -> bytes:
                return bytes(self._buf)

        class _RemoteCalendar:
            def __init__(self, url: str) -> None:
                self.url = url

            def submit(self, digest: bytes, timeout=None) -> _Timestamp:
                behaviour(digest)  # may raise to simulate an unreachable calendar
                return _Timestamp(digest)

        op_module = types.ModuleType("opentimestamps.core.op")
        op_module.OpSHA256 = _OpSHA256
        ts_module = types.ModuleType("opentimestamps.core.timestamp")
        ts_module.Timestamp = _Timestamp
        ts_module.DetachedTimestampFile = _DetachedTimestampFile
        serialize_module = types.ModuleType("opentimestamps.core.serialize")
        serialize_module.BytesSerializationContext = _BytesSerializationContext
        calendar_module = types.ModuleType("opentimestamps.calendar")
        calendar_module.RemoteCalendar = _RemoteCalendar
        core_module = types.ModuleType("opentimestamps.core")
        ots_module = types.ModuleType("opentimestamps")

        for name, module in {
            "opentimestamps": ots_module,
            "opentimestamps.core": core_module,
            "opentimestamps.core.op": op_module,
            "opentimestamps.core.timestamp": ts_module,
            "opentimestamps.core.serialize": serialize_module,
            "opentimestamps.calendar": calendar_module,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)

    return _set_behaviour


def test_submits_only_missing_and_pending(tmp_path: Path, fake_opentimestamps) -> None:
    db = tmp_path / "mine.db"
    _build_db(db, [VALID_HASH_A, VALID_HASH_B, VALID_HASH_C])
    _seed_proof(db, VALID_HASH_B, "PENDING")
    _seed_proof(db, VALID_HASH_C, "SUBMITTED")

    fake_opentimestamps(lambda hb: None)  # all succeed
    DedupNotary(str(db)).batch_submit_unnotarised()

    assert _proof_status(db, VALID_HASH_A) == "SUBMITTED"   # was missing
    assert _proof_status(db, VALID_HASH_B) == "SUBMITTED"   # was pending
    assert _proof_status(db, VALID_HASH_C) == "SUBMITTED"   # untouched, already submitted


def test_proof_blob_is_persisted(tmp_path: Path, fake_opentimestamps) -> None:
    db = tmp_path / "mine.db"
    _build_db(db, [VALID_HASH_A])
    fake_opentimestamps(lambda hb: None)

    DedupNotary(str(db)).batch_submit_unnotarised()

    conn = sqlite3.connect(str(db))
    try:
        blob = conn.execute(
            "SELECT ots_proof_blob FROM blockchain_proofs WHERE file_hash = ?",
            (VALID_HASH_A,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert bytes(blob) == b"OTS:" + bytes.fromhex(VALID_HASH_A)


def test_each_blob_is_paired_with_its_own_hash(tmp_path: Path, fake_opentimestamps) -> None:
    """Every persisted proof must belong to the hash it was generated from."""
    db = tmp_path / "mine.db"
    _build_db(db, [VALID_HASH_A, VALID_HASH_B])
    fake_opentimestamps(lambda hb: None)

    DedupNotary(str(db)).batch_submit_unnotarised()

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT file_hash, ots_proof_blob FROM blockchain_proofs"
        ).fetchall()
    finally:
        conn.close()

    stored = {file_hash: bytes(blob) for file_hash, blob in rows}
    assert stored == {
        VALID_HASH_A: b"OTS:" + bytes.fromhex(VALID_HASH_A),
        VALID_HASH_B: b"OTS:" + bytes.fromhex(VALID_HASH_B),
    }


def test_batch_survives_unreachable_server_timeout(tmp_path: Path, fake_opentimestamps) -> None:
    """A timeout on one hash must not crash the batch or mark it submitted."""
    db = tmp_path / "mine.db"
    _build_db(db, [VALID_HASH_A, VALID_HASH_B])
    _seed_proof(db, VALID_HASH_A, "PENDING")
    _seed_proof(db, VALID_HASH_B, "PENDING")

    def behaviour(hash_bytes: bytes) -> None:
        if hash_bytes == bytes.fromhex(VALID_HASH_A):
            raise TimeoutError("calendar server unreachable")

    fake_opentimestamps(behaviour)

    # Must not raise even though one asset times out.
    DedupNotary(str(db)).batch_submit_unnotarised()

    assert _proof_status(db, VALID_HASH_A) == "PENDING"     # failed -> unchanged
    assert _proof_status(db, VALID_HASH_B) == "SUBMITTED"   # succeeded


def test_no_targets_exits_cleanly(tmp_path: Path, fake_opentimestamps) -> None:
    db = tmp_path / "mine.db"
    _build_db(db, [VALID_HASH_A])
    _seed_proof(db, VALID_HASH_A, "SUBMITTED")

    def behaviour(hash_bytes: bytes) -> None:  # pragma: no cover - must never run
        raise AssertionError("calendar submit should not be called")

    fake_opentimestamps(behaviour)
    DedupNotary(str(db)).batch_submit_unnotarised()  # nothing to do, no error


def _make_flaky_connect(real_connect, fail_times: int, exc: Exception):
    """Return a connect() that raises ``exc`` ``fail_times`` times, then works."""
    state = {"calls": 0}

    def _connect(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise exc
        return real_connect(*args, **kwargs)

    return _connect, state


def test_schema_retries_when_locked_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "retry.db"
    real_connect = sqlite3.connect
    flaky, state = _make_flaky_connect(
        real_connect, fail_times=2, exc=sqlite3.OperationalError("database is locked")
    )
    monkeypatch.setattr(check_db_v2.sqlite3, "connect", flaky)
    monkeypatch.setattr(check_db_v2.time, "sleep", lambda _s: None)

    assert ensure_blockchain_schema(str(db), retries=3, retry_delay=0) is True
    assert state["calls"] == 3


def test_schema_returns_false_after_exhausting_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "locked.db"
    flaky, state = _make_flaky_connect(
        sqlite3.connect, fail_times=99, exc=sqlite3.OperationalError("database is locked")
    )
    monkeypatch.setattr(check_db_v2.sqlite3, "connect", flaky)
    monkeypatch.setattr(check_db_v2.time, "sleep", lambda _s: None)

    assert ensure_blockchain_schema(str(db), retries=3, retry_delay=0) is False
    assert state["calls"] == 3


def test_schema_does_not_retry_on_non_lock_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "fatal.db"
    flaky, state = _make_flaky_connect(
        sqlite3.connect, fail_times=99, exc=sqlite3.OperationalError("no such column")
    )
    monkeypatch.setattr(check_db_v2.sqlite3, "connect", flaky)

    assert ensure_blockchain_schema(str(db), retries=3, retry_delay=0) is False
    assert state["calls"] == 1  # non-transient error -> no retry
