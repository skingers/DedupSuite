# Release Notes: DedupSuite v2.0

This release focuses on enhancing the core auditing engine's reliability, fixing critical UI reporting bugs, and strengthening data privacy protocols.

## Features & Enhancements

*   **Feature: High-Fidelity Hash Enforcement**
    The `FileAuditor` has been upgraded to strictly calculate and enforce SHA-256 hashes for all files prior to their insertion into the database. This ensures that every record in an "Exact Audit" is based on a cryptographic signature, providing a more robust and reliable foundation for duplicate detection.

## Bug Fixes

*   **Fix: Corrected Session Completion Metrics**
    Resolved a critical UI silent failure where the completion popup would incorrectly report '0 duplicates' after a scan. This was traced to a session ID mismatch in the UI callback. The logic now bypasses the session ID for this specific UI report, instead relying on a wildcard search for the active directory (`%folder_name%`) to provide an accurate count of duplicates found in the target folder.

*   **Fix: Accurate Archive Operation Reporting**
    Corrected the 'Archive Complete' popup to accurately report the physical count of files moved. The bulk archive function now iterates a local variable (`moved_count`) during the `shutil.move` loop, ensuring the final count shown to the user matches the exact number of files physically relocated on disk.

## Security

*   **Security: Database Isolation**
    To protect sensitive user data, the `data_mine.db` file (containing all scanned file paths) and its related journal are now strictly isolated from version control via an updated `.gitignore` file. This prevents accidental commits of private user information to a source repository.