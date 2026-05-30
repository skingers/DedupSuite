"""Cryptographic gate for the DedupSuite integrity layer.

Uses the OS credential vault (via :mod:`keyring`) to persist an Ed25519
keypair and :mod:`nacl.signing` to sign and verify compact metadata manifests
(file hash + timestamp).

Typical usage::

    gate = CryptoGate()
    manifest = gate.build_file_manifest(sha256_hex, mtime)
    signature = gate.sign_manifest(manifest)
    assert gate.verify_manifest(manifest, signature)
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Iterable, Optional, Sequence, Tuple, Union

import keyring
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

# OS vault namespace — isolated from other DedupSuite installs on the machine.
KEYRING_SERVICE = "DedupSuite"
KEYRING_SIGNING_SEED = "ed25519-signing-seed"
KEYRING_VERIFY_KEY = "ed25519-verify-key"

ManifestInput = Union[bytes, str, dict[str, Any]]
Row = Tuple[Any, dict]


class CryptoGate:
    """Ed25519 signing gate backed by the OS credential vault."""

    def __init__(self, *, service_name: str = KEYRING_SERVICE) -> None:
        self._service = service_name
        self._signing_key, self._verify_key = self._load_or_create_keypair()

    @property
    def verify_key(self) -> VerifyKey:
        """Public key used by verifiers (e.g. :class:`IntegrityCheck`)."""
        return self._verify_key

    @property
    def public_key_bytes(self) -> bytes:
        """Raw 32-byte Ed25519 public key."""
        return bytes(self._verify_key)

    def _load_or_create_keypair(self) -> Tuple[SigningKey, VerifyKey]:
        """Retrieve an existing keypair from the vault or generate and store one."""
        seed_b64 = keyring.get_password(self._service, KEYRING_SIGNING_SEED)
        verify_b64 = keyring.get_password(self._service, KEYRING_VERIFY_KEY)

        if seed_b64:
            seed = base64.b64decode(seed_b64.encode("ascii"))
            signing_key = SigningKey(seed)
            if verify_b64:
                stored_verify = base64.b64decode(verify_b64.encode("ascii"))
                if stored_verify != bytes(signing_key.verify_key):
                    raise ValueError(
                        "OS vault signing seed does not match stored verify key"
                    )
            return signing_key, signing_key.verify_key

        signing_key = SigningKey.generate()
        verify_key = signing_key.verify_key
        keyring.set_password(
            self._service,
            KEYRING_SIGNING_SEED,
            base64.b64encode(bytes(signing_key)).decode("ascii"),
        )
        keyring.set_password(
            self._service,
            KEYRING_VERIFY_KEY,
            base64.b64encode(bytes(verify_key)).decode("ascii"),
        )
        return signing_key, verify_key

    @staticmethod
    def canonical_manifest_bytes(manifest: ManifestInput) -> bytes:
        """Normalize a manifest to deterministic UTF-8 bytes for signing."""
        if isinstance(manifest, bytes):
            return manifest
        if isinstance(manifest, str):
            return manifest.encode("utf-8")
        return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )

    @staticmethod
    def build_file_manifest(file_hash: str, timestamp: float) -> dict[str, Any]:
        """Build a single-file metadata manifest."""
        return {"file_hash": file_hash, "timestamp": float(timestamp)}

    @classmethod
    def build_batch_manifest(
        cls, records: Sequence[Row], *, signed_at: Optional[float] = None
    ) -> dict[str, Any]:
        """Build a batch manifest from pipeline ``(path, record)`` rows."""
        entries = []
        for path, record in records:
            if record.get("status") != "success":
                continue
            meta = record["metadata"]
            entries.append(
                cls.build_file_manifest(record["hash"], meta["mtime"])
                | {"full_path": str(path)}
            )
        return {
            "signed_at": signed_at if signed_at is not None else time.time(),
            "entries": entries,
        }

    def sign_manifest(self, manifest: ManifestInput) -> bytes:
        """Sign a manifest and return the raw Ed25519 signature bytes."""
        payload = self.canonical_manifest_bytes(manifest)
        signed = self._signing_key.sign(payload)
        return signed.signature

    def verify_manifest(
        self,
        manifest: ManifestInput,
        signature: bytes,
        *,
        verify_key: Optional[VerifyKey] = None,
    ) -> bool:
        """Return True when ``signature`` is valid for ``manifest``."""
        payload = self.canonical_manifest_bytes(manifest)
        key = verify_key if verify_key is not None else self._verify_key
        try:
            key.verify(payload, signature)
            return True
        except BadSignatureError:
            return False


# Module-level default gate for pipeline / integrity helpers.
_default_gate: Optional[CryptoGate] = None


def get_crypto_gate() -> CryptoGate:
    """Return a process-wide :class:`CryptoGate` (lazy OS vault init)."""
    global _default_gate
    if _default_gate is None:
        _default_gate = CryptoGate()
    return _default_gate


if __name__ == "__main__":
    gate = CryptoGate()
    sample = gate.build_file_manifest(
        "ab" * 32,
        time.time(),
    )
    sig = gate.sign_manifest(sample)
    print("public_key:", gate.public_key_bytes.hex()[:16], "...")
    print("sign_ok:", gate.verify_manifest(sample, sig))
    print("tamper_ok:", gate.verify_manifest({"file_hash": "bad"}, sig))
