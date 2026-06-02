"""Unit tests and audit suite for the Knowledge Graph Exporter.

Verifies schema compliance, path normalization, hash-based ID integrity, and
referential integrity of the exported graph_manifest.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import pytest

from core.knowledge_graph_exporter import KnowledgeGraphExporter


@pytest.fixture
def temp_vault(tmp_path: Path) -> tuple[Path, str]:
    """Sets up a temporary vault with structured notes for graph export testing."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    # Create a subfolder with mixed path structure
    subfolder = vault_root / "projects" / "sub_project"
    subfolder.mkdir(parents=True)

    # Note 1: Standard frontmatter sidecar
    note_1 = subfolder / "note_1.md"
    note_1.write_text(
        "---\n"
        'file_hash: "hash_12345"\n'
        'original_path: "C:\\Users\\test\\projects\\sub_project\\src\\main.py"\n'
        'created: "2026-06-02"\n'
        "---\n"
        "# Note 1\n"
        "Some description here with inline #tag-one and wiki-link [[INDEX_sub_project]]\n",
        encoding="utf-8"
    )

    # Note 2: Companion file based sidecar
    note_2 = subfolder / "note_2.md"
    note_2.write_text(
        "---\n"
        "original_filename: data.csv\n"
        "vaulted_name: data_companion.csv\n"
        "---\n"
        "# Note 2\n"
        "Some details about the companion data.\n",
        encoding="utf-8"
    )

    companion = subfolder / "data_companion.csv"
    companion.write_text("col1,col2\nval1,val2\n", encoding="utf-8")

    # Note 3: Adjacent date note (consecutive day)
    note_3 = subfolder / "note_3.md"
    note_3.write_text(
        "---\n"
        'file_hash: "hash_12345"\n'  # Same hash as Note 1 to test cryptographic provenance edge
        'created: "2026-06-03"\n'
        "---\n"
        "# Note 3\n",
        encoding="utf-8"
    )

    return vault_root, str(tmp_path / "test_db.db")


def test_manifest_audit_and_compliance(temp_vault: tuple[Path, str]) -> None:
    """Audit the exported graph_manifest.json to check schema and data integrity."""
    vault_root, db_path = temp_vault

    # Perform the export
    exporter = KnowledgeGraphExporter(str(vault_root), db_path=db_path)
    manifest_path = exporter.export()

    assert os.path.exists(manifest_path)
    assert os.path.basename(manifest_path) == "graph_manifest.json"

    # Load and parse manifest
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # 1. High-Level Schema Compliance
    assert "nodes" in manifest, "Manifest missing 'nodes' key"
    assert "edges" in manifest, "Manifest missing 'edges' key"
    assert isinstance(manifest["nodes"], list), "'nodes' must be a list"
    assert isinstance(manifest["edges"], list), "'edges' must be a list"

    node_ids = set()

    # 2. Node Schema & Integrity Audit
    for idx, node in enumerate(manifest["nodes"]):
        # Required keys check
        for key in (
            "id", "file_name", "vault_relative_path", "original_path",
            "file_hash", "created", "notary_status", "project_tags",
            "date_tags", "metadata"
        ):
            assert key in node, f"Node at index {idx} missing required key '{key}'"

        # ID hash-based format audit
        node_id = node["id"]
        assert isinstance(node_id, str), f"Node ID must be a string: {node_id}"
        assert re.match(r"^[a-f0-9]{64}$", node_id), f"Node ID is not a valid SHA-256 hex string: {node_id}"
        node_ids.add(node_id)

        # Path normalization audit
        rel_path = node["vault_relative_path"]
        assert isinstance(rel_path, str), f"vault_relative_path must be a string: {rel_path}"
        assert "\\" not in rel_path, f"Windows separator found in normalized path: {rel_path}"
        assert "//" not in rel_path, f"Redundant separator found in normalized path: {rel_path}"
        assert ".." not in rel_path, f"Directory traversal segment found in normalized path: {rel_path}"
        assert not rel_path.startswith("/"), f"Path should not be absolute or start with slash: {rel_path}"

        # Hash integrity audit: node ID must be SHA-256 of the normalized UNIX relative path
        expected_id = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()
        assert node_id == expected_id, f"Node ID {node_id} does not match SHA-256 of path {rel_path}"

        # Metadata format check
        metadata = node["metadata"]
        assert isinstance(metadata, dict), f"Node metadata must be a dictionary: {metadata}"
        assert "original_name" in metadata, "Metadata missing 'original_name'"
        assert "ots_proof_bytes" in metadata, "Metadata missing 'ots_proof_bytes'"
        assert "ots_proof_storage" in metadata, "Metadata missing 'ots_proof_storage'"

    # 3. Edge Schema & Integrity Audit
    for idx, edge in enumerate(manifest["edges"]):
        # Required keys check
        for key in ("source", "target", "type", "detail"):
            assert key in edge, f"Edge at index {idx} missing required key '{key}'"

        source = edge["source"]
        target = edge["target"]

        # IDs format audit
        assert re.match(r"^[a-f0-9]{64}$", source), f"Edge source is not a valid SHA-256 hex string: {source}"
        assert re.match(r"^[a-f0-9]{64}$", target), f"Edge target is not a valid SHA-256 hex string: {target}"

        # Referential integrity audit: source and target must exist in the node set
        assert source in node_ids, f"Edge source ID {source} does not exist in any node"
        assert target in node_ids, f"Edge target ID {target} does not exist in any node"

        # Edge type checks
        assert isinstance(edge["type"], str)
        assert isinstance(edge["detail"], str)
