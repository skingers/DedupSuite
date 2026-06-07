"""Freemium license and HMAC-signed processing state (VM-compatible)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Optional

try:
    import jwt
except ImportError:  # pragma: no cover - PyJWT is a declared dependency
    jwt = None  # type: ignore[assignment]

# Must match /opt/sovraan-notary/sovraan_engine.py on the licensing VM.
SECRET_KEY = "SOVRAAN_SECURE_SIGNING_KEY_TOKEN_NODE"
ALGORITHM = "HS256"
LICENSE_ISSUER = "Sovraan Authority"
FREEMIUM_FILE_LIMIT = 2000
STATE_FILENAME = "sovraan_processed_state.json"


class FreemiumLimitExceeded(RuntimeError):
    """Raised when the unlicensed processed-file cap is reached."""

    MESSAGE = (
        "ERROR: Processed file limit (2000) reached. "
        "A valid Sovraan Pro license key is required."
    )

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


def _default_state_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(os.path.dirname(sys.executable))
    else:
        base = Path(os.path.dirname(os.path.abspath(__file__))).parent
    return base / STATE_FILENAME


def calculate_signature(count: int) -> str:
    message = str(count).encode("utf-8")
    key = SECRET_KEY.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def resolve_state_path(db_path: Optional[Path] = None) -> Path:
    """Prefer state alongside the production database when provided."""
    if db_path is not None:
        return Path(db_path).parent / STATE_FILENAME
    return _default_state_path()


def load_state(state_path: Optional[Path] = None) -> int:
    path = state_path or _default_state_path()
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        count = int(data.get("processed_file_count", 0))
        signature = str(data.get("state_signature", ""))
        expected = calculate_signature(count)
        if not hmac.compare_digest(signature, expected):
            print("ALERT: State Tampering Detected!", file=sys.stderr)
            return FREEMIUM_FILE_LIMIT
        return count
    except (OSError, ValueError, json.JSONDecodeError):
        return FREEMIUM_FILE_LIMIT


def save_state(count: int, state_path: Optional[Path] = None) -> None:
    path = state_path or _default_state_path()
    signature = calculate_signature(count)
    payload = {"processed_file_count": count, "state_signature": signature}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def is_license_valid(token: Optional[str]) -> bool:
    if not token or jwt is None:
        return False
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("iss") == LICENSE_ISSUER
    except Exception:
        return False


def assert_processing_allowed(
    license_key: Optional[str],
    *,
    state_path: Optional[Path] = None,
) -> None:
    """Exit-compatible guard used at audit start and per discovered file."""
    count = load_state(state_path)
    if count >= FREEMIUM_FILE_LIMIT and not is_license_valid(license_key):
        raise FreemiumLimitExceeded()


def record_processed_file(
    license_key: Optional[str],
    *,
    state_path: Optional[Path] = None,
) -> None:
    """Increment tamper-evident counter for each file entering the audit walk."""
    path = state_path or _default_state_path()
    assert_processing_allowed(license_key, state_path=path)
    save_state(load_state(path) + 1, state_path=path)
