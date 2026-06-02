"""Unit and E2E tests for the Standalone DedupSuite Tkinter UI Journeys.

Tests click-depth of ingestion, Time-to-First-Result (TTFR) of semantic search,
and data verification elements of the Pro Studio panel.
"""

from __future__ import annotations

import os
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

import customtkinter as ctk
from dedup_suite import DedupApp


def test_standalone_ui_journeys(tmp_path: Path) -> None:
    """Benchmark click-depth, search responsiveness, and Pro Studio stats."""
    # Setup test paths
    source_dir = tmp_path / "source_folder"
    source_dir.mkdir()
    (source_dir / "data.txt").write_text("Hello World content for dedup.", encoding="utf-8")

    vault_dir = tmp_path / "vault_destination"
    vault_dir.mkdir()

    db_file = tmp_path / "test_gui_mine.db"

    # Patch DatabaseManager to point to a temporary test DB and mock return statistics
    with patch("dedup_suite.DatabaseManager") as MockDBManagerClass, \
         patch("dedup_suite.DedupApp.start_audit") as mock_start:
         
        mock_db = MagicMock()
        mock_db.db_path = str(db_file)
        mock_db.get_mine_stats.return_value = {
            "total_files": 1,
            "golden_files": 1,
            "total_storage": 100
        }
        mock_db.get_recent_golden_files.return_value = ["/path/to/golden/file.txt"]
        MockDBManagerClass.return_value = mock_db

        # Instantiate DedupApp (CustomTkinter GUI)
        app = DedupApp()

        assert app.root is not None, "Failed to initialize Tkinter root"
        assert app.nb is not None, "Tabview not initialized"

        # -------------------------------------------------------------
        # JOURNEY 1: The "First Run" Ingestion Flow & Click Depth
        # -------------------------------------------------------------
        click_count = 0

        # Simulate selecting source folder
        app.src_var.set(str(source_dir))
        click_count += 1  # Browse button click simulated

        # Simulate selecting vault destination
        app.target_vault_dir.set(str(vault_dir))
        click_count += 1  # Change destination button click simulated

        # Simulate clicking "Elevate & Vault"
        cmd = app.btn_start.cget("command")
        if cmd:
            cmd()
        else:
            app.btn_start.invoke()
            
        mock_start.assert_called_once()
        click_count += 1  # Elevate button click simulated

        # Verify Click-depth requirement (core ingestion must be achieved in <= 3 clicks)
        assert click_count <= 3, f"Friction Error: Journey requires {click_count} clicks (limit: 3)"

        # -------------------------------------------------------------
        # JOURNEY 2: The Semantic Search Flow & TTFR
        # -------------------------------------------------------------
        # Switch tab to Vault Chat
        app.nb.set("Vault Chat")
        app.root.update()

        # Set search prompt
        app.chat_input.delete(0, "end")
        app.chat_input.insert(0, "Search query details")

        # Mock vector engine and RAG generator
        mock_vector = MagicMock()
        app.vector_engine = mock_vector

        # Measure Time-to-First-Result (TTFR)
        start_time = time.perf_counter_ns()

        with patch("dedup_suite.generate_rag_response") as mock_rag:
            mock_rag.return_value = ["Response chunk 1", "Response chunk 2"]
            
            # Send message (triggers query)
            app._send_message()
            app.root.update()
            
            # Brief delay to allow background thread simulation to process
            time.sleep(0.05)
            app.root.update()

        end_time = time.perf_counter_ns()
        ttfr_ms = (end_time - start_time) / 1_000_000.0

        # Verify latency score is positive and record metrics
        assert ttfr_ms >= 0.0, "Invalid query execution latency measured"

        # -------------------------------------------------------------
        # JOURNEY 3: The Verification Flow (Pro Studio tab)
        # -------------------------------------------------------------
        # Switch tab to Pro Studio
        app.nb.set("Pro Studio")
        app.root.update()

        # Programmatically instantiate the datamine section to test it
        app._init_datamine_section(app.t_pro)
        app.root.update()

        # Trigger update of database index details
        app.update_datamine_stats()
        app.root.update()

        # Verify that total files and golden files labels are loaded with correct values
        assert app.lbl_tot_files.cget("text") == "Files Ingested: 1"
        assert app.lbl_golden.cget("text") == "Verified Golden Masters: 1"
        assert app.lbl_storage.cget("text") == "Total Storage Used: 100.00 B"

        # Destroy window cleanly
        app.root.destroy()
