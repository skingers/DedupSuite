"""Tests for the MarkdownTranslator Obsidian export subsystem."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.markdown_translator import MarkdownTranslator, UniqueFileRecord

WIKILINK = re.compile(r"\[\[[^\]]+\]\]")


def _records():
    return [
        UniqueFileRecord(path=Path("/data/photos/holiday/img1.jpg"), file_hash="a" * 64),
        UniqueFileRecord(path=Path("/data/photos/holiday/img2.jpg"), file_hash="b" * 64),
        UniqueFileRecord(path=Path("/data/docs/report.pdf"), file_hash="c" * 64, notary_status="SUBMITTED"),
    ]


def test_creates_export_dir_and_flattens_notes(tmp_path: Path) -> None:
    result = MarkdownTranslator(tmp_path).translate(_records())

    assert result.export_dir == tmp_path / "Obsidian_Export"
    assert result.export_dir.is_dir()
    # Folder flattening: every note + index sits directly in the export dir.
    for note in result.note_paths + result.index_paths:
        assert note.parent == result.export_dir
        assert note.suffix == ".md"
    assert len(result.note_paths) == 3


def test_frontmatter_contains_required_fields(tmp_path: Path) -> None:
    result = MarkdownTranslator(tmp_path).translate(_records())
    report_note = next(p for p in result.note_paths if "report" in p.name)
    text = report_note.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert 'file_hash: "' + "c" * 64 + '"' in text
    assert "original_path:" in text
    assert "created:" in text
    assert 'notary_status: "SUBMITTED"' in text


def test_bidirectional_wikilinks(tmp_path: Path) -> None:
    result = MarkdownTranslator(tmp_path).translate(_records())

    # A note in the holiday folder links UP to its folder index.
    note = next(p for p in result.note_paths if p.name.startswith("img1"))
    note_text = note.read_text(encoding="utf-8")
    assert WIKILINK.search(note_text)
    m = re.search(r"\[\[(INDEX_[^\]]+)\]\]", note_text)
    assert m, "note must link to a folder index via wikilink"
    index_stem = m.group(1)

    # The folder index links BACK to the member note (bi-directional).
    index_path = result.export_dir / f"{index_stem}.md"
    assert index_path in result.index_paths
    index_text = index_path.read_text(encoding="utf-8")
    assert "[[img1]]" in index_text
    assert "[[img2]]" in index_text


def test_distinct_source_folders_get_distinct_indexes(tmp_path: Path) -> None:
    result = MarkdownTranslator(tmp_path).translate(_records())
    # holiday/ and docs/ are two folders -> two index notes.
    assert len(result.index_paths) == 2


def test_flatten_name_collision_is_disambiguated(tmp_path: Path) -> None:
    records = [
        UniqueFileRecord(path=Path("/srcA/notes.txt"), file_hash="1" * 64),
        UniqueFileRecord(path=Path("/srcB/notes.txt"), file_hash="2" * 64),
    ]
    result = MarkdownTranslator(tmp_path).translate(records)

    names = {p.name for p in result.note_paths}
    assert len(names) == 2  # no overwrite despite identical stems
    assert any(n == "notes.md" for n in names)
    assert any(n.startswith("notes_") and n.endswith(".md") for n in names)


def test_accepts_mapping_records(tmp_path: Path) -> None:
    result = MarkdownTranslator(tmp_path).translate(
        [{"path": "/x/y/file.bin", "file_hash": "d" * 64, "notary_status": "PENDING"}]
    )
    assert len(result.note_paths) == 1
    assert result.note_paths[0].read_text(encoding="utf-8").count("file_hash") == 1
