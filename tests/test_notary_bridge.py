"""Tests for the CloudNotaryBridge REST client (mocked requests)."""

from __future__ import annotations

from typing import List, Optional

import pytest
import requests

from network.notary_bridge import CloudNotaryBridge, DEFAULT_GATEWAY_URL

HASH = "f" * 64


class _FakeResponse:
    def __init__(self, status_code: int, json_body: Optional[dict] = None) -> None:
        self.status_code = status_code
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    """Replays a scripted sequence of responses or raised exceptions."""

    def __init__(self, outcomes: List[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls: List[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _bridge(session: _FakeSession, **kwargs) -> CloudNotaryBridge:
    slept: List[float] = []
    bridge = CloudNotaryBridge(
        session=session,
        sleep=lambda s: slept.append(s),
        logger=lambda m: None,
        backoff_base=0.5,
        **kwargs,
    )
    bridge.slept = slept  # type: ignore[attr-defined]
    return bridge


def test_payload_shape_and_default_endpoint() -> None:
    bridge = CloudNotaryBridge()
    assert bridge.gateway_url == DEFAULT_GATEWAY_URL
    payload = bridge.build_payload(HASH, client_timestamp="2026-05-29T10:00:00+00:00")
    assert payload == {"file_hash": HASH, "client_timestamp": "2026-05-29T10:00:00+00:00"}


def test_auto_timestamp_is_iso8601() -> None:
    payload = CloudNotaryBridge().build_payload(HASH)
    assert payload["file_hash"] == HASH
    # ISO-8601 with timezone offset; must round-trip through fromisoformat.
    import datetime
    datetime.datetime.fromisoformat(payload["client_timestamp"])


def test_200_ok_returns_success() -> None:
    session = _FakeSession([_FakeResponse(200, {"anchored": True})])
    bridge = _bridge(session)
    result = bridge.anchor(HASH)

    assert result.ok is True
    assert result.status == 200
    assert result.attempts == 1
    assert result.response == {"anchored": True}
    assert session.calls[0]["url"] == DEFAULT_GATEWAY_URL
    assert session.calls[0]["json"]["file_hash"] == HASH


def test_transient_503_then_200_succeeds_with_backoff() -> None:
    session = _FakeSession([_FakeResponse(503), _FakeResponse(429), _FakeResponse(200, {})])
    bridge = _bridge(session)
    result = bridge.anchor(HASH)

    assert result.ok is True
    assert result.attempts == 3
    # Exponential backoff between the two transient failures: base*2^0, base*2^1.
    assert bridge.slept == [0.5, 1.0]


def test_persistent_500_exhausts_retries_gracefully() -> None:
    session = _FakeSession([_FakeResponse(500) for _ in range(4)])
    bridge = _bridge(session, max_retries=4)
    result = bridge.anchor(HASH)

    assert result.ok is False
    assert result.status == "exhausted"
    assert result.attempts == 4
    # Backoff happens between attempts, not after the final one.
    assert bridge.slept == [0.5, 1.0, 2.0]


def test_non_transient_400_fails_fast() -> None:
    session = _FakeSession([_FakeResponse(400, {"error": "bad hash"})])
    bridge = _bridge(session, max_retries=4)
    result = bridge.anchor(HASH)

    assert result.ok is False
    assert result.status == 400
    assert result.attempts == 1
    assert bridge.slept == []


def test_offline_connection_error_degrades_gracefully() -> None:
    session = _FakeSession([requests.exceptions.ConnectionError("network down")])
    bridge = _bridge(session)
    result = bridge.anchor(HASH)

    assert result.ok is False
    assert result.status == "offline"
    assert result.error and "network down" in result.error


def test_timeout_degrades_gracefully() -> None:
    session = _FakeSession([requests.exceptions.Timeout("read timed out")])
    bridge = _bridge(session)
    result = bridge.anchor(HASH)

    assert result.ok is False
    assert result.status == "offline"
