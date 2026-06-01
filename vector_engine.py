import os
import chromadb
import threading
import time

class SovereignVectorEngine:
    def __init__(self, vault_path: str) -> None:
        """Initialize a local ChromaDB persistent client pointing to vault_path/.chroma_index."""
        self.vault_path = os.path.abspath(vault_path)
        index_dir = os.path.join(self.vault_path, ".chroma_index")
        self.client = chromadb.PersistentClient(path=index_dir)
        self.collection = self.client.get_or_create_collection(name="sovereign_vault")

    def sync_index(self, progress_callback=None) -> None:
        """Walk the vault_path, extract metadata, chunk markdown/text content, and upsert to ChromaDB."""
        # Walk the vault_path using os.walk
        for root, _, files in os.walk(self.vault_path):
            # Ignore the chroma index folder itself to prevent scanning DB binary files
            if ".chroma_index" in root:
                continue

            for file in files:
                if file.endswith((".md", ".txt")):
                    filepath = os.path.join(root, file)
                    
                    # Metadata Extraction: source (2 levels up), year (1 level up), filename
                    rel_path = os.path.relpath(filepath, self.vault_path)
                    parts = rel_path.split(os.sep)
                    filename = parts[-1]
                    year = parts[-2] if len(parts) >= 2 else "Unknown"
                    source = parts[-3] if len(parts) >= 3 else "Unknown"

                    # Safe reading of content
                    try:
                        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                    except Exception:
                        continue

                    # Chunking by paragraphs (splitting by \n\n)
                    chunks = [c.strip() for c in content.split("\n\n") if c.strip()]
                    if not chunks:
                        continue

                    ids = []
                    documents = []
                    metadatas = []

                    for i, chunk in enumerate(chunks):
                        # Construct a unique ID for each chunk using relative path
                        chunk_id = f"{rel_path.replace(os.sep, '_')}_chunk_{i}"
                        ids.append(chunk_id)
                        documents.append(chunk)
                        metadatas.append({
                            "source": source,
                            "year": year,
                            "filename": filename
                        })

                    # Database Upsert
                    try:
                        self.collection.upsert(
                            ids=ids,
                            documents=documents,
                            metadatas=metadatas
                        )
                        if progress_callback:
                            progress_callback(f"Indexed {filename}...")
                    except Exception:
                        continue

    def query_vault(self, query_text: str):
        """Query the collection and return the matched documents and metadata."""
        if self.collection.count() == 0:
            return {"documents": [], "metadatas": []}

        results = self.collection.query(
            query_texts=[query_text],
            n_results=3
        )

        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        return {"documents": docs, "metadatas": metas}
