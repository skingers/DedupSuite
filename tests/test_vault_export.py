"""Tests for hierarchical vault export helpers in db_ingest."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

from db_ingest import (
    GoldenExportRow,
    OtsStampResult,
    build_note_basename,
    configure_connection,
    copy_source_asset,
    export_golden_file,
    export_golden_vault,
    export_mode_writes_sidecars,
    extract_creation_date,
    extract_original_extension,
    hierarchical_folder,
    normalize_export_mode,
    persist_file_index_ots_proof,
    render_vault_note,
    resolve_export_paths,
    resolve_note_path,
)


def test_hierarchical_folder_path() -> None:
    root = Path("/vault")
    day = datetime.date(2024, 3, 15)
    assert hierarchical_folder(root, day) == Path("/vault/2024/2024-03-15")


def test_dated_filename_prefix() -> None:
    day = datetime.date(2024, 3, 15)
    assert build_note_basename("My Report", day=day, file_hash="a" * 64) == "2024-03-15-My_Report"


def test_depth_hash_fallback_basename() -> None:
    name = build_note_basename("scan", day=None, file_hash="ab" * 32)
    assert name.startswith("Depth_")
    assert name.endswith("-scan")


def test_resolve_note_path_hierarchical() -> None:
    day_ts = datetime.datetime(2022, 6, 1, 12, 0, 0).timestamp()
    row = GoldenExportRow(
        full_path=Path("/src/photos/pic.jpg"),
        file_hash="c" * 64,
        file_name="pic.jpg",
        modified_time=day_ts,
    )
    note = resolve_note_path(Path("/vault"), row, hierarchical=True)
    assert note.parent == Path("/vault/2022/2022-06-01")
    assert note.name == "2022-06-01-pic.md"


def test_extract_creation_date_rejects_corrupt_year() -> None:
    assert extract_creation_date(Path("/x/y.bin"), 0.0) is None
    assert extract_creation_date(Path("/x/y.bin"), None) is None


def test_export_mode_helpers() -> None:
    assert export_mode_writes_sidecars("standard") is False
    assert export_mode_writes_sidecars("plm") is True
    assert export_mode_writes_sidecars("obsidian") is True
    assert normalize_export_mode("OBSIDIAN") == "obsidian"
    with pytest.raises(ValueError):
        normalize_export_mode("verbose")


def test_resolve_export_paths_pairs_asset_and_note(tmp_path: Path) -> None:
    day_ts = datetime.datetime(2023, 8, 9).timestamp()
    row = GoldenExportRow(
        full_path=Path("/archive/scan.png"),
        file_hash="f" * 64,
        file_name="scan.png",
        modified_time=day_ts,
    )
    paths = resolve_export_paths(tmp_path / "vault", row, hierarchical=True)
    assert paths.note_path == tmp_path / "vault/2023/2023-08-09/2023-08-09-scan.md"
    assert paths.asset_path == tmp_path / "vault/2023/2023-08-09/2023-08-09-scan.png"
    assert paths.embed_name == "2023-08-09-scan.png"
    assert extract_original_extension(row) == ".png"


def test_render_note_references_db_ots_storage() -> None:
    row = GoldenExportRow(
        full_path=Path("/data/doc.pdf"),
        file_hash="d" * 64,
        file_name="doc.pdf",
        modified_time=None,
    )
    ots = OtsStampResult(proof_blob=b"\x00\x01proof")
    text = render_vault_note(row, ots=ots, created_iso="2026-05-30T00:00:00+00:00")
    assert 'notary_status: "ANCHORED"' in text
    assert "ots_proof_storage: file_index.ots_proof" in text.replace('"', "")
    assert "ots_proof_bytes: 7" in text


def test_copy_source_asset(tmp_path: Path) -> None:
    source = tmp_path / "src.bin"
    source.write_bytes(b"binary-payload")
    dest = tmp_path / "out/2025-01-01-file.bin"
    copy_source_asset(source, dest)
    assert dest.read_bytes() == b"binary-payload"


def test_persist_ots_proof_column(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    configure_connection(conn)
    conn.execute(
        """
        INSERT INTO file_index (
            sha256_hash, file_name, file_size, modified_time, full_path, is_golden
        ) VALUES (?, 'a.png', 1, 1.0, ?, 1)
        """,
        ("a" * 64, str(tmp_path / "a.png")),
    )
    conn.commit()
    row = GoldenExportRow(
        full_path=tmp_path / "a.png",
        file_hash="a" * 64,
        file_name="a.png",
        modified_time=1.0,
    )
    proof = b"ots-binary-blob"
    persist_file_index_ots_proof(conn, row, proof)
    conn.commit()
    stored = conn.execute(
        "SELECT ots_proof FROM file_index WHERE full_path = ?", (str(row.full_path),)
    ).fetchone()[0]
    assert bytes(stored) == proof
    conn.close()


def test_export_standard_skips_sidecars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import db_ingest as mod

    source = tmp_path / "src"
    vault = tmp_path / "vault"
    db = tmp_path / "mine.db"
    source.mkdir()
    vault.mkdir()
    asset = source / "photo.png"
    asset.write_bytes(b"png-bytes")

    conn = sqlite3.connect(db)
    configure_connection(conn)
    session = "sess-1"
    conn.execute(
        """
        INSERT INTO file_index (
            sha256_hash, file_name, file_size, modified_time, full_path,
            is_golden, last_session_id
        ) VALUES (?, 'photo.png', 4, ?, ?, 1, ?)
        """,
        ("b" * 64, asset.stat().st_mtime, str(asset), session),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        mod,
        "stamp_ots_proof",
        lambda _h: OtsStampResult(proof_blob=b"ots-proof-bytes"),
    )

    result = export_golden_vault(
        db, session, vault, hierarchical=False, export_mode="standard"
    )
    assert result.assets_copied == 1
    assert result.notes_written == 0
    assert result.proofs_persisted == 1
    assert list(vault.glob("*.md")) == []
    assert list(vault.glob("*.ots")) == []
    assert (vault / "photo.png").exists() or any(vault.glob("*.png"))


def test_export_obsidian_writes_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import db_ingest as mod

    source = tmp_path / "src"
    vault = tmp_path / "vault"
    db = tmp_path / "mine.db"
    source.mkdir()
    asset = source / "doc.pdf"
    asset.write_bytes(b"%PDF-1.4")

    conn = sqlite3.connect(db)
    configure_connection(conn)
    session = "sess-2"
    conn.execute(
        """
        INSERT INTO file_index (
            sha256_hash, file_name, file_size, modified_time, full_path,
            is_golden, last_session_id
        ) VALUES (?, 'doc.pdf', 8, ?, ?, 1, ?)
        """,
        ("c" * 64, asset.stat().st_mtime, str(asset), session),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        mod,
        "stamp_ots_proof",
        lambda _h: OtsStampResult(proof_blob=b"ots-proof"),
    )

    result = export_golden_vault(
        db, session, vault, hierarchical=False, export_mode="obsidian"
    )
    assert result.assets_copied == 1
    assert result.notes_written == 1
    assert result.note_paths[0].suffix == ".md"
    assert "![[" in result.note_paths[0].read_text(encoding="utf-8")


def test_export_stamps_ots_before_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import db_ingest as mod

    order: list[str] = []

    def fake_stamp(file_hash: str) -> OtsStampResult:
        order.append("stamp")
        return OtsStampResult(proof_blob=b"proof")

    monkeypatch.setattr(mod, "stamp_ots_proof", fake_stamp)

    row = GoldenExportRow(
        full_path=tmp_path / "note.txt",
        file_hash="e" * 64,
        file_name="note.txt",
        modified_time=datetime.datetime(2025, 1, 2).timestamp(),
    )
    row.full_path.write_bytes(b"hello asset")
    paths = resolve_export_paths(tmp_path / "vault", row, hierarchical=True)

    persisted: list[bool] = []

    def persist(row_in: GoldenExportRow, blob: bytes | None) -> None:
        persisted.append(blob is not None)
        order.append("persist")

    ots, asset_copied, note_written = export_golden_file(
        paths,
        row,
        export_mode="obsidian",
        persist_ots=persist,
    )
    order.append("disk")

    assert order[:3] == ["stamp", "persist", "disk"]
    assert ots.ok and asset_copied and note_written
    assert paths.note_path.is_file()
    assert not list(paths.folder.glob("*.ots"))
