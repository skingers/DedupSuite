"""Tests for concurrent ingest pipeline and golden promotion."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import db_ingest
from pipeline import run_pipeline
from sovraan_core import DatabaseManager


def test_run_pipeline_inserts_and_identify_golden(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    (source / "b.txt").write_text("alpha", encoding="utf-8")

    db_path = tmp_path / "data_mine.db"
    session_id = "sess-pipeline-test"

    gate = MagicMock()
    gate.build_batch_manifest.return_value = {
        "signed_at": 1.0,
        "entries": [{"path": "x", "hash": "y" * 32}],
    }
    gate.sign_manifest.return_value = b"\x00" * 64
    gate.public_key_bytes = b"\x03" * 32

    with patch("pipeline.get_crypto_gate", return_value=gate):
        _duration, inserted, collected = run_pipeline(
            [source / "a.txt", source / "b.txt"],
            db_path,
            session_id=session_id,
        )

    assert inserted >= 2
    assert len(collected) == 2

    manager = DatabaseManager(db_path=db_path)
    try:
        stats = manager.identify_golden_versions(session_id=session_id)
        assert stats["golden"] == 1
        assert stats["legacy"] == 1

        conn = sqlite3.connect(db_path)
        try:
            golden = conn.execute(
                "SELECT COUNT(*) FROM file_index WHERE is_golden = 1 AND last_session_id = ?",
                (session_id,),
            ).fetchone()[0]
            legacy = conn.execute(
                "SELECT COUNT(*) FROM file_index WHERE is_golden = 0 AND last_session_id = ?",
                (session_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert golden == 1
        assert legacy == 1
    finally:
        manager.close()
