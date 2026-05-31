import { existsSync, promises as fs } from "fs";
import { join } from "path";
import initSqlJs, { type Database, type SqlJsStatic } from "sql.js";
import {
  verifyAllBatchRows,
  type BatchSignatureRow,
  type SignatureScanResult,
} from "./signature_verifier";
import { resolveDatabasePath } from "./settings";

const WASM_FILENAME = "sql-wasm.wasm";

export interface BlockchainProof {
  ots_proof_blob: Uint8Array | null;
  status: string | null;
  updated_at: string | null;
}

export interface FileIndexRow {
  sha256_hash: string | null;
  file_name: string | null;
  file_size: number | null;
  modified_time: number | null;
  full_path: string | null;
  is_golden: number | null;
  last_session_id: string | null;
}

export class DedupDBReader {
  private static sqlJsPromise: Promise<SqlJsStatic> | null = null;

  private readonly resolvedDbPath: string;

  /**
   * @param dbPath Configured database path (validated to absolute).
   * @param wasmDirectory Plugin directory containing sql-wasm.wasm (not the DB).
   */
  constructor(dbPath: string, private readonly wasmDirectory: string) {
    this.resolvedDbPath = resolveDatabasePath(dbPath);
  }

  get databasePath(): string {
    return this.resolvedDbPath;
  }

  private static async getSqlJs(wasmDirectory: string): Promise<SqlJsStatic> {
    if (!DedupDBReader.sqlJsPromise) {
      const wasmPath = join(wasmDirectory, WASM_FILENAME);
      const source = await fs.readFile(wasmPath);
      const wasmBinary = source.buffer.slice(
        source.byteOffset,
        source.byteOffset + source.byteLength
      );
      DedupDBReader.sqlJsPromise = initSqlJs({ wasmBinary });
    }
    return DedupDBReader.sqlJsPromise;
  }

  private async withDatabase<T>(fn: (db: Database) => T): Promise<T> {
    if (!existsSync(this.resolvedDbPath)) {
      throw new Error(`Database file was not found at: ${this.resolvedDbPath}`);
    }
    const databaseBuffer = await fs.readFile(this.resolvedDbPath);
    const sqlJs = await DedupDBReader.getSqlJs(this.wasmDirectory);
    const db = new sqlJs.Database(new Uint8Array(databaseBuffer));
    try {
      return fn(db);
    } finally {
      db.close();
    }
  }

  private static blobToUint8Array(value: unknown): Uint8Array {
    if (value instanceof Uint8Array) {
      return value;
    }
    if (value instanceof ArrayBuffer) {
      return new Uint8Array(value);
    }
    if (Array.isArray(value)) {
      return Uint8Array.from(value as number[]);
    }
    return new Uint8Array(0);
  }

  async getBlockchainProof(fileHashStr: string): Promise<BlockchainProof | null> {
    return this.withDatabase((db) => {
      const statement = db.prepare(
        "SELECT ots_proof_blob, status, updated_at FROM blockchain_proofs WHERE file_hash = ?"
      );
      try {
        statement.bind([fileHashStr]);
        if (!statement.step()) {
          return null;
        }
        const row = statement.getAsObject();
        return {
          ots_proof_blob: DedupDBReader.blobToUint8Array(row.ots_proof_blob),
          status: (row.status as string | null) ?? null,
          updated_at: (row.updated_at as string | null) ?? null,
        };
      } finally {
        statement.free();
      }
    });
  }

  async getFileIndexByHash(fileHashStr: string): Promise<FileIndexRow | null> {
    return this.withDatabase((db) => {
      const statement = db.prepare(
        `SELECT sha256_hash, file_name, file_size, modified_time, full_path,
                is_golden, last_session_id
         FROM file_index
         WHERE sha256_hash = ?
         ORDER BY is_golden DESC, modified_time ASC
         LIMIT 1`
      );
      try {
        statement.bind([fileHashStr]);
        if (!statement.step()) {
          return null;
        }
        const row = statement.getAsObject();
        return {
          sha256_hash: (row.sha256_hash as string | null) ?? null,
          file_name: (row.file_name as string | null) ?? null,
          file_size: (row.file_size as number | null) ?? null,
          modified_time: (row.modified_time as number | null) ?? null,
          full_path: (row.full_path as string | null) ?? null,
          is_golden: (row.is_golden as number | null) ?? null,
          last_session_id: (row.last_session_id as string | null) ?? null,
        };
      } finally {
        statement.free();
      }
    });
  }

  async fetchBatchSignatures(): Promise<BatchSignatureRow[]> {
    return this.withDatabase((db) => {
      let hasTable = false;
      const check = db.prepare(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='batch_signatures'"
      );
      try {
        hasTable = check.step();
      } finally {
        check.free();
      }
      if (!hasTable) {
        return [];
      }

      const statement = db.prepare(
        `SELECT batch_index, manifest_json, signature, public_key, row_count, created_at
         FROM batch_signatures
         ORDER BY batch_index ASC`
      );
      const rows: BatchSignatureRow[] = [];
      try {
        while (statement.step()) {
          const row = statement.getAsObject();
          rows.push({
            batchIndex: Number(row.batch_index),
            manifestJson: String(row.manifest_json),
            signature: DedupDBReader.blobToUint8Array(row.signature),
            publicKey: DedupDBReader.blobToUint8Array(row.public_key),
            rowCount: Number(row.row_count),
            createdAt: Number(row.created_at),
          });
        }
      } finally {
        statement.free();
      }
      return rows;
    });
  }

  /** Verify all batch_signatures rows (Ed25519) at the configured database path. */
  async verifyBatchSignatures(): Promise<SignatureScanResult> {
    const rows = await this.fetchBatchSignatures();
    return verifyAllBatchRows(rows);
  }

  async countFileIndexRows(): Promise<number> {
    return this.withDatabase((db) => {
      const statement = db.prepare("SELECT COUNT(*) AS n FROM file_index");
      try {
        statement.step();
        const row = statement.getAsObject();
        return Number(row.n ?? 0);
      } finally {
        statement.free();
      }
    });
  }
}
