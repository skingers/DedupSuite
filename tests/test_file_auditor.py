"""Unit tests for FileAuditor SHA-256 hashing and directory traversal."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List

import pytest

from dedup_suite import FileAuditor


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture()
def auditor(tmp_path: Path) -> FileAuditor:
    """A FileAuditor rooted at a temp dir with no DB and no GUI callbacks."""
    return FileAuditor(tmp_path, dry_run=True, review_mode=True, db_manager=None)


def test_hash_matches_reference_sha256(auditor: FileAuditor, tmp_path: Path) -> None:
    data = b"DedupSuite cryptographic payload \x00\x01\x02"
    target = _write(tmp_path / "a.bin", data)

    expected = hashlib.sha256(data).hexdigest()
    assert auditor.get_file_hash(target) == expected
    assert len(auditor.get_file_hash(target)) == 64


def test_hash_is_consistent_across_calls(auditor: FileAuditor, tmp_path: Path) -> None:
    target = _write(tmp_path / "repeat.bin", b"stable-content")
    first = auditor.get_file_hash(target)
    second = auditor.get_file_hash(target)
    assert first is not None
    assert first == second


def test_identical_files_produce_identical_hashes(auditor: FileAuditor, tmp_path: Path) -> None:
    content = b"the quick brown fox" * 4096
    a = _write(tmp_path / "one.dat", content)
    b = _write(tmp_path / "nested" / "two.dat", content)
    assert auditor.get_file_hash(a) == auditor.get_file_hash(b)


def test_different_content_produces_different_hashes(auditor: FileAuditor, tmp_path: Path) -> None:
    a = _write(tmp_path / "x.dat", b"alpha")
    b = _write(tmp_path / "y.dat", b"beta")
    assert auditor.get_file_hash(a) != auditor.get_file_hash(b)


def test_chunked_reading_is_size_invariant(auditor: FileAuditor, tmp_path: Path) -> None:
    """A large file hashed in tiny chunks must equal the single-shot digest."""
    data = b"x" * (1024 * 1024 + 1234)  # spans multiple 1 MiB chunks
    target = _write(tmp_path / "big.dat", data)
    assert auditor.get_file_hash(target, chunk_size=7) == hashlib.sha256(data).hexdigest()


def test_missing_file_returns_none_gracefully(auditor: FileAuditor, tmp_path: Path) -> None:
    assert auditor.get_file_hash(tmp_path / "does_not_exist.bin") is None


def test_partial_hash_covers_first_block(auditor: FileAuditor, tmp_path: Path) -> None:
    data = b"H" * 10000
    target = _write(tmp_path / "partial.dat", data)
    assert auditor.get_partial_hash(target) == hashlib.sha256(data[:4096]).hexdigest()


class _RecordingAuditor(FileAuditor):
    """FileAuditor that records duplicate groups instead of mutating files."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.groups: List[List[Path]] = []

    def handle_duplicates(self, file_list: List[Path]) -> None:
        self.groups.append(list(file_list))


def test_nested_directories_are_walked_and_grouped(tmp_path: Path) -> None:
    content = b"duplicate-bytes"
    _write(tmp_path / "a.txt", content)
    _write(tmp_path / "sub1" / "b.txt", content)
    _write(tmp_path / "sub1" / "sub2" / "c.txt", content)
    _write(tmp_path / "sub1" / "unique.txt", b"not-a-duplicate")

    auditor = _RecordingAuditor(tmp_path, dry_run=True, review_mode=False, db_manager=None)
    auditor.run()

    assert auditor.files_scanned == 4
    assert len(auditor.groups) == 1
    assert len(auditor.groups[0]) == 3
    assert {p.name for p in auditor.groups[0]} == {"a.txt", "b.txt", "c.txt"}
