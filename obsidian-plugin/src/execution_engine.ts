import { spawn } from "child_process";
import { isAbsolute, normalize } from "path";
import { Notice } from "obsidian";
import { PathValidationError } from "./settings";

const ERROR_NOTICE_MS = 15_000;
const FORBIDDEN_PATH_PATTERN = /[\0\r\n;&|`$<>"']/;

export interface ExecutionResult {
  success: boolean;
  exitCode: number;
  stdout: string;
  stderr: string;
}

function translateExecutionError(error: unknown): string {
  if (error instanceof PathValidationError) {
    return `sovraan configuration is invalid: ${error.message}`;
  }
  const err = error instanceof Error ? error : new Error(String(error));
  const code = (err as NodeJS.ErrnoException).code;
  const combined = `${code ?? ""} ${err.message}`.toLowerCase();
  if (code === "ENOENT" || combined.includes("enoent")) {
    if (combined.includes("python") || combined.includes("spawn")) {
      return "sovraan could not find the Python interpreter. Check the Python path in settings.";
    }
    if (combined.includes(".py") || combined.includes("script")) {
      return "sovraan could not find the backend script. Check the backend script path in settings.";
    }
    return "sovraan could not find a required file or folder. Verify your paths in settings.";
  }
  if (code === "EACCES" || combined.includes("eacces") || combined.includes("permission denied")) {
    return "sovraan could not access a configured path. Check folder permissions.";
  }
  const backendExit = /^backend failed \(code (\d+)\)/i.exec(err.message);
  if (backendExit) {
    return `sovraan backend exited with an error (code ${backendExit[1]}). Enable debug logging for details.`;
  }
  if (combined.includes("failed to launch")) {
    return "sovraan could not start the Python backend. Check your Python and script paths in settings.";
  }
  return "sovraan encountered an unexpected error. Enable debug logging for technical details.";
}

function showUserError(error: unknown, duration = ERROR_NOTICE_MS): void {
  new Notice(translateExecutionError(error), duration);
}

function failSpawn(reject: (reason: Error) => void, error: unknown): void {
  const friendly = translateExecutionError(error);
  showUserError(error);
  reject(new Error(friendly));
}

export class ExecutionEngine {
  constructor(
    private readonly pythonPath: string,
    private readonly backendScriptPath: string,
    private readonly databasePath: string
  ) {}

  static sanitisePath(rawPath: string, label: string, requireAbsolute = true): string {
    if (typeof rawPath !== "string" || rawPath.trim().length === 0) {
      throw new PathValidationError(`${label} must be a non-empty string.`);
    }
    if (FORBIDDEN_PATH_PATTERN.test(rawPath)) {
      throw new PathValidationError(`${label} contains forbidden characters.`);
    }
    const normalised = normalize(rawPath.trim());
    if (requireAbsolute && !isAbsolute(normalised)) {
      throw new PathValidationError(`${label} must be an absolute path.`);
    }
    return normalised;
  }

  private assertSafeCommand(command: string, label: string): void {
    if (typeof command !== "string" || command.trim().length === 0) {
      throw new PathValidationError(`${label} must be a non-empty string.`);
    }
    if (FORBIDDEN_PATH_PATTERN.test(command)) {
      throw new PathValidationError(`${label} contains forbidden characters.`);
    }
  }

  /**
   * Runs sovraan_core.py headless against the vault with explicit production paths.
   */
  async runBackend(vaultPath: string): Promise<ExecutionResult> {
    let scriptPath: string;
    let source: string;
    let destination: string;
    let database: string;
    try {
      this.assertSafeCommand(this.pythonPath, "Python interpreter path");
      scriptPath = ExecutionEngine.sanitisePath(this.backendScriptPath, "Backend script path");
      source = ExecutionEngine.sanitisePath(vaultPath, "Vault path");
      destination = source;
      database = ExecutionEngine.sanitisePath(this.databasePath, "Database Path");
    } catch (error) {
      showUserError(error);
      throw new Error(translateExecutionError(error));
    }
    return this.spawnProcess(scriptPath, source, destination, database);
  }

  private spawnProcess(
    scriptPath: string,
    sourcePath: string,
    destinationPath: string,
    databasePath: string
  ): Promise<ExecutionResult> {
    return new Promise((resolve, reject) => {
      const args = [
        scriptPath,
        "--headless",
        "--source",
        sourcePath,
        "--destination",
        destinationPath,
        "--db",
        databasePath,
        "--notarise",
      ];
      const child = spawn(this.pythonPath, args, { shell: false, windowsHide: true });
      let stdout = "";
      let stderr = "";
      let settled = false;

      const settleFailure = (error: unknown) => {
        if (settled) return;
        settled = true;
        failSpawn(reject, error);
      };

      child.stdout?.on("data", (chunk: Buffer) => {
        stdout += chunk.toString();
      });
      child.stderr?.on("data", (chunk: Buffer) => {
        stderr += chunk.toString();
      });
      child.on("error", (error) => settleFailure(error));
      child.on("close", (code) => {
        if (settled) return;
        settled = true;
        if (code === 0) {
          new Notice("sovraan: backend completed successfully.", 5000);
          resolve({ success: true, exitCode: 0, stdout, stderr });
        } else {
          settleFailure(new Error(`Backend failed (code ${code}): ${stderr.trim()}`));
        }
      });
    });
  }
}
