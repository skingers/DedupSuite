# Technical Architecture Blueprint

This document details the system topology, layered architecture, data state specifications, and integrity checking mechanisms governing sovraan 2.0.0.

---

## 1. System Topology & Layers

sovraan is structured as a local-first, multi-layered system designed to minimize operational coupling and protect boundary lines between local and remote environments.

```
+-------------------------------------------------------------+
| Presentation & Orchestration Layer (CustomTkinter GUI)       |
+-------------------------------------------------------------+
| Obsidian Event Loop Layer (TypeScript Frontend Plugin)      |
+-------------------------------------------------------------+
| Processing & Hashing Layer (WAL Relational Ingestion)        |
+-------------------------------------------------------------+
| Memory Core & Index Layer (ChromaDB / SentenceTransformers) |
+-------------------------------------------------------------+
| Remote Trust Boundary (Decoupled Async Notary VM Endpoint) |
+-------------------------------------------------------------+
```

### 1.1 Presentation & Orchestration Layer (Local GUI)
*   **Implementation**: Implemented in [sovraan_core.py](file:///c:/Users/marks/dev/projects/sovraan/sovraan_core.py) utilizing a tabbed layout (**Your Journey**, **Merge Folders**, **Expert Studio**, **The Vault Index**).
*   **Orchestration**: Directs the **Calm Journey** workflow:
    1.  **Map the Swamp**: Scan raw files and record metadata.
    2.  **Secure the Gold**: Resolve duplicates and execute golden-state promotion.
    3.  **Ignite Your Mind**: Export files to the vault, generate the knowledge graph manifest, and build vector indexes.

### 1.2 Processing & Ingest Layer (Workers)
*   **Ingest Pipeline**: Implemented in `pipeline.py` and `db_ingest.py`. Launches multiple background worker threads (defaulting to 8 producers) to scan and hash files concurrently in chunks of 64. A single consumer thread manages database writes.

### 1.3 State & Storage Layer (SQLite)
*   **Database**: `data_mine.db` holds the schema for:
    *   `file_index`: Records path, size, modified times, hashes, and notary metadata.
    *   `blockchain_proofs`: Stores OTS notary proof blobs and transaction logs.
*   *WAL Mode* is enabled to allow concurrent reads and single-writer isolation without database locks.

### 1.4 Memory Core & Indexing Layer (Vector DB)
*   **Vector Engine**: Described in [vector_engine.py](file:///c:/Users/marks/dev/projects/sovraan/core/vector_engine.py). Coordinates the loading of the graph manifest and sidecar file body strings.
*   **Embedding Pipeline**: Runs entirely locally using SentenceTransformers loaded with the `all-MiniLM-L6-v2` model. Emitted vectors (384 dimensions) are stored in ChromaDB (.chroma_index) using persistent disk mapping.

### 1.5 Remote Notary Interface (Transit Boundary)
*   **Decoupling**: Files are asynchronously submitted to the GCP Micro VM Notary API endpoint `http://34.13.47.2:5000/api/v1/anchor` using unblocked daemon queues, ensuring that network failures do not freeze scanning operations.

---

## 2. Core Data State & Manifest Formats

### 2.1 Knowledge Graph Manifest (`graph_manifest.json`)
The manifest utilizes path-normalized SHA-256 hash IDs for referential integrity:
*   **Nodes**:
    *   `id`: `SHA-256(vault_relative_path)`
    *   `vault_relative_path`: Unix-normalized path relative to the vault root (e.g. `2026/2026-06-02/file.md`).
    *   `file_hash`: SHA-256 hash of the original source file.
    *   `project_tags` / `date_tags`: Automatically extracted from sidecar frontmatter, inline markdown `#tags`, and wiki-link structures.
*   **Edges**:
    *   `source` and `target`: Point to node `id` hashes.
    *   `type`: Categorizes relationships (e.g. `shared_project_tag`, `cryptographic_provenance`, `date_based_adjacency`).

### 2.2 Vector Store Schema (.chroma_index)
Each document represented in ChromaDB maps to exactly one manifest node:
*   **ID**: Identical SHA-256 `node_id`.
*   **Document**: Enriched text format combining file name, tags, path, and parsed body content.
*   **Metadata**: Flat primitives mapping `vault_relative_path`, `file_name`, `original_name`, `original_path`, and `file_hash`.

---

## 3. The Validator (Integrity Gate)

To guarantee that the indexed semantic memory remains in complete alignment with physical files on disk, the system runs an automated integrity gate. 

```
[Query text] 
     │
     ▼ (local embed)
[Query Vector] ──► [ChromaDB Lookup] ──► [Matched Node Metadata]
                                                 │
                                                 ▼ (verify path)
                                         [Check Original File]
                                                 │
                                                 ▼ (100% match)
                                         [Compare binary bytes]
```

1.  **Semantic Retrieval**: Query texts are embedded and evaluated against ChromaDB.
2.  **Path Resolution**: The top result's `original_path` (absolute path to disk) and `vault_relative_path` (vault companion asset path) are extracted.
3.  **Fidelity Assertions**: The validator runs exact binary comparisons (`read_bytes()`) and hash comparisons between the source file on disk and the companion asset in the vault to enforce complete fidelity.
