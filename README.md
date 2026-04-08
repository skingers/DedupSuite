🚀 DedupSuite v2.0
Stateful Data Rationalisation, Governance & Knowledge Readiness

🖋️ Overview
DedupSuite is a high-performance, industrial-grade utility designed to resolve the crisis of "Information Rot"—the accumulation of vast, unstructured data silos that impede current operations and degrade the efficacy of modern AI tools.

Built specifically for legal, administrative and technical professionals, this engine transforms chaotic file systems into structured, verified archives. By employing bit-level SHA-256 integrity checks and a stateful SQLite "Data Mine", it ensures that only the most accurate, "Golden" versions of your data are retained for active use in LLMs (Large Language Models), RAG (Retrieval-Augmented Generation) systems and professional audits.

🏗️ Core Strategic Pillars
1. The Data Mine (Stateful Persistence)
Unlike "volatile" duplicate finders that forget results as soon as they are closed, DedupSuite maintains a persistent SQLite index. This serves as a long-term "File Ledger", tracking the health and location of up to 100,000+ records. It provides the foundation for incremental data management—analysing only what has changed while remembering the state of your entire estate.

2. High-Fidelity Information Readiness
For data to be useful in RAG or AI contexts, it must be unique and verified.

Bit-Level Auditing: Uses cryptographic hashing to identify exact duplicates, even when filenames have been obfuscated or timestamps altered by OS migrations.

Redundancy Elimination: Drastically reduces the "token noise" and storage costs associated with feeding redundant data into AI models.

3. Transactional Bulk Archiving
The "Bulk Archive" engine allows for the surgical removal of redundant data.

Metadata-Driven Sorting: Files are automatically categorised into time-stamped archives based on their lineage.

Atomic Operations: Ensures the database and the physical disk remain in a state of perfect synchronicity. If a file move fails, the database reflects the truth.

4. The "Flight Recorder" (Safety & Governance)
Data management is a high-stakes task. DedupSuite provides enterprise-level safety nets:

1-Click Revert: A transactional "Undo" button that can roll back massive archival moves, restoring files to their original paths with zero data loss.

Audit Manifests: Generates comprehensive CSV logs for every action, providing a professional audit trail for compliance and legal oversight.

🛡️ Operational Safety
Dry Run Mode: Perform a full "Pre-Flight" simulation. Review exactly what will happen in a CSV report before a single byte is shifted.

Collision Protection: Intelligent renaming logic prevents data overwriting when multiple versions of a file (e.g. contract_final.pdf) exist in the same tree.

Session Isolation: Newly implemented logic ensures that incremental searches focus only on the task at hand, preventing "historical ghosts" from contaminating current archival actions.

🚀 Technical Applicability
Legal Discovery: Rationalise decades of disparate case files into a singular source of truth.

AI/LLM Pre-processing: Clean and deduplicate datasets to improve RAG retrieval accuracy and reduce costs.

Legacy Migration: Safely move tens of thousands of files from local drives to cloud repositories or cold storage.

🛠️ Installation & Usage
Prepare Environment: Ensure Python 3.10+ is installed.

Clone & Setup:
Bash
git clone https://gitlab.com/your-repo/dedupsuite.git
cd dedupsuite
pip install -r requirements.txt

Initialise Engine:
Bash
python dedup_suite.py

🏛️ Data Philosophy
“Information is only an asset if it can be found and verified. Otherwise, it is a liability.” DedupSuite was built to bridge the gap between "Digital Hoarding" and "Information Governance."