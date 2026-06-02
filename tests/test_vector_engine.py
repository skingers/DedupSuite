"""Unit tests for the Sovereign Vector Engine.

Verifies index counts, database persistence, and semantic query accuracy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pytest

from core.knowledge_graph_exporter import KnowledgeGraphExporter
from core.vector_engine import SovereignVectorEngine


@pytest.fixture
def temp_vault_with_manifest(tmp_path: Path) -> Path:
    """Sets up a temporary vault, writes structured notes, and exports graph_manifest.json."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    # Note 1: Information about a Python script for deduplication
    note_1 = vault_root / "2026-06-02-dedup-script.md"
    note_1.write_text(
        "---\n"
        'file_hash: "hash_py_script"\n'
        'original_path: "/src/dedup_script.py"\n'
        'created: "2026-06-02"\n'
        "---\n"
        "# Python Dedup Script\n"
        "This script uses SHA-256 to dedup files concurrently. It uses eight parallel worker threads "
        "and is highly performant on large local storage volumes.\n",
        encoding="utf-8"
    )

    # Note 2: Information about financial reports in PDF format
    note_2 = vault_root / "2026-06-02-financial-pdf.md"
    note_2.write_text(
        "---\n"
        'file_hash: "hash_pdf_report"\n'
        'original_path: "/docs/financial_report.pdf"\n'
        'created: "2026-06-02"\n'
        "---\n"
        "# Financial PDF Report\n"
        "Quarterly fiscal statements and accounting balances sheet. Includes tax reports and auditor logs "
        "covering corporate expenditures for KPMG audits.\n",
        encoding="utf-8"
    )

    # Note 3: Random personal notes (noise note)
    note_3 = vault_root / "2026-06-02-random-thoughts.md"
    note_3.write_text(
        "---\n"
        'file_hash: "hash_random"\n'
        'original_path: "/notes/coffee.txt"\n'
        'created: "2026-06-02"\n'
        "---\n"
        "# Coffee Recipes\n"
        "Brewing methods for espresso, cappuccino, and cold brew coffee. Grind sizes and roast temperatures.\n",
        encoding="utf-8"
    )

    # Export graph manifest
    exporter = KnowledgeGraphExporter(str(vault_root), db_path=str(tmp_path / "dummy.db"))
    exporter.export()

    return vault_root


def test_vector_engine_indexing_and_querying(temp_vault_with_manifest: Path) -> None:
    """Verify that the node counts match, query works with distance, and indices are accurate."""
    vault_root = temp_vault_with_manifest
    manifest_path = os.path.join(vault_root, "graph_manifest.json")

    # Read manifest node count
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    manifest_nodes = manifest.get("nodes", [])
    expected_count = len(manifest_nodes)
    assert expected_count == 3

    # Initialize Engine and ingest
    engine = SovereignVectorEngine(str(vault_root))
    ingested_count = engine.ingest_manifest()
    
    # 1. Audit Check: Index node count matches manifest node count
    assert ingested_count == expected_count, "Ingested count doesn't match manifest count"
    assert engine.collection.count() == expected_count, "ChromaDB collection count mismatch"

    # 2. Audit Check: Probe query returns valid distance score and the correct corresponding file path
    # Query for financial/accounting topic
    results = engine.query_index("fiscal tax accounting sheet audit kpmg", n_results=1)
    
    assert len(results) == 1, "Should return 1 query result"
    match = results[0]
    
    # Verify distance score exists and is a valid float
    assert isinstance(match["distance"], float), f"Distance is not a float: {type(match['distance'])}"
    assert match["distance"] >= 0.0, f"Distance score is negative: {match['distance']}"
    
    # Verify correct file path
    expected_rel_path = "2026-06-02-financial-pdf.md"
    assert match["vault_relative_path"] == expected_rel_path, (
        f"Incorrect match. Expected '{expected_rel_path}', got '{match['vault_relative_path']}'"
    )

    # Query for coffee recipes
    coffee_results = engine.query_index("espresso brewing cold brew", n_results=1)
    assert len(coffee_results) == 1
    assert coffee_results[0]["vault_relative_path"] == "2026-06-02-random-thoughts.md"


def test_vector_engine_persistence(temp_vault_with_manifest: Path) -> None:
    """Verify that vector database persistence works correctly across engine instances."""
    vault_root = temp_vault_with_manifest

    # Ingest using first instance
    engine_1 = SovereignVectorEngine(str(vault_root))
    engine_1.ingest_manifest()
    assert engine_1.collection.count() == 3

    # Re-instantiate a second engine pointing to same directory
    engine_2 = SovereignVectorEngine(str(vault_root))
    # Count should match without re-ingesting because of persistence layer
    assert engine_2.collection.count() == 3

    # Query should still return results
    results = engine_2.query_index("SHA-256 worker threads concurrent", n_results=1)
    assert len(results) == 1
    assert results[0]["vault_relative_path"] == "2026-06-02-dedup-script.md"
