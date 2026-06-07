# Technical Design Architecture (TDA)

## Document scope

This architecture record defines sovraan 2.0 production behaviour: metadata-blackout naming, the **relational forensic ledger**, internal OpenTimestamps BLOB storage, and dual-profile vault export. It complements `README.md`, `TECHNICAL_ARCHITECTURE.md`, and the implementation in `db_ingest.py`, `core/ots_proof.py`, and `run_production.py`.

**Operational context (v2.0 locked):** The Calm Journey GUI (`sovraan_core.py`) drives local audits without trial or discovery file caps. Ingest is unlimited via `run_pipeline(trial_golden_limit=None)` and full-tree `os.walk` discovery. Vault export semantics in this document apply to all golden rows produced by that unrestricted ingest path.

---

## 1. System context

sovraan maintains a **self-contained forensic registry** and an optional vault projection:

1. **Ledger row** — `file_index` (path, SHA-256, session, golden flag, **`ots_proof` BLOB**).
2. **Ingest integrity** — Ed25519 `batch_signatures` (canonical manifests per batch commit).
3. **Background notary table** — `blockchain_proofs` (batch OTS worker via `DedupNotary`; optional).
4. **Vault projection** — physical asset copy; Markdown sidecar **only** when export mode \(\in \{\texttt{plm}, \texttt{obsidian}\}\).

Let \(E \in \{\texttt{standard}, \texttt{plm}, \texttt{obsidian}\}\) be the export profile (`run_production.py --export-mode`, default `standard`).

Naming and filesystem layout are **projection functions** from ledger state to vault paths. They must be **total** (every golden row maps to exactly one asset location) and **injective up to collision repair** (distinct content hashes must not silently overwrite).

**OTS storage invariant:** OpenTimestamps serialised proof bytes are written to `file_index.ots_proof` during export. No `.ots` files are emitted under the vault root.

---

## 2. Metadata Blackout & Anomaly Resolution Framework

### 2.1 Definitions

Let:

- \(F\) = set of files selected for export (golden, session-scoped).
- \(p \in F\) = absolute source path.
- \(H(p)\) = SHA-256 hex digest of file contents (64 characters).
- \(M(p)\) = multiset of candidate epoch timestamps from `(modified_time, st_birthtime, st_ctime, st_mtime)`.
- \(\tau(p)\) = calendar date extracted from \(M(p)\) when valid; **undefined** when metadata is missing or corrupt.

**Validity predicate** \(V(t)\):

\[
V(t) \iff \text{year}(t) \geq 1980 \;\land\; \text{date}(t) \leq \text{today} + 1\,\text{day}
\]

\[
\tau(p) =
\begin{cases}
\text{date}(t) & \text{if } \exists\, t \in M(p) : V(t) \\
\bot & \text{otherwise}
\end{cases}
\]

When \(\tau(p) = \bot\), the file is in **metadata blackout** (anomaly state).

### 2.2 Folder graph depth index

Let \(R\) be the configured scan root. Define relative depth:

\[
\delta(p) = \left| \text{rel}(p, R) \right| - 1
\]

where \(\text{rel}(p,R)\) is the relative path from \(R\) to \(p\). Examples:

| Relative path | \(\delta(p)\) |
|---|---|
| `R/photo.jpg` | `0` |
| `R/2003/photo.jpg` | `1` |
| `R/a/b/c/doc.pdf` | `3` |

Depth tokens `0`, `01`, `1`, … are **folder graph indexes**, not calendar years. Leading zeros (`01`) may appear when normalising legacy directory labels; they must not be interpreted as dates.

### 2.3 Content hash prefix

Let \(\pi(H, k)\) denote the first \(k\) hexadecimal characters of \(H(p)\) (default \(k = 8\)):

\[
\pi(H(p), 8) = H(p)_{0:8}
\]

### 2.4 Naming functions

**Primary (chronological) basename** — applies when \(\tau(p) \neq \bot\):

\[
N_{\text{chrono}}(p) = \text{ISO8601}(\tau(p)) \;||\; \text{``-''} \;||\; \text{sanitise}(\text{stem}(p))
\]

Example: `2024-03-15-holiday`.

**Fallback (blackout) logical identity** — applies when \(\tau(p) = \bot\):

\[
N_{\text{fallback}}(p) = \text{str}(\delta(p)) \;||\; \text{``\_''} \;||\; \pi(H(p), 8)
\]

Example: `0_0ef1fdf7`.

**Vault filesystem stem** (implementation normalisation in `db_ingest`):

\[
N_{\text{vault}}(p) = \text{``Depth\_''} \;||\; \pi(H(p), 8) \;||\; \text{``-''} \;||\; \text{sanitise}(\text{stem}(p))
\]

The `Depth_` prefix marks blackout exports in Obsidian without ambiguous numeric-only stems.

### 2.5 Path resolution

**Hierarchical mode** (`hierarchical = true`, \(\tau(p) \neq \bot\)):

\[
\text{dir}(p) = \text{vault} / \text{YYYY}(\tau(p)) / \text{YYYY-MM-DD}(\tau(p))
\]

**Blackout layout** (\(\tau(p) = \bot\)): assets remain under `vault` root (no false dated folders).

Final paths:

\[
\text{asset}(p) = \text{dir}(p) / \big( N_{\text{vault}}(p) \;||\; \text{ext}(p) \big)
\]
\[
\text{note}(p) =
\begin{cases}
\text{dir}(p) / \big( N_{\text{vault}}(p) \;||\; \text{``.md''} \big) & \text{if } E \in \{\texttt{plm}, \texttt{obsidian}\} \\
\varnothing & \text{if } E = \texttt{standard}
\end{cases}
\]

Collision repair appends \(\pi(H(p) \;||\; \text{path}, 8)\) to the stem when either target exists (deterministic disambiguation).

### 2.5.1 Export profile semantics

| Profile | Sidecar generation | Vault clutter |
|---|---|---|
| `standard` | \(\text{note}(p) = \varnothing\) | Physical assets only — clean data lake for standard users |
| `plm`, `obsidian` | YAML frontmatter + structural body + `![[asset]]` embed | Assets plus tokenisation-ready Markdown |

### 2.5.2 OpenTimestamps persistence

During export, for each golden row \(p\):

1. `stamp_ots_proof(H(p))` → serialised proof bytes \(B(p)\) via `core.ots_proof.build_opentimestamps_proof`.
2. `persist_file_index_ots_proof(p, B(p))` → `UPDATE file_index SET ots_proof = ? WHERE full_path = … AND sha256_hash = …`.
3. Asset copy to \(\text{asset}(p)\) when `ext(p)` is defined.
4. Optional sidecar write iff \(E \in \{\texttt{plm}, \texttt{obsidian}\}\).

\[
B(p) \subseteq \texttt{file\_index.ots\_proof}, \quad B(p) \not\subseteq \text{vault filesystem}
\]

### 2.6 Design invariants

| Invariant | Rationale |
|---|---|
| **No synthetic dates under blackout** | Prevents timeline corruption in vault navigation. |
| **Hash always participates in fallback** | Content identity survives rename/path drift on legacy media. |
| **Depth is structural, not temporal** | Folder graph index disambiguates same-hash-prefix collisions across branches. |
| **OTS before vault write** | Proof stamped and persisted to `file_index.ots_proof` before asset/sidecar I/O. |
| **Standard mode is asset-only** | No Markdown sidecars under `standard`; PLM/Obsidian modes add tokenisation notes. |
| **Idempotent export** | Re-running export with same ledger state yields equivalent paths modulo disambiguation suffix. |

### 2.7 State integrity & auditability

1. **Data preservation** — Source binaries are copied with `shutil.copy2` into the vault tree; proofs remain in SQLite.
2. **State integrity** — `file_index.ots_proof` holds the authoritative OTS blob per golden file at export time; `batch_signatures` covers ingest-batch Ed25519 integrity.
3. **Auditability** — Operators correlate vault stems (`Depth_*` vs `YYYY-MM-DD-*`), export mode \(E\), and `LENGTH(ots_proof)` without scanning for loose `.ots` files.

### 2.8 Verification specifications

| Check | Method |
|---|---|
| Batch manifest integrity | `integrity_check.IntegrityCheck.verify_all()` |
| Golden set completeness | `COUNT(file_index WHERE is_golden=1 AND last_session_id=?)` |
| OTS BLOB presence | `SELECT COUNT(*) FROM file_index WHERE is_golden=1 AND ots_proof IS NOT NULL` |
| Export profile | Vault glob: no `*.md` when \(E=\texttt{standard}\); sidecars present when \(E \in \{\texttt{plm},\texttt{obsidian}\}\) |
| Blackout ratio | Export logs + count of `Depth_*` stems |
| Path injectivity | No overwrite without `_<hash8>` disambiguation suffix |

---

## 3. Module map

| Concern | Module |
|---|---|
| Date extraction & blackout detection | `db_ingest.extract_creation_date` |
| Basename selection | `db_ingest.build_note_basename` |
| OTS proof generation | `core.ots_proof.build_opentimestamps_proof` |
| OTS BLOB persistence | `db_ingest.persist_file_index_ots_proof` |
| Vault export orchestration | `db_ingest.export_golden_vault` |
| Production CLI | `run_production.py` (`--export-mode`, `--tree-mode`, `--hierarchical`, `--flat`) |
| Ed25519 batch proofs | `integrity_check.py`, `crypto_gate.py` |

---

## 4. Revision history

| Version | Change |
|---|---|
| 2.0.0 | Metadata Blackout & Anomaly Resolution Framework codified for production baseline. |
| 2.0.1 | Forensic `file_index.ots_proof` BLOB, dual export profiles (`standard` / `plm` / `obsidian`), no vault `.ots` files. |
