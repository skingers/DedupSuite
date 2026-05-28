# DedupSuite

DedupSuite is a desktop-first deduplication and audit platform that combines local cryptographic scanning with optional cloud notary anchoring. The application analyses file estates, indexes results in SQLite, identifies duplicate content, and supports controlled archival workflows with a review-friendly GUI.

## Core Capabilities

- SHA-256 content hashing for exact duplicate detection.
- Visual/video perceptual matching for media-oriented review paths.
- Session-aware SQLite indexing for historical deduplication state.
- Background OpenTimestamps submission pipeline via `core/notary.py`.
- Cloud Oracle targeting for anchoring workflows at `http://34.13.47.2:5000/api/v1/anchor`.

## Local Execution Guide

1. **Clone and enter the repository**
   - `git clone https://gitlab.com/skingers/DedupSuite.git`
   - `cd DedupSuite`
2. **Install dependencies**
   - `pip install -r requirements.txt`
3. **Run the application**
   - `python dedup_suite.py`
4. **Select a source path in the GUI**
   - Use `Audit / Dedup` to run Exact or Visual/Video scans.
5. **Review and process outputs**
   - Inspect duplicates, archive safely, and validate state in `Data Mine`.

## Deployment Overview

DedupSuite is architected for a hybrid local-plus-cloud flow:

- **Local host** runs `dedup_suite.py`, performs scanning/hashing, and writes to `data_mine.db`.
- **Background notary worker** (`DedupNotary`) runs in a daemon thread after audit completion and submits pending hashes asynchronously.
- **Remote Cloud Notary integration** is mapped to the dedicated endpoint on port `5000`:
  - `http://34.13.47.2:5000/api/v1/anchor`
- **User interface remains responsive** while background anchoring continues.

## Repository Notes

- Keep runtime artefacts untracked (`data_mine.db`, logs, cache files, and temporary scripts).
- Use `main` as the production branch and push only verified, compile-clean changes.
- Do not commit local secrets, private manifests, or host-specific scratch assets.
