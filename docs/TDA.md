# Technical Design Architecture (TDA)

## Document scope

This architecture record defines deterministic behaviour for DedupSuite 2.0 when **creation metadata is unavailable** during vault export. It complements `README.md` (operator-facing protocol) and the implementation in `db_ingest.py` (`extract_creation_date`, `build_note_basename`, `export_golden_vault`).

---

## 1. System context

DedupSuite maintains three coupled artefacts per golden file:

1. **Ledger row** — `file_index` (path, SHA-256, session, golden flag).
2. **Cryptographic proof** — Ed25519 `batch_signatures` + optional `blockchain_proofs`.
3. **Vault projection** — physical asset copy + Markdown sidecar for Obsidian.

Naming is a **projection function** from ledger state to vault paths. It must be **total** (every golden row maps to exactly one vault location) and **injective up to collision repair** (distinct content hashes must not silently overwrite).

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
\text{note}(p) = \text{dir}(p) / \big( N_{\text{vault}}(p) \;||\; \text{``.md''} \big)
\]

Collision repair appends \(\pi(H(p) \;||\; \text{path}, 8)\) to the stem when either target exists (deterministic disambiguation).

### 2.6 Design invariants

| Invariant | Rationale |
|---|---|
| **No synthetic dates under blackout** | Prevents timeline corruption in vault navigation. |
| **Hash always participates in fallback** | Content identity survives rename/path drift on legacy media. |
| **Depth is structural, not temporal** | Folder graph index disambiguates same-hash-prefix collisions across branches. |
| **Single notary gate before write** | Sidecar `notary_status` reflects gateway receipt; no permanent `PENDING` when anchor succeeds. |
| **Idempotent export** | Re-running export with same ledger state yields equivalent paths modulo disambiguation suffix. |

### 2.7 State integrity & auditability

1. **Data preservation** — Source binaries are copied with `shutil.copy2` before the sidecar is sealed; embed wikilinks reference the co-located asset.
2. **State integrity** — `blockchain_proofs` rows transition to `SUBMITTED` only after synchronous `CloudNotaryBridge.anchor()` returns HTTP 200.
3. **Auditability** — Operators correlate vault stems (`Depth_*` vs `YYYY-MM-DD-*`) with `file_index.modified_time` and `batch_signatures` manifests for the ingest session.

### 2.8 Verification specifications

| Check | Method |
|---|---|
| Batch manifest integrity | `integrity_check.IntegrityCheck.verify_all()` |
| Golden set completeness | `COUNT(file_index WHERE is_golden=1 AND last_session_id=?)` |
| Blackout ratio | Export logs: `ANCHORED` / `PENDING` + count of `Depth_*` stems |
| Path injectivity | No overwrite without `_<hash8>` disambiguation suffix |

---

## 3. Module map

| Concern | Module |
|---|---|
| Date extraction & blackout detection | `db_ingest.extract_creation_date` |
| Basename selection | `db_ingest.build_note_basename` |
| Vault export orchestration | `db_ingest.export_golden_vault` |
| Production CLI | `run_production.py` (`--tree-mode`, `--hierarchical`, `--flat`) |
| Ed25519 batch proofs | `integrity_check.py`, `crypto_gate.py` |

---

## 4. Revision history

| Version | Change |
|---|---|
| 2.0.0 | Metadata Blackout & Anomaly Resolution Framework codified for production baseline. |
