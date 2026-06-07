"""Absolute-path production configuration for sovraan 2.0."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set


@dataclass(frozen=True)
class ProductionPaths:
    """Resolved absolute paths for a production ingest run."""

    source: Path
    destination: Path
    database: Path


def require_absolute_path(value: str, label: str) -> Path:
    """Resolve ``value`` and require a fully absolute path."""
    resolved = Path(value).expanduser().resolve()
    if not Path(value).expanduser().is_absolute():
        raise argparse.ArgumentTypeError(
            f"{label} must be an absolute path (got: {value!r})"
        )
    return resolved


def add_production_path_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``--source``, ``--destination``, and ``--db`` on ``parser``."""
    parser.add_argument(
        "--source",
        required=True,
        type=lambda v: require_absolute_path(v, "--source"),
        metavar="DIR",
        help="Absolute path to raw, un-deduplicated source files.",
    )
    parser.add_argument(
        "--destination",
        required=True,
        type=lambda v: require_absolute_path(v, "--destination"),
        metavar="DIR",
        help="Absolute path to the Obsidian vault (export target).",
    )
    parser.add_argument(
        "--db",
        required=True,
        type=lambda v: require_absolute_path(v, "--db"),
        metavar="PATH",
        help="Absolute path to the production SQLite database file.",
    )


def paths_from_namespace(args: argparse.Namespace) -> ProductionPaths:
    """Build :class:`ProductionPaths` from parsed CLI arguments."""
    return ProductionPaths(
        source=args.source,
        destination=args.destination,
        database=args.db,
    )


def _normalise_ignore_exts(exts: Sequence[str]) -> Set[str]:
    out: Set[str] = set()
    for ext in exts:
        token = ext.strip().lower()
        if not token:
            continue
        if not token.startswith("."):
            token = f".{token}"
        out.add(token)
    return out


def _normalise_ignore_folders(folders: Sequence[str]) -> Set[str]:
    return {f.strip().lower() for f in folders if f.strip()}


def collect_ingest_paths(
    source: Path,
    *,
    ignore_exts: Optional[Iterable[str]] = None,
    ignore_folders: Optional[Iterable[str]] = None,
) -> List[Path]:
    """Walk ``source`` and return file paths ready for :func:`pipeline.run_pipeline`."""
    root = source.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Source is not a directory: {root}")

    ext_filter = _normalise_ignore_exts(ignore_exts or ())
    folder_filter = _normalise_ignore_folders(ignore_folders or ())
    paths: List[Path] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in folder_filter]
        for filename in filenames:
            if filename.startswith("."):
                continue
            if ext_filter and filename.lower().endswith(tuple(ext_filter)):
                continue
            paths.append((Path(dirpath) / filename).resolve())

    return paths
