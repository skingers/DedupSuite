"""OpenTimestamps proof generation for the sovraan forensic ledger."""

from __future__ import annotations

import sys
from typing import Optional, Tuple

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
