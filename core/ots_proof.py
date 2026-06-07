"""OpenTimestamps proof generation and verification for the sovraan forensic ledger."""

from __future__ import annotations

import sys
from typing import Any, Dict, Optional, Tuple

CALENDAR_URLS = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
    "https://ots.btc.catallaxy.com",
)
CALENDAR_TIMEOUT = 10


def build_opentimestamps_proof(file_hash_hex: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Stamp a SHA-256 digest and return the serialised OTS proof bytes.

    Returns:
        ``(proof_blob, error_message)`` — proof is ``None`` when stamping fails.
    """
    try:
        hash_bytes = bytes.fromhex(file_hash_hex)
    except ValueError as exc:
        return None, f"invalid hash hex: {exc}"

    if len(hash_bytes) != 32:
        return None, "hash must be 32 bytes"

    try:
        from opentimestamps.core.op import OpSHA256
        from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp
        from opentimestamps.core.serialize import BytesSerializationContext
        from opentimestamps.calendar import RemoteCalendar
    except ImportError as exc:
        return None, f"opentimestamps not available: {exc}"

    detached = DetachedTimestampFile(OpSHA256(), Timestamp(hash_bytes))
    submitted = False
    last_error: Optional[str] = None
    for url in CALENDAR_URLS:
        try:
            calendar = RemoteCalendar(url)
            calendar_timestamp = calendar.submit(
                detached.timestamp.msg, timeout=CALENDAR_TIMEOUT
            )
            detached.timestamp.merge(calendar_timestamp)
            submitted = True
        except Exception as cal_exc:
            last_error = str(cal_exc)
            print(f"[OTS] Calendar {url} unavailable: {cal_exc}", file=sys.stderr)
            continue

    if not submitted:
        return None, last_error or "no calendar accepted the timestamp"

    ctx = BytesSerializationContext()
    detached.serialize(ctx)
    proof_blob = ctx.getbytes()
    if not proof_blob:
        return None, "empty OpenTimestamps serialisation"
    return proof_blob, None


def verify_opentimestamps_proof(file_hash_hex: str, proof_blob: bytes) -> Dict[str, Any]:
    """Cryptographically validate a stored OTS proof against an expected file hash.

    Returns a JSON-serialisable dict consumed by the Obsidian plugin (SOV-SEC-015):

    - ``state``: ``invalid`` | ``pending`` | ``attested``
    - ``ok``: ``True`` when the proof structure is valid for the hash (pending counts)
    - ``block_height``: Bitcoin block height when attested, else ``None``
    - ``message``: short human-readable summary
    """
    normalised = (file_hash_hex or "").strip().lower()
    try:
        expected_digest = bytes.fromhex(normalised)
    except ValueError as exc:
        return {
            "ok": False,
            "state": "invalid",
            "block_height": None,
            "message": f"invalid hash hex: {exc}",
        }

    if len(expected_digest) != 32:
        return {
            "ok": False,
            "state": "invalid",
            "block_height": None,
            "message": "hash must be 32 bytes",
        }

    if not proof_blob:
        return {
            "ok": False,
            "state": "invalid",
            "block_height": None,
            "message": "empty proof blob",
        }

    try:
        from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
        from opentimestamps.core.serialize import DeserializationError, StreamDeserializationContext
        from opentimestamps.core.timestamp import DetachedTimestampFile
    except ImportError as exc:
        return {
            "ok": False,
            "state": "invalid",
            "block_height": None,
            "message": f"opentimestamps not available: {exc}",
        }

    try:
        detached = DetachedTimestampFile.deserialize(StreamDeserializationContext(proof_blob))
    except DeserializationError as exc:
        return {
            "ok": False,
            "state": "invalid",
            "block_height": None,
            "message": f"invalid OpenTimestamps proof: {exc}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "state": "invalid",
            "block_height": None,
            "message": f"could not deserialize proof: {exc}",
        }

    if detached.file_digest != expected_digest:
        return {
            "ok": False,
            "state": "invalid",
            "block_height": None,
            "message": "proof digest does not match file hash",
        }

    block_height: Optional[int] = None
    has_bitcoin = False
    has_pending = False

    for _msg, attestation in detached.timestamp.all_attestations():
        if isinstance(attestation, BitcoinBlockHeaderAttestation):
            has_bitcoin = True
            block_height = int(attestation.height)
        elif isinstance(attestation, PendingAttestation):
            has_pending = True

    if has_bitcoin:
        return {
            "ok": True,
            "state": "attested",
            "block_height": block_height,
            "message": f"Bitcoin block attestation present (height {block_height})",
        }

    if has_pending:
        return {
            "ok": True,
            "state": "pending",
            "block_height": None,
            "message": "OpenTimestamps proof valid; awaiting Bitcoin confirmation",
        }

    return {
        "ok": True,
        "state": "pending",
        "block_height": None,
        "message": "OpenTimestamps proof valid; no Bitcoin attestation yet",
    }
