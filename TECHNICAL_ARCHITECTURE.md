# Technical Architecture

## 1. Executive Summary

DedupSuite is a high-scale cryptographic deduplication platform designed to prioritise local integrity, deterministic hashing, and independent trust anchoring. The architecture centralises primary scan and classification workloads on the operator workstation while delegating final anchoring to an isolated cloud endpoint. This split model reduces operational coupling, improves resilience, and enables defensible auditability for long-term data governance.

At platform level, the system combines:

- local high-throughput hash-based deduplication;
- persistent relational state management in SQLite;
- asynchronous background notary dispatch; and
- an independent distributed cloud trust anchor pathway.

## 2. System Topology

The topology is intentionally multi-process and layered:

1. **Presentation and orchestration layer (local GUI)**
   - `dedup_suite.py` coordinates scan lifecycle, review flows, archive actions, and user prompts.
2. **Processing layer (local worker threads)**
   - content hashing, media analysis, and duplicate grouping run in background threads.
3. **State layer (local SQLite)**
   - `data_mine.db` persists file index records, golden-state logic, and blockchain proof metadata.
4. **Notary integration layer (local async daemon thread)**
   - `DedupNotary.batch_submit_unnotarised()` executes after audit closure without blocking UI control.
5. **Remote trust anchor layer (isolated GCP Micro VM)**
   - cloud-facing anchoring endpoint: `http://34.13.47.2:5000/api/v1/anchor`.

This arrangement links a local dedup engine to an isolated Google Cloud Platform Micro VM backend, keeping interactive operations performant while segregating external network trust operations.

## 3. Security Architecture

Security posture is based on minimised exposure and constrained ingress:

- ingress firewall rules are bound to a single authenticated local development IP where feasible;
- Gunicorn/Flask workloads on Port `5000` are shielded from broad public visibility;
- local SQLite interactions use explicit parameterised queries for mutation paths;
- foreign key enforcement is enabled at connection initialisation points;
- asynchronous notary submission is decoupled to reduce blast radius of network faults.

Operationally, the architecture categorises trust boundaries into:

- **Local trusted zone**: scanning engine, GUI controller, and SQLite state.
- **Remote constrained zone**: cloud notary microservice endpoint.
- **Transit boundary**: outbound-only anchoring requests with strict exception handling.

## 4. Operational Protocols

Service reliability and process hygiene are managed through daemonised operations:

- remote notary stack is managed by `systemd` via `notary.service`;
- service runtime should execute under unprivileged user profiles;
- restart policies provide automatic fallback/restart loops on transient failure;
- local GUI remains non-blocking by offloading notary submission into daemon threads;
- all high-risk paths (database locking, network timeout, remote refusal) are trapped and logged.

Recommended operations practice:

1. compile-check modules before release;
2. validate DB schema migrations idempotently;
3. monitor notary service health and restart counters;
4. verify endpoint reachability and TLS/proxy posture where applicable.

## 5. Data & Risk Management

DedupSuite uses cryptographic anchor hashing matrices to ensure local database integrity and state preservation:

- SHA-256 hashes are the canonical identity for exact-match equivalence;
- proof blobs and statuses are persisted in `blockchain_proofs` for replayable verification state;
- dedup session metadata tracks temporal progression and rationalisation outcomes;
- failure handling is designed to continue batch progression instead of halting entire pipelines.

Primary risk controls include:

- **Data integrity**: explicit schema guarantees plus deterministic hash identity.
- **Availability**: non-blocking async submission and restartable remote daemon services.
- **Operational safety**: strong ignore rules for runtime artefacts and temporary scripts.
- **Traceability**: structured logs and persistent status transitions (`PENDING` to `SUBMITTED`).
