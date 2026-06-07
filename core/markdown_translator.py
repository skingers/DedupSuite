"""Translate deduplicated unique files into a linked Obsidian knowledge graph.

The :class:`MarkdownTranslator` takes the unique ("golden") files surfaced by
:class:`~sovraan_core.FileAuditor` and renders one Markdown note per file inside
a flattened ``Obsidian_Export`` directory. Notes carry YAML frontmatter and are
cross-linked to a per-source-folder index note using Obsidian ``[[wikilinks]]``
so the resulting vault is navigable bi-directionally.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Union

_SAFE_CHARS = re.compile(r"[^0-9A-Za-z_-]+")


@dataclass
class UniqueFileRecord:
    """A single unique file emitted by the deduplication engine.

    Attributes:
        path: Original absolute path of the unique file.
        file_hash: SHA-256 hex digest of the file's contents.
        notary_status: Current notary state (e.g. ``PENDING``/``SUBMITTED``).
        created: Optional ISO-8601 creation timestamp. When omitted the
            translator derives it from the filesystem if the file exists.
    """

    path: Path
    file_hash: str
    notary_status: str = "PENDING"
    created: Optional[str] = None


@dataclass
class TranslationResult:
    """Summary of a translation run, returned by :meth:`MarkdownTranslator.translate`."""

    export_dir: Path
    note_paths: List[Path] = field(default_factory=list)
    index_paths: List[Path] = field(default_factory=list)


def _sanitise(name: str) -> str:
    """Collapse a string into an Obsidian-safe note stem."""
    cleaned = _SAFE_CHARS.sub("_", name).strip("_")
    return cleaned or "untitled"


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


class MarkdownTranslator:
    """Render unique files into a flattened, wiki-linked Obsidian vault."""

    def __init__(self, export_root: Union[str, Path], export_dirname: str = "Obsidian_Export") -> None:
        """Initialise the translator.

        Args:
            export_root: Base directory under which the export folder is created.
            export_dirname: Name of the dedicated export directory.
        """
        self.export_dir = Path(export_root) / export_dirname

    def translate(self, files: Iterable[Union[UniqueFileRecord, Mapping[str, object]]]) -> TranslationResult:
        """Generate Markdown notes and folder-index notes for ``files``.

        Every file is flattened to a single note at the top level of the export
        directory (nested source paths do not create nested output folders).
        Name collisions are disambiguated with a short content hash. Each note
        links to the index note of its original source folder, and each index
        note links back to its member notes, yielding bi-directional links.

        Args:
            files: Iterable of :class:`UniqueFileRecord` or equivalent mappings
                with ``path``/``file_hash`` (and optional ``notary_status``,
                ``created``) keys.

        Returns:
            A :class:`TranslationResult` describing the files written.
        """
        records = [self._coerce(f) for f in files]
        self.export_dir.mkdir(parents=True, exist_ok=True)

        used_note_stems: Dict[str, int] = {}
        # Map absolute source folder -> (index_note_stem, [member_note_stems])
        folder_index_stems: Dict[Path, str] = {}
        used_index_stems: Dict[str, int] = {}
        folder_members: Dict[Path, List[str]] = {}
        record_note_stem: List[str] = []

        # First pass: assign a unique flattened note stem to every record and
        # register it against its source folder.
        for record in records:
            note_stem = self._unique_stem(_sanitise(record.path.stem), record, used_note_stems)
            record_note_stem.append(note_stem)

            parent = self._parent_key(record.path)
            if parent not in folder_index_stems:
                base = f"INDEX_{_sanitise(parent.name) or 'root'}"
                folder_index_stems[parent] = self._unique_index_stem(base, parent, used_index_stems)
                folder_members[parent] = []
            folder_members[parent].append(note_stem)

        result = TranslationResult(export_dir=self.export_dir)

        # Second pass: write file notes (linking up to their folder index).
        for record, note_stem in zip(records, record_note_stem):
            parent = self._parent_key(record.path)
            index_stem = folder_index_stems[parent]
            note_path = self.export_dir / f"{note_stem}.md"
            note_path.write_text(self._render_note(record, index_stem), encoding="utf-8")
            result.note_paths.append(note_path)

        # Third pass: write the folder index notes (linking back to members).
        for parent, index_stem in folder_index_stems.items():
            index_path = self.export_dir / f"{index_stem}.md"
            index_path.write_text(
                self._render_index(parent, folder_members[parent]), encoding="utf-8"
            )
            result.index_paths.append(index_path)

        return result

    @staticmethod
    def _coerce(item: Union[UniqueFileRecord, Mapping[str, object]]) -> UniqueFileRecord:
        if isinstance(item, UniqueFileRecord):
            return item
        return UniqueFileRecord(
            path=Path(str(item["path"])),
            file_hash=str(item["file_hash"]),
            notary_status=str(item.get("notary_status", "PENDING")),
            created=(str(item["created"]) if item.get("created") is not None else None),
        )

    @staticmethod
    def _parent_key(path: Path) -> Path:
        parent = path.parent
        try:
            return parent.resolve()
        except OSError:
            return parent

    def _unique_stem(self, base: str, record: UniqueFileRecord, used: Dict[str, int]) -> str:
        if base not in used:
            used[base] = 1
            return base
        used[base] += 1
        # Disambiguate flattened collisions deterministically by content hash.
        suffix = _short_hash(f"{record.path}|{record.file_hash}")
        return f"{base}_{suffix}"

    @staticmethod
    def _unique_index_stem(base: str, parent: Path, used: Dict[str, int]) -> str:
        if base not in used:
            used[base] = 1
            return base
        used[base] += 1
        return f"{base}_{_short_hash(str(parent))}"

    def _resolve_created(self, record: UniqueFileRecord) -> str:
        if record.created:
            return record.created
        try:
            if record.path.exists():
                ts = record.path.stat().st_ctime
                return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
        except OSError:
            pass
        return "unknown"

    def _render_note(self, record: UniqueFileRecord, index_stem: str) -> str:
        created = self._resolve_created(record)
        # Quote string values so Windows backslashes never break the YAML.
        frontmatter = (
            "---\n"
            f'file_hash: "{record.file_hash}"\n'
            f'original_path: "{self._yaml_escape(str(record.path))}"\n'
            f'created: "{created}"\n'
            f'notary_status: "{record.notary_status}"\n'
            "---\n"
        )
        body = (
            f"\n# {record.path.name}\n\n"
            f"- **Source folder:** [[{index_stem}]]\n"
            f"- **SHA-256:** `{record.file_hash}`\n"
        )
        return frontmatter + body

    def _render_index(self, parent: Path, member_stems: List[str]) -> str:
        frontmatter = (
            "---\n"
            "type: folder-index\n"
            f'source_folder: "{self._yaml_escape(str(parent))}"\n'
            f"file_count: {len(member_stems)}\n"
            "---\n"
        )
        lines = "\n".join(f"- [[{stem}]]" for stem in member_stems)
        body = f"\n# Folder Index: {parent.name or parent}\n\n{lines}\n"
        return frontmatter + body

    @staticmethod
    def _yaml_escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')
