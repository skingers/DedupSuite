import { createHash } from "crypto";
import { readFileSync } from "fs";
import { join } from "path";
import {
  addIcon,
  ItemView,
  Notice,
  Plugin,
  TFile,
  type WorkspaceLeaf,
} from "obsidian";
import { DedupDBReader } from "./db_reader";
import { ExecutionEngine } from "./execution_engine";
import {
  DEFAULT_SETTINGS,
  DedupSuiteSettingTab,
  resolveDatabasePath,
  SettingsManager,
  type DedupSuiteSettings,
} from "./settings";

const RIBBON_ICON_ID = "dedupsuite-ribbon";
const RIBBON_FALLBACK_ICON = "shield";
const RIBBON_SVG_FILENAME = "dedupsuite icon ONLY_3.svg";
const RIBBON_SOURCE_VIEWBOX = 512;
const OBSIDIAN_ICON_VIEWBOX = 100;
const RIBBON_ICON_SCALE = OBSIDIAN_ICON_VIEWBOX / RIBBON_SOURCE_VIEWBOX;

export function iconSvgForObsidian(raw: string): string {
  const trimmed = raw.trim();
  const match = trimmed.match(/<svg[^>]*>([\s\S]*)<\/svg>/i);
  let inner = (match ? match[1] : trimmed).trim();
  inner = inner.replace(/<rect[^>]*width="512"[^>]*\/?>\s*/i, "");
  inner = inner.replace(/\sfill="(?!none)[^"]*"/gi, ' fill="currentColor"');
  inner = inner.replace(/\sstroke="[^"]*"/gi, ' stroke="currentColor"');
  return `<g fill="currentColor" transform="scale(${RIBBON_ICON_SCALE})">${inner}</g>`;
}

export default class DedupSuiteBridgePlugin extends Plugin {
  settings: DedupSuiteSettings = { ...DEFAULT_SETTINGS };
  private settingsManager!: SettingsManager;

  async onload(): Promise<void> {
    this.settingsManager = new SettingsManager(this);
    await this.loadSettings();
    this.registerRibbonIcon();
    this.addSettingTab(new DedupSuiteSettingTab(this.app, this));

    if (this.settings.verifySignaturesOnStartup) {
      void this.runSignatureVerification();
    }

    this.addCommand({
      id: "dedupsuite-verify-signatures",
      name: "Verify ingest signatures (Ed25519)",
      callback: () => {
        void this.runSignatureVerification();
      },
    });

    this.addCommand({
      id: "dedupsuite-run-backend",
      name: "Run DedupSuite notarisation sweep on vault",
      callback: () => {
        void this.triggerDedupExecution();
      },
    });

    this.registerEvent(
      this.app.workspace.on("active-leaf-change", (leaf) => {
        void this.handleActiveLeafChange(leaf);
      })
    );
  }

  async loadSettings(): Promise<void> {
    await this.settingsManager.load();
    this.settings = { ...this.settingsManager.get() };
  }

  async saveSettings(): Promise<void> {
    await this.settingsManager.save();
    this.settings = { ...this.settingsManager.get() };
  }

  getSettings(): DedupSuiteSettings {
    return this.settings;
  }

  getSettingsManager(): SettingsManager {
    return this.settingsManager;
  }

  notifyUser(message: string, duration = 5000): void {
    new Notice(`DedupSuite: ${message}`, duration);
  }

  debugLog(message: string, detail?: unknown): void {
    if (this.settings.enableDebugLogging) {
      console.warn(`[DedupSuite] ${message}`, detail ?? "");
    }
  }

  private createDbReader(): DedupDBReader {
    const dbPath = resolveDatabasePath(this.settings.databasePath);
    return new DedupDBReader(dbPath, this.resolvePluginDir());
  }

  /**
   * Verifies every row in batch_signatures at the configured Database Path.
   */
  async runSignatureVerification(): Promise<void> {
    try {
      const reader = this.createDbReader();
      const result = await reader.verifyBatchSignatures();
      if (result.total === 0) {
        this.notifyUser(
          `No batch signatures in ledger (${reader.databasePath}). Run production ingest first.`,
          8000
        );
        return;
      }
      if (result.failedBatchIndices.length === 0) {
        this.notifyUser(
          `All ${result.valid}/${result.total} ingest batch signatures verified (Ed25519).`,
          8000
        );
        return;
      }
      this.notifyUser(
        `Signature verification failed: ${result.valid}/${result.total} valid. Failed batches: ${result.failedBatchIndices.join(", ")}`,
        12000
      );
      this.debugLog("Failed signature batches", result.failedBatchIndices);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.notifyUser(`Signature verification error: ${message}`, 10000);
      this.debugLog("Signature verification error", error);
    }
  }

  registerRibbonIcon(): void {
    const iconPath = join(
      this.app.vault.adapter.getBasePath?.() ?? "",
      this.manifest.dir ?? "",
      "assets",
      RIBBON_SVG_FILENAME
    );
    const onRibbonClick = () => {
      void this.triggerDedupExecution();
    };
    try {
      const rawSvg = readFileSync(iconPath, "utf8");
      addIcon(RIBBON_ICON_ID, iconSvgForObsidian(rawSvg));
      this.addRibbonIcon(RIBBON_ICON_ID, "DedupSuite 2.0", onRibbonClick);
    } catch (err) {
      console.warn("[DedupSuite] Ribbon icon failed to load, falling back:", err);
      this.addRibbonIcon(RIBBON_FALLBACK_ICON, "DedupSuite 2.0", onRibbonClick);
    }
  }

  triggerDedupExecution(): void {
    void this.runBackendSweep();
  }

  async runBackendSweep(): Promise<void> {
    const vaultBasePath = this.getVaultBasePath();
    if (!vaultBasePath) {
      this.notifyUser("Unable to resolve the vault path.", 5000);
      return;
    }
    try {
      const databasePath = resolveDatabasePath(this.settings.databasePath);
      const engine = new ExecutionEngine(
        this.settings.pythonPath,
        this.settings.backendScriptPath,
        databasePath
      );
      await engine.runBackend(vaultBasePath);
    } catch (error) {
      this.debugLog("Backend sweep failed", error);
    }
  }

  resolvePluginDir(): string {
    const vaultBasePath = this.getVaultBasePath();
    return join(vaultBasePath, this.manifest.dir ?? "");
  }

  getVaultBasePath(): string {
    const adapter = this.app.vault.adapter;
    return typeof adapter.getBasePath === "function" ? adapter.getBasePath() : "";
  }

  async handleActiveLeafChange(leaf: WorkspaceLeaf | null): Promise<void> {
    if (!leaf || !(leaf.view instanceof ItemView)) {
      return;
    }
    const file = this.app.workspace.getActiveFile();
    if (!(file instanceof TFile)) {
      return;
    }
    try {
      const binaryContents = await this.app.vault.readBinary(file);
      const calculatedHash = createHash("sha256").update(Buffer.from(binaryContents)).digest("hex");
      const reader = this.createDbReader();
      const indexRow = await reader.getFileIndexByHash(calculatedHash);
      const proof = await reader.getBlockchainProof(calculatedHash);

      if (!indexRow && !proof) {
        this.notifyUser(`No ledger entry for "${file.path}".`, 5000);
        this.debugLog("No ledger row", { path: file.path, hash: calculatedHash });
        return;
      }

      const parts: string[] = [];
      if (indexRow?.full_path) {
        parts.push(`indexed: ${indexRow.full_path}`);
      }
      if (indexRow?.is_golden === 1) {
        parts.push("golden copy");
      }
      if (proof?.ots_proof_blob && proof.ots_proof_blob.length > 0) {
        const status = String(proof.status ?? "UNKNOWN");
        const badge =
          status.toUpperCase() === "SECURED"
            ? "BITCOIN SECURED"
            : "PENDING AUTOMATED NOTARISATION";
        parts.push(badge);
        parts.push(`updated ${String(proof.updated_at ?? "N/A")}`);
      } else if (indexRow) {
        parts.push("no OpenTimestamps proof yet");
      }

      this.notifyUser(`${file.path}\n${parts.join("\n")}`, 6000);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.notifyUser(`Ledger lookup failed: ${message}`, 5000);
      this.debugLog("Active file lookup skipped", message);
    }
  }
}
