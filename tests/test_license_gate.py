"""Tests for VM-compatible freemium license gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest

from core.license_gate import (
    FREEMIUM_FILE_LIMIT,
    SECRET_KEY,
    ALGORITHM,
    LICENSE_ISSUER,
    FreemiumLimitExceeded,
    assert_processing_allowed,
    calculate_signature,
    is_license_valid,
    load_state,
    record_processed_file,
    save_state,
)


def _make_token() -> str:
    payload = {
        "iss": LICENSE_ISSUER,
        "exp": datetime.now(timezone.utc) + timedelta(days=30),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def test_hmac_state_round_trip(tmp_path: Path) -> None:
    state_path = tmp_path / "sovraan_processed_state.json"
    save_state(42, state_path=state_path)
    assert load_state(state_path) == 42
    assert calculate_signature(42) == calculate_signature(42)


def test_tampered_state_triggers_limit(tmp_path: Path) -> None:
    state_path = tmp_path / "sovraan_processed_state.json"
    state_path.write_text(
        '{"processed_file_count": 1, "state_signature": "bad"}',
        encoding="utf-8",
    )
    assert load_state(state_path) == FREEMIUM_FILE_LIMIT


def test_freemium_cap_without_license(tmp_path: Path) -> None:
    state_path = tmp_path / "sovraan_processed_state.json"
    save_state(FREEMIUM_FILE_LIMIT, state_path=state_path)
    with pytest.raises(FreemiumLimitExceeded):
        assert_processing_allowed(None, state_path=state_path)


def test_valid_license_bypasses_cap(tmp_path: Path) -> None:
    state_path = tmp_path / "sovraan_processed_state.json"
    save_state(FREEMIUM_FILE_LIMIT, state_path=state_path)
    token = _make_token()
    assert is_license_valid(token)
    assert_processing_allowed(token, state_path=state_path)
    record_processed_file(token, state_path=state_path)
    assert load_state(state_path) == FREEMIUM_FILE_LIMIT + 1
