# 🚀 DedupSuite v2.0

## Overview

DedupSuite is an enterprise-grade utility for stateful data rationalization. It performs exact, bit-level hash auditing to create a persistent historical ledger of your entire file estate, transforming unstructured data silos into a verified, queryable archive. It is designed for professionals who require absolute certainty and a permanent record of their data's lifecycle.

## Key Features

### High-Fidelity Duplicate Detection (SHA-256)
Utilizes cryptographic SHA-256 hashing to identify true, bit-level duplicates, ignoring misleading filenames, dates, or metadata. This ensures that only genuinely redundant files are flagged for archival, providing a single source of truth.

### The SQLite Data Mine: A Persistent File Ledger
Unlike volatile scanners, DedupSuite maintains a local SQLite database (`data_mine.db`) that acts as a long-term historical ledger. It intelligently catalogs every file, designating the first-seen version as the **"Golden"** source of truth and all subsequent identical copies as **"Legacy"** duplicates. This stateful awareness is critical for long-term data governance.

### Automated Archiving with Governance Manifests
The Bulk Archive engine allows for the safe, transactional removal of thousands of "Legacy" files based on a specific audit session. Every archival operation is logged to a timestamped CSV manifest, providing a clear, professional audit trail for compliance and governance. A one-click revert function provides an essential safety net for all archival operations.

## Installation

DedupSuite is distributed as a single, standalone executable (`DedupSuite.exe`) for Microsoft Windows, built with PyInstaller.

No installation or external dependencies are required for the end-user. Simply download the application from the latest release and run it.

## 🛡️ Data Privacy & Governance

Your data's privacy and security are paramount. All file analysis and indexing occur exclusively on your local machine.

*   **Local Database:** The historical ledger (`data_mine.db`) is stored in the same directory as the application. This file contains all metadata about your scanned files. It is **never** transmitted or shared.

*   **Version Control:** This database file is your asset and liability. It contains the history of your file estate and should be backed up accordingly. It **MUST NOT** be committed to version control (e.g., Git). The project's `.gitignore` file is explicitly configured to prevent this, and this configuration should not be altered.