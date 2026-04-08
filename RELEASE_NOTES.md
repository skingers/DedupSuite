# Release Notes: DedupSuite v2.0 (Stable)

## 🖋️ Executive Summary
Version 2.0 represents a significant architectural shift from a "global" duplicate finder to a "stateful" information governance utility. This release introduces Session-Based Isolation and Hash-Only Audit protocols, designed specifically to manage high-volume, unstructured data environments (20,000+ files) with surgical precision.

## 🚀 New Features & Enhancements

### 🛡️ Session-Based Isolation
* **Transactional Integrity:** Introduced a unique `session_id` for every audit. This ensures that archival actions only target files within the current search scope, preventing "historical ghosts" from contaminating active workflows.
* **Scoped Archiving:** The Bulk Archive confirmation logic now strictly filters by the active path and session, providing an accurate pre-flight check for the task at hand.

### 🧬 High-Fidelity Hash Audit
* **Cryptographic DNA:** Refactored the "Exact Audit" to prioritise SHA-256 bit-level hashing. 
* **Metadata Independence:** The engine now correctly identifies duplicates even when file names have been altered (e.g., Windows " - copy" suffixes) or modification timestamps have changed during migration.

### ⏪ Enhanced Recovery & Governance
* **Transactional Revert:** Improved the robustness of the 1-click "Undo" feature, ensuring archival moves can be rolled back to their original paths using the SQLite flight-recorder.
* **Audit Manifests (v2):** Redesigned CSV manifests to include session metadata, facilitating compliance and legal oversight.

## 🛠️ Bug Fixes
* **Resolved:** `TypeError` in `FileAuditor` and `DatabaseManager` related to mismatched session arguments.
* **Fixed:** "Ghosting" issue where the UI reported global database statistics instead of active search results.
* **Fixed:** Layout conflict where the 'Revert' button was occasionally obscured by the scrolling results window.

## 🏛️ Strategic Alignment
This version establishes DedupSuite as a primary tool for **Knowledge Readiness**. By ensuring data hygiene at the file-system level, v2.0 provides a clean, high-fidelity foundation for Retrieval-Augmented Generation (RAG) and LLM-based knowledge management.