"""REST client that routes notary anchoring through the GCP API Gateway.

The :class:`CloudNotaryBridge` replaces direct, unmanaged ``opentimestamps``
calls on the client. It POSTs a small JSON payload to the gateway, retries
transient server errors (HTTP 429/5xx) with exponential backoff, and degrades
gracefully when the host is offline so the local Markdown export is never
interrupted by network failure.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import requests

DEFAULT_GATEWAY_URL = "http://34.13.47.2:5000/api/v1/anchor"
# HTTP status codes treated as transient (worth retrying with backoff).
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass
class AnchorResult:
    """Outcome of an anchoring attempt.

    Attributes:
        ok: ``True`` only when the gateway accepted the anchor (HTTP 200).
        status: HTTP status code, or a string state such as ``"offline"`` or
            ``"exhausted"`` when no successful HTTP exchange occurred.
        attempts: Number of HTTP attempts made.
        payload: The JSON payload that was sent.
        response: Parsed JSON response body when available.
        error: Human-readable error description on failure.
    """

    ok: bool
    status: Any
    attempts: int
    payload: Dict[str, str]
    response: Optional[Any] = None
    error: Optional[str] = None


class CloudNotaryBridge:
    """Submit SHA-256 hashes to the sovereign notary gateway over HTTP."""

    def __init__(
        self,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        *,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Initialise the bridge.

        Args:
            gateway_url: Fully-qualified anchor endpoint on the API gateway.
            max_retries: Maximum number of HTTP attempts before giving up.
            backoff_base: Base seconds for exponential backoff (``base * 2**n``).
            timeout: Per-request timeout in seconds.
            session: Optional pre-configured :class:`requests.Session`.
            sleep: Sleep function (injectable for testing).
            logger: Optional callable used for warnings; defaults to ``print``.
        """
        self.gateway_url = gateway_url
        self.max_retries = max(1, max_retries)
        self.backoff_base = backoff_base
        self.timeout = timeout
        self.session = session or requests.Session()
        self._sleep = sleep
        self._log = logger or (lambda msg: print(msg))

    def build_payload(self, file_hash: str, client_timestamp: Optional[str] = None) -> Dict[str, str]:
        """Build the gateway JSON payload for a single hash.

        Args:
            file_hash: SHA-256 hex digest to anchor.
            client_timestamp: ISO-8601 timestamp; generated (UTC) when omitted.

        Returns:
            A dict with ``file_hash`` and ``client_timestamp`` keys.
        """
        if client_timestamp is None:
            client_timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return {"file_hash": file_hash, "client_timestamp": client_timestamp}

    def anchor(self, file_hash: str, client_timestamp: Optional[str] = None) -> AnchorResult:
        """Anchor a single hash, retrying transient errors with backoff.

        Network failures (offline host, DNS, connection reset, timeout) are
        caught and reported as a degraded result rather than raising, so the
        caller's local workflow continues uninterrupted.

        Args:
            file_hash: SHA-256 hex digest to anchor.
            client_timestamp: Optional ISO-8601 timestamp.

        Returns:
            An :class:`AnchorResult` describing the outcome.
        """
        payload = self.build_payload(file_hash, client_timestamp)
        attempts = 0
        last_status: Any = None

        for attempt in range(self.max_retries):
            attempts = attempt + 1
            try:
                response = self.session.post(self.gateway_url, json=payload, timeout=self.timeout)
            except requests.exceptions.RequestException as exc:
                # Offline / unreachable / timeout: degrade gracefully, never crash.
                self._log(f"[NOTARY-BRIDGE] Offline or unreachable, skipping anchor: {exc}")
                return AnchorResult(
                    ok=False, status="offline", attempts=attempts,
                    payload=payload, error=str(exc),
                )

            last_status = response.status_code
            if response.status_code == 200:
                return AnchorResult(
                    ok=True, status=200, attempts=attempts,
                    payload=payload, response=self._safe_json(response),
                )

            if response.status_code in TRANSIENT_STATUS:
                # Exponential backoff before the next attempt (if any remain).
                if attempt < self.max_retries - 1:
                    self._sleep(self.backoff_base * (2 ** attempt))
                continue

            # Non-transient error (e.g. 400/401/404): fail fast.
            return AnchorResult(
                ok=False, status=response.status_code, attempts=attempts,
                payload=payload, response=self._safe_json(response),
                error=f"Non-transient gateway response {response.status_code}",
            )

        self._log(f"[NOTARY-BRIDGE] Exhausted {attempts} attempts (last status {last_status}).")
        return AnchorResult(
            ok=False, status="exhausted", attempts=attempts,
            payload=payload, error=f"Gave up after {attempts} attempts; last status {last_status}",
        )

    @staticmethod
    def _safe_json(response: requests.Response) -> Optional[Any]:
        try:
            return response.json()
        except ValueError:
            return None
