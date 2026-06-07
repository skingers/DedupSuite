"""Unit tests for the ExportKnowledgeGraph utility."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
import pytest

from core.knowledge_graph_exporter import (
    KnowledgeGraphExporter,
    parse_frontmatter_and_body,
    parse_date,
    extract_project_tags_from_path
)


def test_parse_frontmatter_and_body_simple() -> None:
    content = (
        "---\n"
        'file_hash: "abcd1234"\n'
        'original_path: "/data/docs/notes.txt"\n'
        'created: "2026-06-02T10:00:00Z"\n'
        'notary_status: "ANCHORED"\n'
        "ots_proof_bytes: 12\n"
        "---\n"
        "\n# notes.txt\nSome body text here.\n"
    )
    meta, body = parse_frontmatter_and_body(content)
    assert meta["file_hash"] == "abcd1234"
    assert meta["original_path"] == "/data/docs/notes.txt"
    assert meta["created"] == "2026-06-02T10:00:00Z"
    assert meta["notary_status"] == "ANCHORED"
    assert meta["ots_proof_bytes"] == 12
    assert "Some body text here." in body


def test_parse_frontmatter_and_body_lists() -> None:
    # Test flow list: [a, b]
    content_flow = (
        "---\n"
        "tags: [tag-a, tag-b]\n"
        "---\n"
    )
    meta, _ = parse_frontmatter_and_body(content_flow)
    assert meta["tags"] == ["tag-a", "tag-b"]

    # Test block list:
    # tags:
    #   - tag-c
    #   - tag-d
    content_block = (
        "---\n"
        "tags:\n"
        "  - tag-c\n"
        "  - tag-d\n"
        "---\n"
    )
    meta, _ = parse_frontmatter_and_body(content_block)
    assert meta["tags"] == ["tag-c", "tag-d"]


def test_parse_date() -> None:
    assert parse_date("2026-06-02T10:00:00Z").year == 2026
    assert parse_date("2026-06-02").day == 2
    assert parse_date("invalid-date") is None
    assert parse_date("") is None


def test_extract_project_tags_from_path() -> None:
    tags = extract_project_tags_from_path("/Users/marks/dev/projects/sovraan/core/notary.py")
    assert "core" in tags
    assert "sovraan" in tags
    # Ignored paths like dev, projects, users should not be in tags
    assert "dev" not in tags
    assert "projects" not in tags
    assert "users" not in tags


def test_scan_and_build_graph(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    # Create elevated sidecars in hierarchical layout
    folder_2026_06_01 = vault_root / "2026" / "2026-06-01"
    folder_2026_06_01.mkdir(parents=True)
    
    note_1 = folder_2026_06_01 / "2026-06-01-note1.md"
    note_1.write_text(
        "---\n"
        'file_hash: "hash_1"\n'
        'original_path: "/data/project_alpha/src/file1.py"\n'
        'created: "2026-06-01T12:00:00Z"\n'
        "---\n"
        "# note1\n"
        "Some text. #custom-tag [[INDEX_src]]\n",
        encoding="utf-8"
    )

    note_2 = folder_2026_06_01 / "2026-06-01-note2.md"
    note_2.write_text(
        "---\n"
        'file_hash: "hash_2"\n'
        'original_path: "/data/project_alpha/src/file2.py"\n'
        'created: "2026-06-01T14:00:00Z"\n'
        "---\n"
        "# note2\n",
        encoding="utf-8"
    )

    # Consecutive day note
    folder_2026_06_02 = vault_root / "2026" / "2026-06-02"
    folder_2026_06_02.mkdir(parents=True)

    note_3 = folder_2026_06_02 / "2026-06-02-note3.md"
    note_3.write_text(
        "---\n"
        'file_hash: "hash_3"\n'
        'original_path: "/data/project_beta/utils/file3.py"\n'
        'created: "2026-06-02T10:00:00Z"\n'
        "---\n"
        "# note3\n",
        encoding="utf-8"
    )

    # Non-sidecar file (should be ignored)
    dummy_note = vault_root / "dummy.md"
    dummy_note.write_text("Just some text without frontmatter", encoding="utf-8")

    # Folder-index file (should be ignored)
    index_note = vault_root / "INDEX_src.md"
    index_note.write_text(
        "---\n"
        "type: folder-index\n"
        'source_folder: "/data/project_alpha/src"\n'
        "---\n"
        "- [[2026-06-01-note1]]\n",
        encoding="utf-8"
    )

    # Instantiate exporter
    exporter = KnowledgeGraphExporter(str(vault_root), db_path=str(tmp_path / "nonexistent.db"))
    
    # Verify scanning finds exactly 3 sidecars
    sidecars = exporter.scan_sidecars()
    assert len(sidecars) == 3
    rel_paths = {sc["rel_path"] for sc in sidecars}
    assert "2026/2026-06-01/2026-06-01-note1.md" in rel_paths
    assert "2026/2026-06-01/2026-06-01-note2.md" in rel_paths
    assert "2026/2026-06-02/2026-06-02-note3.md" in rel_paths

    # Build relationship graph
    graph = exporter.build_graph()
    assert "nodes" in graph
    assert "edges" in graph
    assert len(graph["nodes"]) == 3

    hash_note_1 = hashlib.sha256(b"2026/2026-06-01/2026-06-01-note1.md").hexdigest()
    hash_note_2 = hashlib.sha256(b"2026/2026-06-01/2026-06-01-note2.md").hexdigest()
    hash_note_3 = hashlib.sha256(b"2026/2026-06-02/2026-06-02-note3.md").hexdigest()

    # Check node fields
    node_1_data = next(n for n in graph["nodes"] if n["id"] == hash_note_1)
    assert node_1_data["vault_relative_path"] == "2026/2026-06-01/2026-06-01-note1.md"
    assert node_1_data["file_hash"] == "hash_1"
    assert node_1_data["original_path"] == "/data/project_alpha/src/file1.py"
    # Project tags from original path (src, project_alpha) + custom tag (#custom-tag) + index ([[INDEX_src]])
    assert "src" in node_1_data["project_tags"]
    assert "project_alpha" in node_1_data["project_tags"]
    assert "custom-tag" in node_1_data["project_tags"]
    assert "INDEX_src" in node_1_data["project_tags"]
    
    # Date tags
    assert "2026-06-01" in node_1_data["date_tags"]
    assert "year_2026" in node_1_data["date_tags"]
    assert "month_2026_06" in node_1_data["date_tags"]

    # Check edge relationships
    edges = graph["edges"]
    
    # Relations:
    # note1 and note2 share project tags: "src", "project_alpha"
    # note1 and note2 are created on same day: "2026-06-01"
    # note2 and note3 are created on consecutive days: "2026-06-01" and "2026-06-02"
    
    # Find shared project tag edges between note1 and note2
    shared_proj_edges = [
        e for e in edges
        if e["type"] == "shared_project_tag"
        and {e["source"], e["target"]} == {hash_note_1, hash_note_2}
    ]
    assert len(shared_proj_edges) >= 2  # at least "src" and "project_alpha"
    
    # Find same-day adjacency edges
    same_day_edges = [
        e for e in edges
        if e["type"] == "date_based_adjacency" and e["detail"] == "same_day"
    ]
    assert len(same_day_edges) == 1
    assert {same_day_edges[0]["source"], same_day_edges[0]["target"]} == {
        hash_note_1, hash_note_2
    }

    # Find consecutive-day adjacency edges
    consec_day_edges = [
        e for e in edges
        if e["type"] == "date_based_adjacency" and e["detail"] == "consecutive_day"
    ]
    assert len(consec_day_edges) == 2
    
    # Export and verify JSON write
    manifest_file = exporter.export()
    assert Path(manifest_file).exists()
    assert Path(manifest_file).name == "graph_manifest.json"
    
    with open(manifest_file, "r", encoding="utf-8") as f:
        loaded_graph = json.load(f)
    assert len(loaded_graph["nodes"]) == 3
    assert len(loaded_graph["edges"]) == len(edges)


def test_db_and_hash_fallbacks(tmp_path: Path) -> None:
    # 1. Create a dummy sqlite database in tmp_path
    db_file = tmp_path / "test_mine.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE file_index (
            sha256_hash TEXT,
            phash TEXT,
            file_name TEXT,
            file_size INTEGER,
            modified_time REAL,
            full_path TEXT,
            is_golden_version INTEGER,
            device_id TEXT,
            last_session_id TEXT,
            is_golden INTEGER,
            ots_proof BLOB,
            classification TEXT,
            status TEXT,
            pre_archive_path TEXT,
            archive_transaction_id INTEGER
        )
        """
    )
    # Insert a record (we will map expected_hash in step 2)
    conn.execute(
        "INSERT INTO file_index (sha256_hash, file_name, full_path, ots_proof, classification, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("placeholder", "KPMG (2).docx", "/original/path/KPMG/KPMG (2).docx", b"dummy_proof", "golden", "active")
    )
    conn.commit()
    conn.close()

    # 2. Create vault structure
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    
    # Write a companion file
    companion = vault_root / "2023-08-18_kpmg_2.docx"
    companion.write_bytes(b"some content")
    
    # Compute its expected hash
    expected_hash = hashlib.sha256(b"some content").hexdigest()
    
    # Update the database to map expected_hash
    conn = sqlite3.connect(db_file)
    conn.execute("UPDATE file_index SET sha256_hash = ?", (expected_hash,))
    conn.commit()
    conn.close()

    # Write sidecar with no file_hash/original_path but original_filename and vaulted_name
    note = vault_root / "2023-08-18_kpmg_2.md"
    note.write_text(
        "---\n"
        "original_filename: KPMG (2).docx\n"
        "vaulted_name: 2023-08-18_kpmg_2.docx\n"
        "---\n"
        "# KPMG (2).docx\n",
        encoding="utf-8"
    )

    exporter = KnowledgeGraphExporter(str(vault_root), db_path=str(db_file))
    graph = exporter.build_graph()
    
    # Assert sidecar parsed successfully
    assert len(graph["nodes"]) == 1
    node = graph["nodes"][0]
    
    # Hash fallback resolved expected_hash
    assert node["file_hash"] == expected_hash
    # DB lookup resolved original path from KPMG (2).docx match / hash match
    assert node["original_path"] == "/original/path/KPMG/KPMG (2).docx"
    # DB lookup resolved ots_proof_bytes
    assert node["metadata"]["ots_proof_bytes"] == len(b"dummy_proof")
    # notary_status resolved to ANCHORED since proof exists
    assert node["notary_status"] == "ANCHORED"
    
    # Project tags should contain folder name from original path "KPMG" and "original"
    assert "KPMG" in node["project_tags"]
