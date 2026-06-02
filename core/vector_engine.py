"""Sovereign Vector Engine for DedupSuite.

Ingests graph_manifest.json, loads companion sidecar contents, embeds them using
sentence-transformers, and stores/queries the vectors via a local ChromaDB instance.
"""

from __future__ import annotations

import json
import os
import chromadb
from sentence_transformers import SentenceTransformer
from typing import Any, Dict, List, Optional

from core.knowledge_graph_exporter import parse_frontmatter_and_body


class SovereignVectorEngine:
    """Ingests note content and graph metadata into a local persistent vector store."""

    def __init__(self, vault_path: str, collection_name: str = "sovereign_vault") -> None:
        """Initialize ChromaDB pointing to vault_path/.chroma_index."""
        self.vault_path = os.path.abspath(vault_path)
        index_dir = os.path.join(self.vault_path, ".chroma_index")
        
        self.client = chromadb.PersistentClient(path=index_dir)
        self.collection = self.client.get_or_create_collection(name=collection_name)
        self._model = None

    @property
    def model(self) -> SentenceTransformer:
        """Lazily load the SentenceTransformer model to optimize import/startup times."""
        if self._model is None:
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
        return self._model

    def ingest_manifest(self, manifest_path: Optional[str] = None) -> int:
        """Parse graph_manifest.json, read node bodies, generate embeddings, and upsert to ChromaDB."""
        if not manifest_path:
            manifest_path = os.path.join(self.vault_path, "graph_manifest.json")

        if not os.path.exists(manifest_path):
            raise FileNotFoundError(f"Manifest file not found at: {manifest_path}")

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)

        nodes = manifest_data.get("nodes", [])
        if not nodes:
            return 0

        ids = []
        documents = []
        metadatas = []

        for node in nodes:
            node_id = node.get("id")
            rel_path = node.get("vault_relative_path")
            if not node_id or not rel_path:
                continue

            # Read full note body from vault file
            filepath = os.path.join(self.vault_path, rel_path)
            body = ""
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    _, body = parse_frontmatter_and_body(content)
                except Exception:
                    pass

            # Build enriched text representing the node's semantics
            project_tags_str = ", ".join(node.get("project_tags", []))
            date_tags_str = ", ".join(node.get("date_tags", []))
            original_path = node.get("original_path") or ""
            file_name = node.get("file_name") or ""
            created = node.get("created") or ""
            notary_status = node.get("notary_status") or "PENDING"

            doc_text = (
                f"File: {file_name}\n"
                f"Original Path: {original_path}\n"
                f"Created: {created}\n"
                f"Project Tags: {project_tags_str}\n"
                f"Date Tags: {date_tags_str}\n"
                f"Notary Status: {notary_status}\n\n"
                f"Content:\n{body.strip()}"
            )

            # Metadata values in ChromaDB must be primitives (str, int, float, bool)
            meta = {
                "vault_relative_path": rel_path,
                "file_name": file_name,
                "original_name": node.get("metadata", {}).get("original_name", ""),
                "original_path": original_path,
                "file_hash": node.get("file_hash") or "",
                "created": created,
                "notary_status": notary_status,
                "project_tags": project_tags_str,
                "date_tags": date_tags_str
            }

            ids.append(node_id)
            documents.append(doc_text)
            metadatas.append(meta)

        if ids:
            # Generate vectors
            embeddings = self.model.encode(documents, show_progress_bar=False).tolist()
            
            # Upsert into ChromaDB
            self.collection.upsert(
                ids=ids,
                embeddings=embeddings,
                metadatas=metadatas,
                documents=documents
            )

        return len(ids)

    def query_index(self, query_text: str, n_results: int = 3) -> List[Dict[str, Any]]:
        """Perform semantic search using query embeddings and return formatted matches."""
        total_items = self.collection.count()
        if total_items == 0:
            return []

        query_embedding = self.model.encode([query_text], show_progress_bar=False).tolist()
        
        results = self.collection.query(
            query_embeddings=query_embedding,
            n_results=min(n_results, total_items)
        )

        matches = []
        ids = results.get("ids", [[]])[0]
        distances = results.get("distances", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        documents = results.get("documents", [[]])[0]

        for i in range(len(ids)):
            matches.append({
                "id": ids[i],
                "distance": distances[i],
                "metadata": metadatas[i],
                "document": documents[i],
                "vault_relative_path": metadatas[i].get("vault_relative_path", "")
            })

        return matches
