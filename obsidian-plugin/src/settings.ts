import { App, PluginSettingTab, Setting } from "obsidian";
import { isAbsolute, normalize } from "path";
import type sovraanBridgePlugin from "./main";

export interface sovraan {
  /** Absolute path to the production SQLite database (e.g. data_mine.db). */
  databasePath: string;
  pythonPath: string;
  backendScriptPath: string;
  enableDebugLogging: boolean;
  /** Verify Ed25519 batch_signatures rows on plugin load. */
  verifySignaturesOnStartup: boolean;
}

export const DEFAULT_SETTINGS: sovraan = Object.freeze({
  databasePath: "",
  pythonPath: "python",
  backendScriptPath: "",
  enableDebugLogging: false,
  verifySignaturesOnStartup: false,
});

export class PathValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PathValidationError";
  }
}

/** Normalise and require an absolute database path from settings. */
export function resolveDatabasePath(raw: string, label = "Database Path"): string {
  if (typeof raw !== "string" || raw.trim().length === 0) {
    throw new PathValidationError(`${label} must be configured in plugin settings.`);
  }
  const normalised = normalize(raw.trim());
  if (!isAbsolute(normalised)) {
    throw new PathValidationError(`${label} must be an absolute path.`);
  }
  return normalised;
}

export class SettingsManager {
  private settings: sovraan = { ...DEFAULT_SETTINGS };

  constructor(private readonly persistence: { loadData(): Promise<unknown>; saveData(data: unknown): Promise<void> }) {}

  async load(): Promise<sovraan> {
    const loaded = (await this.persistence.loadData()) as Partial<sovraan> & {
      debugMode?: boolean;
      autoVerifyOnStartup?: boolean;
    } | null;
    this.settings = { ...DEFAULT_SETTINGS, ...(loaded ?? {}) };
    if (loaded?.debugMode !== undefined && loaded.enableDebugLogging === undefined) {
      this.settings.enableDebugLogging = loaded.debugMode;
    }
    if (loaded?.autoVerifyOnStartup !== undefined && loaded.verifySignaturesOnStartup === undefined) {
      this.settings.verifySignaturesOnStartup = loaded.autoVerifyOnStartup;
    }
    return this.settings;
  }

  async save(): Promise<void> {
    await this.persistence.saveData(this.settings);
  }

  get(): sovraan {
    return this.settings;
  }

  async update(patch: Partial<sovraan>): Promise<void> {
    this.settings = { ...this.settings, ...patch };
    await this.save();
  }
}

export class sovraanSettingTab extends PluginSettingTab {
  constructor(app: App, private readonly plugin: sovraanBridgePlugin) {
    super(app, plugin);
  }

  display(): void {
    const { containerEl } = this;
    containerEl.empty();
    const manager = this.plugin.getSettingsManager();
    const settings = manager.get();

    new Setting(containerEl)
      .setName("Database Path")
      .setDesc(
        "Absolute path to the sovraan production SQLite database. Used for file_index lookups, metadata, and batch_signatures verification."
      )
      .addText((text) =>
        text
          .setPlaceholder("D:\\sovraan\\data_mine.db")
          .setValue(settings.databasePath)
          .onChange(async (value) => {
            await manager.update({ databasePath: value.trim() });
            await this.plugin.saveSettings();
          })
      );

    new Setting(containerEl)
      .setName("Python interpreter")
      .setDesc("Command or absolute path used to launch the sovraan backend.")
      .addText((text) =>
        text
          .setPlaceholder("python")
          .setValue(settings.pythonPath)
          .onChange(async (value) => {
            await manager.update({ pythonPath: value.trim() });
            await this.plugin.saveSettings();
          })
      );

    new Setting(containerEl)
      .setName("Backend script path")
      .setDesc("Absolute path to sovraan_core.py (headless ingest / notarise).")
      .addText((text) =>
        text
          .setPlaceholder("D:\\sovraan\\sovraan_core.py")
          .setValue(settings.backendScriptPath)
          .onChange(async (value) => {
            await manager.update({ backendScriptPath: value.trim() });
            await this.plugin.saveSettings();
          })
      );

    new Setting(containerEl)
      .setName("Enable debug logging")
      .setDesc("Log diagnostic messages to the developer console.")
      .addToggle((toggle) =>
        toggle.setValue(settings.enableDebugLogging).onChange(async (value) => {
          await manager.update({ enableDebugLogging: value });
          await this.plugin.saveSettings();
        })
      );

    new Setting(containerEl)
      .setName("Verify signatures on startup")
      .setDesc(
        "On load, verify every Ed25519 signature in batch_signatures at the configured Database Path (no Python spawn)."
      )
      .addToggle((toggle) =>
        toggle.setValue(settings.verifySignaturesOnStartup).onChange(async (value) => {
          await manager.update({ verifySignaturesOnStartup: value });
          await this.plugin.saveSettings();
        })
      );
  }
}
