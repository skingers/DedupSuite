# Release Notes: DedupSuite v2.0 (Production)

DedupSuite 2.0 is locked for production deployment: a Calm Journey operator interface, unrestricted cryptographic ingest, and hardened window behaviour on Windows.

---

## Production Finalization (Current)

### Calm Journey interface

* **Guided three-step audit** on the **Your Journey** tab: folder selection, live rescue status, and export mode before **Begin Rescue**.
* **Native mode control** in Step 3 (*Ignite Your Mind*): `CTkSegmentedButton` with `Standard Mode` and `Intelligence Mode`, replacing custom mode cards that caused layout overlap.
* **Tab naming** aligned with operator language: **Expert Studio** (advanced scanner settings) and **The Vault Index** (ledger summary and archive).
* **Window policy**: minimum size **1100 × 700** for taskbar-safe layouts on scaled displays; deferred `zoomed` state after UI build.

### Unrestricted ingest engine

* **Trial golden-file cap removed** — audits no longer halt with `Trial limit of 1000 Golden Files reached.` The default `run_pipeline()` path uses unlimited concurrent ingest.
* **Discovery testing cap removed** — `FileAuditor` and `VideoFileAuditor` process every file discovered under the target root (respecting ignore rules and Stop/Pause only).

---

## Prior Release Highlights

### Features & enhancements

* **High-fidelity hash enforcement** — `FileAuditor` computes SHA-256 for every file before database insertion in exact-audit mode.
* **Concurrent Sovereign Stack** — producer/consumer pipeline with Ed25519 batch manifests (`pipeline.py`, `crypto_gate.py`).

### Bug fixes

* **Session completion metrics** — completion reporting uses directory-scoped duplicate counts where session filters previously returned zero.
* **Archive operation reporting** — bulk archive popups reflect the physical `moved_count` from the relocation loop.

### Security

* **Database isolation** — `data_mine.db` and journals are gitignored to prevent accidental commit of private path inventories.

---

## Operator checklist

1. Launch `python dedup_suite.py` (or the PyInstaller build from `dedup_suite.spec`).
2. Select source folder on **Your Journey** → Step 1.
3. Choose export mode on Step 3 → run **Begin Rescue**.
4. Verify ingest progress in **Background notes**; confirm vault state under **The Vault Index**.
5. Optional: run `integrity_check.py` against `data_mine.db` before production vault export via `run_production.py`.
