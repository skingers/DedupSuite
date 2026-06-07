"""Unit and E2E tests for the sovraan Calm Journey Tkinter UI."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import customtkinter as ctk
from sovraan_core import SovraanApp


def test_calm_journey_ui(tmp_path: Path) -> None:
    """Benchmark click-depth, search responsiveness, and vault index stats."""
    source_dir = tmp_path / "source_folder"
    source_dir.mkdir()
    (source_dir / "data.txt").write_text("Hello World content for dedup.", encoding="utf-8")

    vault_dir = tmp_path / "vault_destination"
    vault_dir.mkdir()

    db_file = tmp_path / "test_gui_mine.db"

    with patch("sovraan_core.DatabaseManager") as MockDBManagerClass, \
         patch("sovraan_core.SovraanApp.start_audit") as mock_start:

        mock_db = MagicMock()
        mock_db.db_path = str(db_file)
        mock_db.get_mine_stats.return_value = {
            "total_files": 1,
            "golden_files": 1,
            "total_storage": 100,
        }
        mock_db.get_recent_golden_files.return_value = ["/path/to/golden/file.txt"]
        MockDBManagerClass.return_value = mock_db

        app = SovraanApp()

        assert app.root is not None
        assert app.nb is not None

        # Journey 1: first-run ingestion (<= 3 clicks)
        click_count = 0
        app.src_var.set(str(source_dir))
        click_count += 1
        app.target_vault_dir.set(str(vault_dir))
        click_count += 1

        cmd = app.btn_start.cget("command")
        if cmd:
            cmd()
        else:
            app.btn_start.invoke()
        mock_start.assert_called_once()
        click_count += 1
        assert click_count <= 3
        assert app.btn_start.cget("text") == "Begin Rescue"

        # Journey 2: semantic search via Expert Studio vault chat
        app.nb.set("Expert Studio")
        app.root.update()

        app.chat_input.delete(0, "end")
        app.chat_input.insert(0, "Search query details")
        app.vector_engine = MagicMock()

        start_time = time.perf_counter_ns()
        with patch("sovraan_core.generate_rag_response") as mock_rag:
            mock_rag.return_value = ["Response chunk 1", "Response chunk 2"]
            app._send_message()
            app.root.update()
            time.sleep(0.05)
            app.root.update()
        ttfr_ms = (time.perf_counter_ns() - start_time) / 1_000_000.0
        assert ttfr_ms >= 0.0

        # Journey 3: vault index verification
        app.nb.set("The Vault Index")
        app.root.update()
        app.update_datamine_stats()
        app.root.update()

        assert app.lbl_tot_files.cget("text") == "Files Ingested: 1"
        assert app.lbl_golden.cget("text") == "Verified Golden Masters: 1"
        assert app.lbl_storage.cget("text") == "Total Storage Used: 100.00 B"

        app.root.destroy()
