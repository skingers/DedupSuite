"""Tests for OpenTimestamps proof verification (SOV-SEC-015)."""

from __future__ import annotations

import pytest

from core.ots_proof import verify_opentimestamps_proof

VALID_HASH = "a" * 64


def test_verify_rejects_empty_blob() -> None:
    result = verify_opentimestamps_proof(VALID_HASH, b"")
    assert result["ok"] is False
    assert result["state"] == "invalid"


def test_verify_rejects_invalid_hash_hex() -> None:
    result = verify_opentimestamps_proof("not-hex", b"blob")
    assert result["ok"] is False
    assert "invalid hash" in result["message"]


def test_verify_rejects_garbage_blob() -> None:
    result = verify_opentimestamps_proof(VALID_HASH, b"not-an-ots-proof")
    assert result["ok"] is False
    assert result["state"] == "invalid"
