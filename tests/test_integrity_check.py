"""Unit tests for Ed25519 batch signature verification."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import db_ingest
from integrity_check import IntegrityCheck


def _insert_signed_batch(
    conn: sqlite3.Connection,
    *,
    batch_index: int,
    manifest: dict,
    signature: bytes,
    public_key: bytes,
) -> None:
    db_ingest.insert_batch_signature(
        conn,
        batch_index=batch_index,
        manifest_json=json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        signature=signature,
        public_key=public_key,
        row_count=len(manifest.get("entries", [])),
        created_at=manifest["signed_at"],
    )


def test_integrity_check_verifies_valid_batch(tmp_path: Path) -> None:
    db_path = tmp_path / "mine.db"
    gate = MagicMock()
    gate.public_key_bytes = b"\x01" * 32
    gate.verify_key = MagicMock()
    gate.verify_manifest.return_value = True

    manifest = {
        "signed_at": 1.0,
        "entries": [{"path": "/a.txt", "hash": "ab" * 32}],
    }
    conn = sqlite3.connect(db_path)
    try:
        db_ingest.configure_connection(conn)
        _insert_signed_batch(
            conn,
            batch_index=0,
            manifest=manifest,
            signature=b"sig",
            public_key=gate.public_key_bytes,
        )
        conn.commit()
    finally:
        conn.close()

    checker = IntegrityCheck(db_path, crypto_gate=gate)
    valid, total, failed = checker.verify_all()
    assert total == 1
    assert valid == 1
    assert failed == []
    assert checker.scan() is True


def test_integrity_check_detects_tampered_manifest(tmp_path: Path) -> None:
    db_path = tmp_path / "mine.db"
    gate = MagicMock()
    gate.public_key_bytes = b"\x02" * 32
    gate.verify_key = MagicMock()
    gate.verify_manifest.return_value = False

    manifest = {
        "signed_at": 2.0,
        "entries": [{"path": "/b.txt", "hash": "cd" * 32}],
    }
    conn = sqlite3.connect(db_path)
    try:
        db_ingest.configure_connection(conn)
        _insert_signed_batch(
            conn,
            batch_index=0,
            manifest=manifest,
            signature=b"bad",
            public_key=gate.public_key_bytes,
        )
        conn.commit()
    finally:
        conn.close()

    checker = IntegrityCheck(db_path, crypto_gate=gate)
    valid, total, failed = checker.verify_all()
    assert total == 1
    assert valid == 0
    assert failed == [0]
    assert checker.scan() is False
