import nacl from "tweetnacl";

/** Deep-sort object keys to match Python json.dumps(..., sort_keys=True). */
function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortKeysDeep);
  }
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(record).sort()) {
      sorted[key] = sortKeysDeep(record[key]);
    }
    return sorted;
  }
  return value;
}

/** Canonical UTF-8 bytes aligned with crypto_gate.canonical_manifest_bytes. */
export function canonicalManifestBytes(manifest: unknown): Uint8Array {
  const payload = JSON.stringify(sortKeysDeep(manifest));
  return new TextEncoder().encode(payload);
}

export interface BatchSignatureRow {
  batchIndex: number;
  manifestJson: string;
  signature: Uint8Array;
  publicKey: Uint8Array;
  rowCount: number;
  createdAt: number;
}

/**
 * Verify one batch_signatures row using the stored Ed25519 public key.
 * Matches integrity_check.IntegrityCheck.verify_batch_row behaviour.
 */
export function verifyBatchSignatureRow(row: BatchSignatureRow): boolean {
  if (row.publicKey.length !== nacl.sign.publicKeyLength) {
    return false;
  }
  if (row.signature.length !== nacl.sign.signatureLength) {
    return false;
  }
  let manifest: unknown;
  try {
    manifest = JSON.parse(row.manifestJson) as unknown;
  } catch {
    return false;
  }
  const payload = canonicalManifestBytes(manifest);
  return nacl.sign.detached.verify(payload, row.signature, row.publicKey);
}

export interface SignatureScanResult {
  valid: number;
  total: number;
  failedBatchIndices: number[];
}

export function verifyAllBatchRows(rows: BatchSignatureRow[]): SignatureScanResult {
  const failedBatchIndices: number[] = [];
  let valid = 0;
  for (const row of rows) {
    if (verifyBatchSignatureRow(row)) {
      valid += 1;
    } else {
      failedBatchIndices.push(row.batchIndex);
    }
  }
  return { valid, total: rows.length, failedBatchIndices };
}
