"""Fidelity Validator and Integrity Gate for DedupSuite.

Runs 10 Golden Queries against the vector engine, resolves matching nodes,
compares original source files on disk against vault companion assets with 100%
fidelity, and appends a detailed Fidelity Audit Report to walkthrough.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
import pytest

from core.knowledge_graph_exporter import KnowledgeGraphExporter
from core.vector_engine import SovereignVectorEngine

# Define the 10 Golden datasets containing semantically distinct contents and queries
GOLDEN_DATASETS = [
    {
        "file_name": "financial_tax_ledger.pdf",
        "content": b"Yearly corporate taxation forms, IRS schedule K-1 filings, and profit ledger statements for fiscal audit.",
        "golden_query": "corporate tax filings and irs forms audit ledger",
        "tags": ["finance", "taxes", "audit"],
        "created": "2026-06-01"
    },
    {
        "file_name": "neural_network_weights.bin",
        "content": b"Trained transformer model weights, tensor layers float32 bias matrices, and attention head checkpoints.",
        "golden_query": "attention head checkpoints and transformer model weights",
        "tags": ["machine-learning", "transformers", "weights"],
        "created": "2026-06-02"
    },
    {
        "file_name": "espresso_grind_guide.txt",
        "content": b"Grind size adjustments, extraction time in seconds, water pressure in bars, and espresso shot recipes.",
        "golden_query": "espresso grind extraction pressure guide",
        "tags": ["coffee", "espresso", "recipes"],
        "created": "2026-06-03"
    },
    {
        "file_name": "quantum_physics_paper.md",
        "content": b"Superposition states, entanglement entropy coefficients, Hilbert space dimensions, and wave function collapse.",
        "golden_query": "quantum entanglement wave function hilbert space",
        "tags": ["physics", "quantum", "paper"],
        "created": "2026-06-04"
    },
    {
        "file_name": "garden_irrigation_map.dwg",
        "content": b"Subsurface drip line layouts, water solenoid valves, flow meters, and zone control schedules.",
        "golden_query": "drip irrigation zone solenoid valves layouts",
        "tags": ["gardening", "irrigation", "map"],
        "created": "2026-06-05"
    },
    {
        "file_name": "organic_sourdough_bake.pdf",
        "content": b"Wild yeast sourdough starter feeding schedule, flour hydration ratios, bulk fermentation hours, and Dutch oven baking.",
        "golden_query": "yeast starter hydration bulk fermentation sourdough",
        "tags": ["baking", "sourdough", "bread"],
        "created": "2026-06-06"
    },
    {
        "file_name": "rust_compiler_optimization.rs",
        "content": b"LLVM compiler flags, inline assembly blocks, zero-cost abstractions, memory safety borrow checker rules.",
        "golden_query": "llvm compiler flags rust borrow checker abstractions",
        "tags": ["programming", "rust", "compilers"],
        "created": "2026-06-07"
    },
    {
        "file_name": "ancient_history_timeline.xlsx",
        "content": b"Mesopotamian clay tablet records, bronze age trade routes, dynasty reigns, and archaeological excavations.",
        "golden_query": "mesopotamian clay tablets bronze age excavations",
        "tags": ["history", "archaeology", "bronze-age"],
        "created": "2026-06-08"
    },
    {
        "file_name": "rocket_nozzle_thermal.stp",
        "content": b"Liquid fuel regeneratively cooled engine nozzle CAD designs, niobium alloy heat stress limits.",
        "golden_query": "regeneratively cooled engine nozzle thermal stress",
        "tags": ["aerospace", "propulsion", "cad"],
        "created": "2026-06-09"
    },
    {
        "file_name": "medical_mri_scan.dcm",
        "content": b"High-resolution cerebral cortex sagittal slices, ventricular brain scan measurements, radiological patient charts.",
        "golden_query": "cerebral cortex brain mri sagittal slices scan",
        "tags": ["medicine", "radiology", "mri"],
        "created": "2026-06-10"
    }
]


@pytest.fixture
def setup_integrity_environment(tmp_path: Path) -> tuple[Path, Path]:
    """Generates source files and vault sidecars for the 10 Golden Datasets."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()

    for idx, ds in enumerate(GOLDEN_DATASETS):
        file_name = ds["file_name"]
        content = ds["content"]
        created = ds["created"]
        tags = ds["tags"]

        # Write original source file on disk
        src_file = source_dir / file_name
        src_file.write_bytes(content)
        file_hash = hashlib.sha256(content).hexdigest()

        # Create hierarchical directories in vault based on date
        dt_parts = created.split("-")
        year = dt_parts[0]
        date_folder = vault_dir / year / created
        date_folder.mkdir(parents=True, exist_ok=True)

        # Write companion asset file to vault
        vault_companion = date_folder / file_name
        vault_companion.write_bytes(content)

        # Write sidecar markdown note
        note_name = f"{created}-{Path(file_name).stem}.md"
        note_file = date_folder / note_name
        
        tags_yaml = "\n".join([f"  - {t}" for t in tags])
        note_content = (
            "---\n"
            f'file_hash: "{file_hash}"\n'
            f'original_path: "{src_file.resolve()}"\n'
            f'original_filename: "{file_name}"\n'
            f'vaulted_name: "{file_name}"\n'
            f'created: "{created}"\n'
            "tags:\n"
            f"{tags_yaml}\n"
            "---\n"
            f"# {file_name}\n"
            f"{content.decode('utf-8', errors='ignore')}\n"
        )
        note_file.write_text(note_content, encoding="utf-8")

    # Export knowledge graph manifest
    exporter = KnowledgeGraphExporter(str(vault_dir), db_path=str(tmp_path / "db.db"))
    exporter.export()

    # Ingest using Vector Engine
    engine = SovereignVectorEngine(str(vault_dir))
    engine.ingest_manifest()

    return vault_dir, source_dir


def test_fidelity_integrity_gate(setup_integrity_environment: tuple[Path, Path]) -> None:
    """Run the 10 Golden Queries, resolve paths, compare binary contents, and write audit report."""
    vault_dir, source_dir = setup_integrity_environment
    engine = SovereignVectorEngine(str(vault_dir))

    audit_rows = []
    failed_audits = []

    for idx, ds in enumerate(GOLDEN_DATASETS):
        query = ds["golden_query"]
        expected_file_name = ds["file_name"]
        
        # Performance measurement
        start_time = time.perf_counter_ns()
        results = engine.query_index(query, n_results=1)
        end_time = time.perf_counter_ns()
        
        latency_ms = (end_time - start_time) / 1_000_000.0

        if not results:
            failed_audits.append((expected_file_name, "No match returned from vector index"))
            audit_rows.append({
                "query": query,
                "file_name": expected_file_name,
                "node_id": "N/A",
                "latency_ms": latency_ms,
                "fidelity": "FAIL",
                "details": "Lookup returned no results"
            })
            continue

        match = results[0]
        node_id = match["id"]
        meta = match["metadata"]
        vault_rel_path = match["vault_relative_path"]
        original_path = meta.get("original_path")

        # Basic path matching check
        retrieved_file_name = meta.get("original_name")
        if retrieved_file_name != expected_file_name:
            err_msg = f"Match mismatch. Expected '{expected_file_name}', got '{retrieved_file_name}'"
            failed_audits.append((expected_file_name, err_msg))
            audit_rows.append({
                "query": query,
                "file_name": expected_file_name,
                "node_id": node_id,
                "latency_ms": latency_ms,
                "fidelity": "FAIL",
                "details": err_msg
            })
            continue

        # Exact absolute binary check
        try:
            # Original file check
            if not original_path or not os.path.exists(original_path):
                raise FileNotFoundError(f"Original path on disk does not exist: {original_path}")
            
            original_bytes = Path(original_path).read_bytes()

            # Vault companion file check
            vault_companion_path = os.path.join(
                str(vault_dir),
                os.path.dirname(vault_rel_path),
                expected_file_name
            )
            if not os.path.exists(vault_companion_path):
                raise FileNotFoundError(f"Vault companion file does not exist: {vault_companion_path}")

            vault_bytes = Path(vault_companion_path).read_bytes()

            # Verify 100% binary fidelity
            if original_bytes != vault_bytes:
                raise ValueError("Binary mismatch between original source and vault companion")

            # Verify cryptographic hash matching
            expected_hash = hashlib.sha256(original_bytes).hexdigest()
            node_hash = meta.get("file_hash")
            if node_hash != expected_hash:
                raise ValueError(f"Hash mismatch: metadata hash {node_hash} vs calculated {expected_hash}")

            audit_rows.append({
                "query": query,
                "file_name": expected_file_name,
                "node_id": node_id,
                "latency_ms": latency_ms,
                "fidelity": "PASS",
                "details": "100% Binary & Hash Match"
            })

        except Exception as e:
            err_msg = str(e)
            # Log the specific node_id and path of the discrepancy
            print(f"[DISCREPANCY DETECTED] Node ID: {node_id} | Path: {original_path} | Error: {err_msg}")
            failed_audits.append((expected_file_name, f"Node ID: {node_id} | Path: {original_path} | Error: {err_msg}"))
            audit_rows.append({
                "query": query,
                "file_name": expected_file_name,
                "node_id": node_id,
                "latency_ms": latency_ms,
                "fidelity": "FAIL",
                "details": err_msg
            })

    # Prepare report markdown
    report_md = "\n## Phase 3: Fidelity Audit Report (Integrity Gate)\n\n"
    report_md += "The validator executes a verification loop mapping semantic queries back to source files and comparing binary contents to guarantee 100% fidelity.\n\n"
    report_md += "| # | Golden Query | Target File | Retrieved Node ID | Latency (ms) | Fidelity | Verification Details |\n"
    report_md += "|---|---|---|---|---|---|---|\n"
    
    total_latency = 0.0
    for idx, row in enumerate(audit_rows):
        total_latency += row["latency_ms"]
        report_md += (
            f"| {idx+1} | `{row['query']}` | `{row['file_name']}` | `{row['node_id'][:12]}...` | "
            f"{row['latency_ms']:.2f} | **{row['fidelity']}** | {row['details']} |\n"
        )
    
    avg_latency = total_latency / len(audit_rows)
    compilation_status = "SUCCESS" if not failed_audits else "FAILED"
    
    report_md += f"\n- **Average Retrieval Latency**: {avg_latency:.2f} ms\n"
    report_md += f"- **Ready-for-Compilation Status**: **{compilation_status}**\n"

    # Append report to walkthrough.md
    walkthrough_path = r"C:\Users\marks\.gemini\antigravity-ide\brain\d585fdff-6748-4b44-b8ab-4366a988d437\walkthrough.md"
    if not os.path.exists(os.path.dirname(walkthrough_path)):
        walkthrough_path = "walkthrough.md"

    try:
        with open(walkthrough_path, "a", encoding="utf-8") as wf:
            wf.write(report_md)
    except Exception as e:
        print(f"Failed to write Fidelity Audit Report to walkthrough: {e}")

    # Assert that all 10 Golden Queries passed the integrity gate
    assert len(failed_audits) == 0, f"Integrity gate failed on {len(failed_audits)} files: {failed_audits}"
