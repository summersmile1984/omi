#!/usr/bin/env node
// LIFECYCLE: permanent

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

// This planner consumes an operator-exported, de-identified Firestore memory
// snapshot. It never connects to Firestore/GCS, calls a provider, or writes
// D1. Its staged entries are shaped for the reviewed Jobs endpoint in
// workers/jobs/memory-archive-import.ts.
const SCHEMA_VERSION = 1;
const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const MAX_ENTRIES = 50;
const SHA256 = /^[0-9a-f]{64}$/;
const UID = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;
const ID = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;
const RESTRICTED_LABELS = new Set([
  "credential", "secret", "financial", "health", "intimate", "minor",
  "minors", "workplace_confidential", "identity_authentication",
]);
const SENSITIVE_KEYS = new Set([
  "access_token", "api_key", "authorization", "bearer", "client_secret",
  "credential", "credentials", "custom_token", "email", "firebase_id_token",
  "firebase_uid", "id_token", "openai_api_key", "password", "private_key",
  "refresh_token", "secret", "secret_key", "token",
]);
const ROW_KEYS = new Set([
  "uid", "memory_id", "memory_tier", "content", "version", "status",
  "processing_state", "source_state", "sensitivity_labels", "visibility",
  "user_asserted", "captured_at", "updated_at", "expires_at",
  "ledger_commit_id", "ledger_sequence", "item_revision", "source_id",
  "evidence", "confidence", "superseded_by", "is_locked", "account_generation",
  "created_at", "deleted_at",
]);

function fail(message) {
  throw new Error(`memory archive reconciliation: ${message}`);
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) fail("value is not serializable");
  return encoded;
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function byteLength(value) {
  return Buffer.byteLength(value, "utf8");
}

function requiredHash(value, field) {
  if (typeof value !== "string" || !SHA256.test(value)) fail(`${field} is invalid`);
  return value;
}

function requiredId(value, field, pattern = ID) {
  if (typeof value !== "string" || !pattern.test(value)) fail(`${field} is invalid`);
  return value;
}

function integerValue(value, field, minimum = 0) {
  if (!Number.isSafeInteger(value) || value < minimum) fail(`${field} is invalid`);
  return value;
}

function boundedText(value, field, maximum, minimum = 0) {
  if (typeof value !== "string" || value.length < minimum || byteLength(value) > maximum || value.includes("\0")) {
    fail(`${field} is invalid`);
  }
  return value;
}

function sensitiveField(value, path = "") {
  if (!value || typeof value !== "object") return null;
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      const found = sensitiveField(value[index], `${path}[${index}]`);
      if (found) return found;
    }
    return null;
  }
  for (const [key, nested] of Object.entries(value)) {
    const current = path ? `${path}.${key}` : key;
    if (SENSITIVE_KEYS.has(key.toLowerCase())) return current;
    const found = sensitiveField(nested, current);
    if (found) return found;
  }
  return null;
}

function safeJson(value, depth = 0, nodes = { count: 0 }) {
  nodes.count += 1;
  if (nodes.count > 4096 || depth > 20) fail("row JSON is too large");
  if (!value || typeof value !== "object") return;
  for (const nested of Array.isArray(value) ? value : Object.values(value)) safeJson(nested, depth + 1, nodes);
}

function sourceValue(value) {
  const source = objectValue(value);
  if (!source) fail("source is invalid");
  if (sensitiveField(source)) fail("source contains sensitive data");
  const allowed = new Set(["kind", "collection", "export_sha256", "exported_at"]);
  if (Object.keys(source).some((key) => !allowed.has(key))) fail("source contains unsupported fields");
  if (source.kind !== "firestore" || source.collection !== "users/{uid}/memories") fail("source is not a memory export");
  const normalized = {
    kind: "firestore",
    collection: "users/{uid}/memories",
    export_sha256: requiredHash(source.export_sha256, "source.export_sha256"),
  };
  if (source.exported_at !== undefined) normalized.exported_at = boundedText(source.exported_at, "source.exported_at", 128, 1);
  return normalized;
}

function accountValue(value, index) {
  const account = objectValue(value);
  if (!account) fail(`accounts[${index + 1}] is invalid`);
  if (sensitiveField(account)) fail(`accounts[${index + 1}] contains sensitive data`);
  const allowed = new Set(["uid", "account_generation", "source_fingerprint"]);
  if (Object.keys(account).some((key) => !allowed.has(key))) fail(`accounts[${index + 1}] contains unsupported fields`);
  return {
    uid: requiredId(account.uid, `accounts[${index + 1}].uid`, UID),
    account_generation: integerValue(account.account_generation, `accounts[${index + 1}].account_generation`),
    source_fingerprint: requiredHash(account.source_fingerprint, `accounts[${index + 1}].source_fingerprint`),
  };
}

function normalizeRow(value, account) {
  const row = objectValue(value);
  if (!row) fail("memory row is invalid");
  safeJson(row);
  if (sensitiveField(row)) fail(`memory row ${account.uid} contains sensitive data`);
  if (Object.keys(row).some((key) => !ROW_KEYS.has(key))) fail("memory row contains unsupported fields");
  const memoryId = requiredId(row.memory_id, "row.memory_id");
  if (row.uid !== account.uid || row.account_generation !== account.account_generation) fail("memory row identity/generation mismatch");
  if (row.memory_tier !== "archive" || row.status !== "active" || row.processing_state !== "processed" || row.source_state !== "active") {
    fail("memory row is not an active processed archive item");
  }
  if (!["private", "public", "shared"].includes(row.visibility)) fail("row.visibility is invalid");
  if (!Array.isArray(row.sensitivity_labels) || row.sensitivity_labels.length > 100 || row.sensitivity_labels.some((label) => typeof label !== "string" || byteLength(label) > 128)) {
    fail("row.sensitivity_labels is invalid");
  }
  if (row.sensitivity_labels.some((label) => RESTRICTED_LABELS.has(label.trim().toLowerCase()))) fail("row has restricted sensitivity");
  if (!Array.isArray(row.evidence) || byteLength(stableJson(row.evidence)) > 65536) fail("row.evidence is invalid");
  if (row.confidence !== null && (typeof row.confidence !== "number" || !Number.isFinite(row.confidence) || row.confidence < 0 || row.confidence > 1)) fail("row.confidence is invalid");
  if (row.expires_at !== null) integerValue(row.expires_at, "row.expires_at");
  if (row.ledger_commit_id !== null) boundedText(row.ledger_commit_id, "row.ledger_commit_id", 256, 1);
  if (row.ledger_sequence !== null) integerValue(row.ledger_sequence, "row.ledger_sequence");
  if (row.is_locked !== 0 && row.is_locked !== false) fail("row.is_locked must be false");
  if (![0, 1, false, true].includes(row.user_asserted)) fail("row.user_asserted is invalid");
  if (row.deleted_at !== null) fail("row.deleted_at must be null");
  return {
    uid: account.uid,
    memory_id: memoryId,
    memory_tier: "archive",
    content: boundedText(row.content, "row.content", 50000, 1),
    version: integerValue(row.version, "row.version", 1),
    status: "active",
    processing_state: "processed",
    source_state: "active",
    sensitivity_labels: [...row.sensitivity_labels],
    visibility: row.visibility,
    user_asserted: row.user_asserted === true || row.user_asserted === 1 ? 1 : 0,
    captured_at: integerValue(row.captured_at, "row.captured_at"),
    updated_at: integerValue(row.updated_at, "row.updated_at"),
    expires_at: row.expires_at,
    ledger_commit_id: row.ledger_commit_id,
    ledger_sequence: row.ledger_sequence,
    item_revision: integerValue(row.item_revision, "row.item_revision", 1),
    source_id: boundedText(row.source_id, "row.source_id", 256, 1),
    evidence: row.evidence,
    confidence: row.confidence,
    superseded_by: row.superseded_by === null ? null : requiredId(row.superseded_by, "row.superseded_by"),
    is_locked: 0,
    account_generation: account.account_generation,
    created_at: integerValue(row.created_at, "row.created_at"),
    deleted_at: null,
  };
}

export function planMemoryArchiveReconciliation(input, { fencedUids = [] } = {}) {
  const manifest = objectValue(input);
  if (!manifest || manifest.schema_version !== SCHEMA_VERSION) fail(`schema_version must be ${SCHEMA_VERSION}`);
  const source = sourceValue(manifest.source);
  if (!Array.isArray(manifest.accounts) || manifest.accounts.length === 0) fail("accounts must be a non-empty array");
  const accounts = manifest.accounts.map(accountValue);
  if (new Set(accounts.map((account) => account.uid)).size !== accounts.length) fail("accounts contain duplicate uid");
  if (!Array.isArray(manifest.memories) || manifest.memories.length === 0 || manifest.memories.length > MAX_ENTRIES) fail(`memories must contain 1-${MAX_ENTRIES} rows`);
  const accountByUid = new Map(accounts.map((account) => [account.uid, account]));
  const fenced = new Set(fencedUids);
  const entries = [];
  for (let index = 0; index < manifest.memories.length; index += 1) {
    const raw = objectValue(manifest.memories[index]);
    if (!raw) fail(`memories[${index + 1}] is invalid`);
    const account = accountByUid.get(raw.uid);
    if (!account) fail(`memories[${index + 1}] uid is not listed in accounts`);
    const row = normalizeRow(raw, account);
    const sourceFingerprint = account.source_fingerprint;
    const sourceRowSha256 = sha256(stableJson({
      uid: account.uid,
      memory_id: row.memory_id,
      source_fingerprint: sourceFingerprint,
      account_generation: account.account_generation,
      row,
    }));
    const importId = sha256(`${account.uid}\0archive\0${row.memory_id}\0${sourceFingerprint}\0${sourceRowSha256}`);
    const planHash = sha256(stableJson({
      uid: account.uid,
      memory_id: row.memory_id,
      account_generation: account.account_generation,
      source_fingerprint: sourceFingerprint,
      source_row_sha256: sourceRowSha256,
      import_id: importId,
      action: "stage",
      last_error: null,
    }));
    entries.push({
      uid: account.uid,
      memory_id: row.memory_id,
      source_fingerprint: sourceFingerprint,
      source_row_sha256: sourceRowSha256,
      import_id: importId,
      plan_hash: planHash,
      account_generation: account.account_generation,
      row,
      action: fenced.has(account.uid) ? "blocked" : "stage",
      status: fenced.has(account.uid) ? "blocked" : "planned",
      last_error: fenced.has(account.uid) ? "account_deletion_fence" : null,
    });
  }
  entries.sort((left, right) => `${left.uid}\0${left.memory_id}`.localeCompare(`${right.uid}\0${right.memory_id}`));
  const seen = new Set();
  for (const entry of entries) {
    const key = `${entry.uid}\0${entry.memory_id}`;
    if (seen.has(key)) fail("memories contain duplicate uid/memory_id");
    seen.add(key);
  }
  const staged = entries.filter((entry) => entry.action === "stage");
  const manifestSha256 = sha256(stableJson({
    schema_version: SCHEMA_VERSION,
    source,
    entries: staged.map((entry) => entry.source_row_sha256),
  }));
  return {
    mode: "dry-run",
    schema_version: SCHEMA_VERSION,
    source,
    manifest_sha256: manifestSha256,
    total: entries.length,
    staged: staged.length,
    blocked: entries.length - staged.length,
    entries,
    apply_request: {
      manifest_sha256: manifestSha256,
      source,
      entries: staged,
    },
  };
}

async function readJson(filename) {
  if (!filename || filename.startsWith("--")) fail("--input is required");
  const raw = await readFile(filename);
  if (raw.byteLength > MAX_INPUT_BYTES) fail(`input exceeds ${MAX_INPUT_BYTES} bytes`);
  try {
    return JSON.parse(raw.toString("utf8"));
  } catch {
    fail("input is not valid JSON");
  }
}

async function main() {
  const args = process.argv.slice(2);
  const inputIndex = args.indexOf("--input");
  const fencedUids = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === "--fenced-uid" && args[index + 1]) fencedUids.push(args[++index]);
  }
  const plan = planMemoryArchiveReconciliation(await readJson(inputIndex >= 0 ? args[inputIndex + 1] : null), { fencedUids });
  process.stdout.write(`${JSON.stringify(plan, null, 2)}\n`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 2;
  });
}
