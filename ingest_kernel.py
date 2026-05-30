"""Ingestion kernel for the DedupSuite pipeline.

`parse_metadata_and_hash` is the per-file worker invoked by the crawler. For a
readable file it returns::

    {
        "hash": "<sha256_hex>",
        "metadata": {"size": <int>, "mtime": <float>},
        "status": "success",
    }

For any file that cannot be read (permission denied, missing, locked, etc.) it
returns a stable failure record instead of raising::

    {"status": "read_error", "hash": None, "metadata": None}

Memory contract: files are hashed by streaming fixed-size chunks, never by
reading the whole file into memory. This keeps the resident footprint flat and
well under the 8GB budget regardless of individual file sizes.
"""

import hashlib
import os
import threading
from pathlib import Path

_BUF_SIZE = 1048576
_thread_bufs = threading.local()


def _read_buffer() -> bytearray:
    """Per-thread reusable buffer (safe under concurrent hashing)."""
    buf = getattr(_thread_bufs, "buf", None)
    if buf is None:
        buf = bytearray(_BUF_SIZE)
        _thread_bufs.buf = buf
    return buf


def parse_metadata_and_hash(file_path):
    """Fingerprint a single file and collect its basic metadata.

    Args:
        file_path: A path-like object (typically a ``pathlib.Path``) to a file.

    Returns:
        dict: A record with ``hash``, ``metadata`` and ``status`` keys. On any
        access error the ``status`` is ``"read_error"`` and ``hash``/``metadata``
        are ``None``. The function never raises for filesystem/permission
        problems, so a crawl loop can keep going.
    """
    path = file_path if isinstance(file_path, Path) else Path(file_path)

    try:
        with open(path, "rb") as handle:
            stat_result = os.fstat(handle.fileno())
            hasher = hashlib.sha256(usedforsecurity=False)
            buf = _read_buffer()
            while True:
                n = handle.readinto(buf)
                if not n:
                    break
                hasher.update(memoryview(buf)[:n])

        return {
            "hash": hasher.hexdigest(),
            "metadata": {
                "size": stat_result.st_size,
                "mtime": stat_result.st_mtime,
            },
            "status": "success",
        }
    except OSError:
        return {"status": "read_error", "hash": None, "metadata": None}
