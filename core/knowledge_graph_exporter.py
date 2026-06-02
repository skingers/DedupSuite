"""Export knowledge graph from elevated sidecars in the DedupSuite Vault.

Scans the DEDUPSUITE_VAULT directory recursively to parse .md sidecars, extracts
metadata, original source path, creation date, and notary status to generate
a flattened graph structure of nodes and semantic relationship edges.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Common system/generic folder names to exclude from automatic project tags
IGNORE_TAGS: Set[str] = {
    "", "users", "desktop", "documents", "downloads", "dev", "projects",
    "tmp", "temp", "windows", "program files", "home", "var", "usr", "etc",
    "bin", "opt", "c", "d", "e", "f", "g", "h"
}


def parse_frontmatter_and_body(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter and note body from a markdown sidecar.

    Supports simple scalar values (int, string, bool) and list structures.
    """
    metadata: Dict[str, Any] = {}
    body = ""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return metadata, content

    frontmatter_lines: List[str] = []
    in_frontmatter = False
    body_start_idx = 0
    for idx, line in enumerate(lines):
        if line.strip() == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            else:
                body_start_idx = idx + 1
                break
        if in_frontmatter:
            frontmatter_lines.append(line)

    body = "\n".join(lines[body_start_idx:])

    # Simple line-by-line parsing of simple YAML
    current_key: Optional[str] = None
    for line in frontmatter_lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check if line starts a new key
        if ":" in line and not stripped.startswith("-"):
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()

            # Flow-style list like [tag1, tag2]
            if val.startswith("[") and val.endswith("]"):
                items = [x.strip().strip('"\'') for x in val[1:-1].split(",") if x.strip()]
                metadata[key] = items
                current_key = None
            elif not val:
                # Might be a block-style list on subsequent lines
                metadata[key] = []
                current_key = key
            else:
                # Scalar value
                val = val.strip('"\'')
                if val.isdigit():
                    metadata[key] = int(val)
                elif val.lower() == "true":
                    metadata[key] = True
                elif val.lower() == "false":
                    metadata[key] = False
                else:
                    metadata[key] = val
                current_key = None
        elif current_key and stripped.startswith("-"):
            # Block list item under current_key
            val = stripped.lstrip("-").strip().strip('"\'')
            if isinstance(metadata[current_key], list):
                metadata[current_key].append(val)

    return metadata, body


def parse_date(date_str: str) -> Optional[datetime.date]:
    """Parse a date string (ISO timestamp or YYYY-MM-DD) into a date object."""
    if not date_str:
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", date_str)
    if match:
        try:
            return datetime.date(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            )
        except ValueError:
            pass
    return None


def extract_project_tags_from_path(original_path_str: str) -> List[str]:
    """Extract and sanitize directory segments from source paths to act as project tags."""
    tags: List[str] = []
    if not original_path_str:
        return tags
    try:
        path = Path(original_path_str)
        current = path.parent
        levels = 0
        while current and current.name and levels < 4:
            name = current.name
            name_lower = name.lower()
            if name_lower not in IGNORE_TAGS and not name_lower.startswith("depth_"):
                cleaned = re.sub(r"[^0-9A-Za-z_-]+", "_", name).strip("_")
                if cleaned:
                    tags.append(cleaned)
            current = current.parent
            levels += 1
    except Exception:
        pass
    return tags


class KnowledgeGraphExporter:
    """Crawls a vault, extracts file metadata, and outputs a relationship graph JSON."""

    def __init__(self, vault_path: str, db_path: Optional[str] = None) -> None:
        self.vault_path = os.path.abspath(vault_path)
        if db_path:
            self.db_path = os.path.abspath(db_path)
        else:
            # Default to data_mine.db in the application root
            self.db_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data_mine.db"
            )

    def lookup_db_metadata(self, file_hash: Optional[str] = None, file_name: Optional[str] = None) -> Dict[str, Any]:
        """Query the SQLite database to resolve original path, ots proof size, and classification."""
        db_metadata: Dict[str, Any] = {}
        if not os.path.exists(self.db_path):
            return db_metadata

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            row = None

            if file_hash:
                cursor.execute(
                    "SELECT full_path, sha256_hash, file_name, ots_proof, classification, status "
                    "FROM file_index WHERE sha256_hash = ? LIMIT 1",
                    (file_hash,)
                )
                row = cursor.fetchone()

            if not row and file_name:
                cursor.execute(
                    "SELECT full_path, sha256_hash, file_name, ots_proof, classification, status "
                    "FROM file_index WHERE file_name = ? LIMIT 1",
                    (file_name,)
                )
                row = cursor.fetchone()

            if row:
                full_path, sha256_hash, db_file_name, ots_proof, classification, status = row
                db_metadata["original_path"] = full_path
                db_metadata["file_hash"] = sha256_hash
                if ots_proof:
                    db_metadata["ots_proof_bytes"] = len(ots_proof)
                db_metadata["classification"] = classification
                db_metadata["notary_status"] = "ANCHORED" if ots_proof else "PENDING"
                if status:
                    db_metadata["status"] = status

            conn.close()
        except Exception:
            pass

        return db_metadata

    def scan_sidecars(self) -> List[Dict[str, Any]]:
        """Identify all .md sidecars in the vault and parse their content."""
        sidecars: List[Dict[str, Any]] = []
        if not os.path.exists(self.vault_path):
            return sidecars

        for root, _, files in os.walk(self.vault_path):
            # Ignore index databases and config directories
            if ".chroma_index" in root or ".git" in root:
                continue

            for file in files:
                if file.endswith(".md"):
                    filepath = os.path.join(root, file)
                    try:
                        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                    except Exception:
                        continue

                    meta, body = parse_frontmatter_and_body(content)
                    
                    # Accept sidecars containing (file_hash & original_path) OR (original_filename & vaulted_name)
                    is_sidecar = (
                        ("file_hash" in meta and "original_path" in meta) or
                        ("original_filename" in meta and "vaulted_name" in meta)
                    )
                    
                    if is_sidecar:
                        rel_path = os.path.relpath(filepath, self.vault_path)
                        rel_path_unix = rel_path.replace(os.sep, "/")
                        sidecars.append({
                            "filepath": filepath,
                            "rel_path": rel_path_unix,
                            "metadata": meta,
                            "body": body
                        })
        return sidecars

    def build_graph(self) -> Dict[str, Any]:
        """Crawl the vault, construct graph nodes, and map semantic edges."""
        sidecars = self.scan_sidecars()
        nodes: List[Dict[str, Any]] = []

        # Maps for quick adjacency grouping
        tag_to_nodes: Dict[str, List[str]] = collections.defaultdict(list)
        date_to_nodes: Dict[str, List[str]] = collections.defaultdict(list)
        hash_to_nodes: Dict[str, List[str]] = collections.defaultdict(list)

        for sc in sidecars:
            meta = sc["metadata"]
            body = sc["body"]
            node_id = sc["rel_path"]
            filepath = sc["filepath"]

            # Compute hash fallback by reading binary companion asset if not in frontmatter
            file_hash = meta.get("file_hash")
            companion_name = meta.get("vaulted_name")
            if not file_hash and companion_name:
                companion_path = os.path.join(os.path.dirname(filepath), companion_name)
                if os.path.exists(companion_path):
                    h = hashlib.sha256()
                    try:
                        with open(companion_path, "rb") as bf:
                            while chunk := bf.read(65536):
                                h.update(chunk)
                        file_hash = h.hexdigest()
                    except Exception:
                        pass

            # Query database for missing metadata
            db_meta = self.lookup_db_metadata(
                file_hash=file_hash, 
                file_name=meta.get("original_filename") or meta.get("original_name")
            )

            # Resolve paths, hashes, and status fields using db fallbacks
            orig_path = meta.get("original_path") or db_meta.get("original_path")
            file_hash = file_hash or db_meta.get("file_hash")
            notary_status = meta.get("notary_status") or db_meta.get("notary_status") or "PENDING"
            ots_proof_bytes = meta.get("ots_proof_bytes") or db_meta.get("ots_proof_bytes") or 0

            # 1. Project Context Extraction
            project_tags: Set[str] = set()

            # Frontmatter tag fields
            for key in ("tags", "project_tags", "project"):
                val = meta.get(key)
                if isinstance(val, list):
                    for item in val:
                        project_tags.add(str(item).strip())
                elif isinstance(val, str) and val:
                    project_tags.add(val.strip())

            # Original path parent segments
            if orig_path:
                for tag in extract_project_tags_from_path(orig_path):
                    project_tags.add(tag)

            # Inline markdown tags (#tag-name)
            for tag in re.findall(r"(?<!\w)#([a-zA-Z][a-zA-Z0-9/_-]*)", body):
                project_tags.add(tag)

            # Wiki-links to INDEX files
            for link in re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", body):
                link_clean = link.strip()
                if link_clean.startswith("INDEX_"):
                    project_tags.add(link_clean)

            # 2. Date Tag Extraction
            date_tags: Set[str] = set()
            created = meta.get("created")
            
            # Find date by checking created metadata or parsing filename/folder
            d = parse_date(created)
            if not d:
                # Try filename YYYY-MM-DD
                match = re.search(r"(\d{4})-(\d{2})-(\d{2})", os.path.basename(filepath))
                if match:
                    try:
                        d = datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                    except ValueError:
                        pass
            if not d:
                # Try parent directory YYYY-MM-DD
                parent_dir = os.path.basename(os.path.dirname(filepath))
                match = re.search(r"(\d{4})-(\d{2})-(\d{2})", parent_dir)
                if match:
                    try:
                        d = datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                    except ValueError:
                        pass
            if not d:
                # Try filesystem timestamp
                try:
                    mtime = os.path.getmtime(filepath)
                    d = datetime.date.fromtimestamp(mtime)
                except Exception:
                    pass

            if d:
                date_tags.add(f"year_{d.year:04d}")
                date_tags.add(f"month_{d.year:04d}_{d.month:02d}")
                date_tags.add(d.strftime("%Y-%m-%d"))
                created_str = d.strftime("%Y-%m-%d")
            else:
                created_str = created or "unknown"

            # Build Node structure
            node = {
                "id": node_id,
                "file_name": os.path.basename(sc["filepath"]),
                "vault_relative_path": node_id,
                "original_path": orig_path,
                "file_hash": file_hash,
                "created": created_str,
                "notary_status": notary_status,
                "project_tags": sorted(list(project_tags)),
                "date_tags": sorted(list(date_tags)),
                "metadata": {
                    "original_name": (
                        meta.get("original_filename") or 
                        meta.get("original_name") or 
                        (os.path.basename(orig_path) if orig_path else os.path.basename(filepath))
                    ),
                    "ots_proof_bytes": ots_proof_bytes,
                    "ots_proof_storage": meta.get("ots_proof_storage") or "file_index.ots_proof"
                }
            }
            nodes.append(node)

            # Update indices for edge grouping
            for tag in project_tags:
                tag_to_nodes[tag].append(node_id)
            if d:
                date_to_nodes[d.strftime("%Y-%m-%d")].append(node_id)
            if file_hash:
                hash_to_nodes[file_hash].append(node_id)

        edges: List[Dict[str, Any]] = []
        seen_edges: Set[Tuple[str, str, str, str]] = set()

        def add_edge(u: str, v: str, edge_type: str, detail: str) -> None:
            u_node, v_node = (u, v) if u < v else (v, u)
            edge_key = (u_node, v_node, edge_type, detail)
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append({
                    "source": u_node,
                    "target": v_node,
                    "type": edge_type,
                    "detail": detail
                })

        # Relationship 1: Shared Project Tags
        for tag, node_ids in tag_to_nodes.items():
            for i in range(len(node_ids)):
                for j in range(i + 1, len(node_ids)):
                    add_edge(node_ids[i], node_ids[j], "shared_project_tag", tag)

        # Relationship 2: Same-day Adjacency
        for date_str, node_ids in date_to_nodes.items():
            for i in range(len(node_ids)):
                for j in range(i + 1, len(node_ids)):
                    add_edge(node_ids[i], node_ids[j], "date_based_adjacency", "same_day")

        # Relationship 3: Consecutive-day Adjacency
        unique_dates = sorted(list(date_to_nodes.keys()))
        for k in range(len(unique_dates) - 1):
            d1_str, d2_str = unique_dates[k], unique_dates[k + 1]
            d1, d2 = parse_date(d1_str), parse_date(d2_str)
            if d1 and d2 and (d2 - d1).days == 1:
                for u in date_to_nodes[d1_str]:
                    for v in date_to_nodes[d2_str]:
                        add_edge(u, v, "date_based_adjacency", "consecutive_day")

        # Relationship 4: Cryptographic Provenance (same file hash)
        for f_hash, node_ids in hash_to_nodes.items():
            if len(node_ids) > 1:
                for i in range(len(node_ids)):
                    for j in range(i + 1, len(node_ids)):
                        add_edge(node_ids[i], node_ids[j], "cryptographic_provenance", "shared_hash")

        return {
            "nodes": nodes,
            "edges": edges
        }

    def export(self, output_path: Optional[str] = None) -> str:
        """Construct relationship graph and write graph_manifest.json."""
        graph_data = self.build_graph()
        if not output_path:
            output_path = os.path.join(self.vault_path, "graph_manifest.json")

        output_dir = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(output_dir, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(graph_data, f, indent=2)

        return os.path.abspath(output_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scans the DEDUPSUITE_VAULT to extract metadata and export relationship graph."
    )
    parser.add_argument(
        "--vault",
        default=os.environ.get("DEDUPSUITE_VAULT"),
        help="Path to the DEDUPSUITE_VAULT to scan. Defaults to DEDUPSUITE_VAULT env var.",
    )
    parser.add_argument(
        "--db",
        help="Path to the SQLite database (defaults to data_mine.db in application root).",
    )
    parser.add_argument(
        "--output",
        help="Custom output file path for the graph_manifest.json (defaults to vault_path/graph_manifest.json).",
    )
    args = parser.parse_args()

    # Fallback to AppConfig settings
    vault_path = args.vault
    if not vault_path:
        try:
            from config_manager import AppConfig
            vault_path = AppConfig().get("vault_path")
        except Exception:
            pass

    if not vault_path:
        print("Error: No vault path specified. Provide --vault or set DEDUPSUITE_VAULT.")
        exit(1)

    print(f"Traversing vault: {vault_path}")
    exporter = KnowledgeGraphExporter(vault_path, db_path=args.db)
    sidecars = exporter.scan_sidecars()
    print(f"Found {len(sidecars)} matching elevated .md sidecar(s).")
    
    out_file = exporter.export(args.output)
    print(f"Successfully generated relationship graph manifest: {out_file}")


if __name__ == "__main__":
    main()
