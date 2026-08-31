import type { Context, Hono } from "hono";
import type { JobsEnv } from "./env";

const REVIEW_PATH = "/internal/memory-archive/reviews";
const MAX_BODY_BYTES = 4 * 1024 * 1024;
const MAX_ENTRIES = 50;
const REVIEW_TTL_SECONDS = 60 * 60;
const SHA256 = /^[0-9a-f]{64}$/;
const UID = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;
const ID = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;
const RESTRICTED_LABELS = new Set([
  "credential",
  "secret",
  "financial",
  "health",
  "intimate",
  "minor",
  "minors",
  "workplace_confidential",
  "identity_authentication",
]);
const SENSITIVE_KEYS = new Set([
  "access_token",
  "api_key",
  "authorization",
  "bearer",
  "client_secret",
  "credential",
  "credentials",
  "custom_token",
  "email",
  "firebase_id_token",
  "firebase_uid",
  "id_token",
  "openai_api_key",
  "password",
  "private_key",
  "refresh_token",
  "secret",
  "secret_key",
  "token",
]);

type Ctx = Context<{ Bindings: JobsEnv }>;

type ArchiveRow = {
  uid: string;
  memory_id: string;
  memory_tier: "archive";
  content: string;
  version: number;
  status: "active";
  processing_state: "processed";
  source_state: "active";
  sensitivity_labels: string[];
  visibility: "private" | "public" | "shared";
  user_asserted: 0 | 1;
  captured_at: number;
  updated_at: number;
  expires_at: number | null;
  ledger_commit_id: string | null;
  ledger_sequence: number | null;
  item_revision: number;
  source_id: string;
  evidence: unknown[];
  confidence: number | null;
  superseded_by: string | null;
  is_locked: 0;
  account_generation: number;
  created_at: number;
  deleted_at: null;
};

type Entry = {
  uid: string;
  memory_id: string;
  source_fingerprint: string;
  source_row_sha256: string;
  import_id: string;
  plan_hash: string;
  account_generation: number;
  row: Record<string, unknown>;
  normalized: ArchiveRow;
};

type Plan = {
  manifest_sha256: string;
  source: Record<string, unknown>;
  entries: Entry[];
};

function noStore(): Record<string, string> {
  return { "cache-control": "no-store" };
}

function fail(message: string): never {
  throw new Error("memory archive import: " + message);
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(stableJson).join(",") + "]";
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return "{" + Object.keys(object).sort().map((key) =>
      JSON.stringify(key) + ":" + stableJson(object[key])).join(",") + "}";
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) fail("value is not serializable");
  return encoded;
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function equal(left: string, right: string): boolean {
  const a = new TextEncoder().encode(left);
  const b = new TextEncoder().encode(right);
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) diff |= (a[i] || 0) ^ (b[i] || 0);
  return diff === 0;
}

function adminAuthorized(c: Ctx): boolean {
  const expected = c.env.ADMIN_KEY || "";
  const supplied = c.req.header("secret-key") || "";
  return Boolean(expected && supplied && equal(expected, supplied));
}

function assertHash(value: unknown, field: string): string {
  if (typeof value !== "string" || !SHA256.test(value)) fail(field + " is invalid");
  return value;
}

function assertId(value: unknown, field: string): string {
  if (typeof value !== "string" || !ID.test(value)) fail(field + " is invalid");
  return value;
}

function assertInteger(value: unknown, field: string, minimum = 0): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) fail(field + " is invalid");
  return value;
}

function assertText(value: unknown, field: string, maximum: number, minimum = 0): string {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum || value.includes("\0")) fail(field + " is invalid");
  if (new TextEncoder().encode(value).byteLength > maximum) fail(field + " is too large");
  return value;
}

function assertSafeJson(value: unknown, depth = 0, nodes = { count: 0 }): void {
  nodes.count += 1;
  if (nodes.count > 4096 || depth > 20) fail("row JSON is too large");
  if (!value || typeof value !== "object") return;
  if (Array.isArray(value)) {
    value.forEach((item) => assertSafeJson(item, depth + 1, nodes));
    return;
  }
  Object.values(value as Record<string, unknown>).forEach((item) => assertSafeJson(item, depth + 1, nodes));
}

function sensitiveField(value: unknown, path = ""): string | null {
  if (!value || typeof value !== "object") return null;
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i += 1) {
      const found = sensitiveField(value[i], path + "[" + i + "]");
      if (found) return found;
    }
    return null;
  }
  for (const [key, nested] of Object.entries(value as Record<string, unknown>)) {
    const current = path ? path + "." + key : key;
    if (SENSITIVE_KEYS.has(key.toLowerCase())) return current;
    const found = sensitiveField(nested, current);
    if (found) return found;
  }
  return null;
}

function keysOnly(value: Record<string, unknown>, allowed: string[], field: string): void {
  const accepted = new Set(allowed);
  if (Object.keys(value).some((key) => !accepted.has(key))) fail(field + " contains unsupported fields");
}

function parseBody(c: Ctx): Promise<Record<string, unknown> | null> {
  return c.req.raw.arrayBuffer().then((buffer) => {
    if (buffer.byteLength < 1 || buffer.byteLength > MAX_BODY_BYTES) return null;
    try {
      const parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer)) as unknown;
      return objectValue(parsed);
    } catch {
      return null;
    }
  });
}

function normalizedSource(value: unknown): Record<string, unknown> {
  const source = objectValue(value);
  if (!source) fail("source is invalid");
  keysOnly(source, ["kind", "collection", "export_sha256", "exported_at"], "source");
  if (source.kind !== "firestore" || source.collection !== "users/{uid}/memories") fail("source is not the archive export");
  const normalized: Record<string, unknown> = {
    kind: "firestore",
    collection: "users/{uid}/memories",
    export_sha256: assertHash(source.export_sha256, "source.export_sha256"),
  };
  if (source.exported_at !== undefined) normalized.exported_at = assertText(source.exported_at, "source.exported_at", 128, 1);
  return normalized;
}

function normalizeRow(value: unknown, uid: string, memoryId: string, generation: number): ArchiveRow {
  const row = objectValue(value);
  if (!row) fail("row is invalid");
  assertSafeJson(row);
  const sensitive = sensitiveField(row);
  if (sensitive) fail("row contains sensitive field " + sensitive);
  keysOnly(row, [
    "uid", "memory_id", "memory_tier", "content", "version", "status",
    "processing_state", "source_state", "sensitivity_labels", "visibility",
    "user_asserted", "captured_at", "updated_at", "expires_at",
    "ledger_commit_id", "ledger_sequence", "item_revision", "source_id",
    "evidence", "confidence", "superseded_by", "is_locked", "account_generation",
    "created_at", "deleted_at",
  ], "row");
  if (row.uid !== uid || row.memory_id !== memoryId) fail("row identity does not match entry");
  if (row.memory_tier !== "archive" || row.status !== "active" || row.processing_state !== "processed" || row.source_state !== "active") {
    fail("row is not an active processed archive item");
  }
  if (row.visibility !== "private" && row.visibility !== "public" && row.visibility !== "shared") fail("row.visibility is invalid");
  const labels = row.sensitivity_labels;
  if (!Array.isArray(labels) || labels.length > 100 || labels.some((item) => typeof item !== "string" || item.length > 128)) fail("row.sensitivity_labels is invalid");
  if (labels.some((item) => RESTRICTED_LABELS.has(item.trim().toLowerCase()))) fail("row has restricted sensitivity");
  const evidence = row.evidence;
  if (!Array.isArray(evidence) || new TextEncoder().encode(stableJson(evidence)).byteLength > 65536) fail("row.evidence is invalid");
  const confidence = row.confidence === null ? null : row.confidence;
  if (confidence !== null && (typeof confidence !== "number" || !Number.isFinite(confidence) || confidence < 0 || confidence > 1)) fail("row.confidence is invalid");
  const expiresAt = row.expires_at === null ? null : assertInteger(row.expires_at, "row.expires_at");
  const ledgerCommitId = row.ledger_commit_id === null ? null : assertText(row.ledger_commit_id, "row.ledger_commit_id", 256, 1);
  const ledgerSequence = row.ledger_sequence === null ? null : assertInteger(row.ledger_sequence, "row.ledger_sequence");
  const sourceId = assertText(row.source_id, "row.source_id", 256, 1);
  if (row.account_generation !== generation) fail("row.account_generation does not match entry");
  if (row.deleted_at !== null) fail("row.deleted_at must be null");
  if (row.is_locked !== 0 && row.is_locked !== false) fail("row.is_locked must be false");
  if (row.user_asserted !== 0 && row.user_asserted !== 1 && row.user_asserted !== false && row.user_asserted !== true) fail("row.user_asserted is invalid");
  const userAsserted = row.user_asserted === true ? 1 : row.user_asserted === false ? 0 : row.user_asserted as 0 | 1;
  return {
    uid,
    memory_id: memoryId,
    memory_tier: "archive",
    content: assertText(row.content, "row.content", 50000, 1),
    version: assertInteger(row.version, "row.version", 1),
    status: "active",
    processing_state: "processed",
    source_state: "active",
    sensitivity_labels: labels as string[],
    visibility: row.visibility,
    user_asserted: userAsserted,
    captured_at: assertInteger(row.captured_at, "row.captured_at"),
    updated_at: assertInteger(row.updated_at, "row.updated_at"),
    expires_at: expiresAt,
    ledger_commit_id: ledgerCommitId,
    ledger_sequence: ledgerSequence,
    item_revision: assertInteger(row.item_revision, "row.item_revision", 1),
    source_id: sourceId,
    evidence: evidence as unknown[],
    confidence,
    superseded_by: row.superseded_by === null ? null : assertId(row.superseded_by, "row.superseded_by"),
    is_locked: 0,
    account_generation: generation,
    created_at: assertInteger(row.created_at, "row.created_at"),
    deleted_at: null,
  };
}

async function normalizeEntry(value: unknown): Promise<Entry> {
  const raw = objectValue(value);
  if (!raw) fail("entry is invalid");
  keysOnly(raw, [
    "uid", "memory_id", "memoryId", "source_fingerprint", "sourceFingerprint",
    "source_row_sha256", "sourceRowSha256", "import_id", "importId",
    "plan_hash", "planHash", "account_generation", "accountGeneration",
    "row", "action", "status", "last_error", "lastError",
  ], "entry");
  if (raw.action !== "stage" || raw.status !== "planned" || (raw.last_error !== null && raw.last_error !== undefined) || (raw.lastError !== null && raw.lastError !== undefined)) fail("entry is not a planned stage");
  const field = (snake: string, camel?: string): unknown => raw[snake] ?? (camel ? raw[camel] : undefined);
  const uid = assertId(raw.uid, "entry.uid");
  const memoryId = assertId(field("memory_id", "memoryId"), "entry.memory_id");
  const sourceFingerprint = assertHash(field("source_fingerprint", "sourceFingerprint"), "entry.source_fingerprint");
  const generation = assertInteger(field("account_generation", "accountGeneration"), "entry.account_generation");
  const normalized = normalizeRow(raw.row, uid, memoryId, generation);
  const sourceRowSha256 = await sha256(stableJson({
    uid,
    memory_id: memoryId,
    source_fingerprint: sourceFingerprint,
    account_generation: generation,
    row: normalized,
  }));
  const importId = await sha256(uid + "\0archive\0" + memoryId + "\0" + sourceFingerprint + "\0" + sourceRowSha256);
  const planHash = await sha256(stableJson({
    uid,
    memory_id: memoryId,
    account_generation: generation,
    source_fingerprint: sourceFingerprint,
    source_row_sha256: sourceRowSha256,
    import_id: importId,
    action: "stage",
    last_error: null,
  }));
  if (assertHash(field("source_row_sha256", "sourceRowSha256"), "entry.source_row_sha256") !== sourceRowSha256) fail("entry.source_row_sha256 does not match row");
  if (assertHash(field("import_id", "importId"), "entry.import_id") !== importId) fail("entry.import_id does not match row");
  if (assertHash(field("plan_hash", "planHash"), "entry.plan_hash") !== planHash) fail("entry.plan_hash does not match row");
  return {
    uid,
    memory_id: memoryId,
    source_fingerprint: sourceFingerprint,
    source_row_sha256: sourceRowSha256,
    import_id: importId,
    plan_hash: planHash,
    account_generation: generation,
    row: normalized as unknown as Record<string, unknown>,
    normalized,
  };
}

async function normalizePlan(body: Record<string, unknown>): Promise<Plan> {
  keysOnly(body, ["manifest_sha256", "source", "entries"], "plan");
  if (!Array.isArray(body.entries) || body.entries.length < 1 || body.entries.length > MAX_ENTRIES) fail("entries are invalid");
  const source = normalizedSource(body.source);
  const manifest = assertHash(body.manifest_sha256, "manifest_sha256");
  const entries = await Promise.all(body.entries.map((entry) => normalizeEntry(entry)));
  const uid = entries[0].uid;
  const generation = entries[0].account_generation;
  if (entries.some((entry) => entry.uid !== uid || entry.account_generation !== generation)) fail("plan spans accounts or generations");
  const identifiers = new Set<string>();
  for (const entry of entries) {
    const key = entry.uid + "\0" + entry.memory_id;
    if (identifiers.has(key)) fail("plan contains duplicate memory ids");
    identifiers.add(key);
  }
  const ordered = [...entries].sort((a, b) => (a.uid + "\0" + a.memory_id).localeCompare(b.uid + "\0" + b.memory_id));
  const expectedManifest = await sha256(stableJson({
    schema_version: 1,
    source,
    entries: ordered.map((entry) => entry.source_row_sha256),
  }));
  if (manifest !== expectedManifest) fail("manifest does not match entries");
  return { manifest_sha256: manifest, source, entries: ordered };
}

function decodeSignature(value: string): Uint8Array | null {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) return null;
  try {
    const padded = value + "=".repeat((4 - value.length % 4) % 4);
    const binary = atob(padded.replaceAll("-", "+").replaceAll("_", "/"));
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

function arrayBuffer(bytes: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return copy.buffer;
}

async function verifySignature(c: Ctx, payload: string): Promise<boolean> {
  const secret = c.env.MEMORY_ARCHIVE_IMPORT_SIGNING_SECRET || "";
  const encoded = c.req.header("x-memory-archive-plan-signature") || "";
  const signature = decodeSignature(encoded);
  if (secret.length < 32 || !signature) return false;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  return crypto.subtle.verify(
    "HMAC",
    key,
    arrayBuffer(signature),
    new TextEncoder().encode(payload),
  );
}

function signaturePayload(plan: Plan): string {
  return stableJson({
    manifest_sha256: plan.manifest_sha256,
    entries: plan.entries.map((entry) => ({
      uid: entry.uid,
      memory_id: entry.memory_id,
      source_fingerprint: entry.source_fingerprint,
      source_row_sha256: entry.source_row_sha256,
      import_id: entry.import_id,
      plan_hash: entry.plan_hash,
      account_generation: entry.account_generation,
    })),
  });
}

async function authority(env: JobsEnv, uid: string, generation: number): Promise<boolean> {
  const global = await env.APP_DB.prepare(
    "SELECT source, memory_reads_enabled, kill_switch_active FROM cf_memory_global_read_gate WHERE id = 1",
  ).first<Record<string, unknown>>();
  if (!global || global.source !== "cloudflare_operator" || Number(global.memory_reads_enabled) !== 1 || Number(global.kill_switch_active) !== 0) return false;
  const control = await env.APP_DB.prepare(
    "SELECT source, memory_reads_enabled, default_memory_grant, archive_capability, account_generation FROM cf_memory_control WHERE uid = ?",
  ).bind(uid).first<Record<string, unknown>>();
  if (!control || control.source !== "cloudflare_cutover_projection" || Number(control.memory_reads_enabled) !== 1 || Number(control.default_memory_grant) !== 1 || Number(control.archive_capability) !== 1 || Number(control.account_generation) !== generation) return false;
  const cutover = await env.APP_DB.prepare(
    "SELECT state, checkpoint_phase, destination_backend_bound, account_generation FROM cf_account_cutover WHERE uid = ?",
  ).bind(uid).first<Record<string, unknown>>();
  if (!cutover || cutover.state !== "new" || cutover.checkpoint_phase !== "completed" || Number(cutover.destination_backend_bound) !== 1 || Number(cutover.account_generation) !== generation) return false;
  const fence = await env.APP_DB.prepare(
    "SELECT uid FROM cf_account_deletion_intents WHERE uid = ? UNION SELECT uid FROM cf_account_deletion_tombstones WHERE uid = ?",
  ).bind(uid, uid).first<Record<string, unknown>>();
  return !fence;
}

async function review(c: Ctx): Promise<Response> {
  const body = await parseBody(c);
  if (!body) return c.json({ error: "invalid_request" }, 422, noStore());
  let plan: Plan;
  try {
    plan = await normalizePlan(body);
  } catch {
    return c.json({ error: "invalid_plan" }, 422, noStore());
  }
  if (!(await verifySignature(c, signaturePayload(plan)))) return c.json({ error: "plan_signature_invalid" }, 403, noStore());
  if (!(await authority(c.env, plan.entries[0].uid, plan.entries[0].account_generation))) {
    return c.json({ error: "memory_archive_authority_changed" }, 409, noStore());
  }
  const now = Math.floor(Date.now() / 1000);
  const reviewId = crypto.randomUUID();
  const statements = [
    c.env.APP_DB.prepare(
      "INSERT INTO cf_memory_archive_review_batches (review_id, uid, manifest_sha256, entry_count, status, reviewed_at, expires_at, updated_at) VALUES (?, ?, ?, ?, 'approved', ?, ?, ?) ON CONFLICT(uid, manifest_sha256) DO NOTHING",
    ).bind(reviewId, plan.entries[0].uid, plan.manifest_sha256, plan.entries.length, now, now + REVIEW_TTL_SECONDS, now),
  ];
  for (const entry of plan.entries) {
    statements.push(c.env.APP_DB.prepare(
      "INSERT INTO cf_memory_archive_review_items (review_id, uid, memory_id, import_id, source_fingerprint, source_row_sha256, plan_hash, account_generation, row_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(review_id, uid, memory_id) DO NOTHING",
    ).bind(
      reviewId,
      entry.uid,
      entry.memory_id,
      entry.import_id,
      entry.source_fingerprint,
      entry.source_row_sha256,
      entry.plan_hash,
      entry.account_generation,
      stableJson(entry.normalized),
      now,
      now,
    ));
  }
  try {
    await c.env.APP_DB.batch(statements);
    const existing = await c.env.APP_DB.prepare(
      "SELECT review_id, entry_count, status, expires_at FROM cf_memory_archive_review_batches WHERE uid = ? AND manifest_sha256 = ?",
    ).bind(plan.entries[0].uid, plan.manifest_sha256).first<Record<string, unknown>>();
    if (!existing) return c.json({ error: "memory_archive_review_unavailable" }, 503, noStore());
    return c.json({
      review_id: String(existing.review_id),
      manifest_sha256: plan.manifest_sha256,
      entry_count: Number(existing.entry_count),
      status: existing.status,
      expires_at: Number(existing.expires_at),
    }, String(existing.review_id) === reviewId ? 201 : 200, noStore());
  } catch (error) {
    if (error instanceof Error && error.message.includes("account deletion fence")) {
      return c.json({ error: "memory_archive_authority_changed" }, 409, noStore());
    }
    return c.json({ error: "memory_archive_review_unavailable" }, 503, noStore());
  }
}

function archiveRowFromDatabase(row: Record<string, unknown>): ArchiveRow | null {
  try {
    const labels = JSON.parse(String(row.sensitivity_labels_json));
    const evidence = JSON.parse(String(row.evidence_json));
    return {
      uid: String(row.uid),
      memory_id: String(row.memory_id),
      memory_tier: "archive",
      content: String(row.content),
      version: Number(row.version),
      status: row.status as "active",
      processing_state: row.processing_state as "processed",
      source_state: row.source_state as "active",
      sensitivity_labels: labels as string[],
      visibility: row.visibility as ArchiveRow["visibility"],
      user_asserted: Number(row.user_asserted) as 0 | 1,
      captured_at: Number(row.captured_at),
      updated_at: Number(row.updated_at),
      expires_at: row.expires_at === null ? null : Number(row.expires_at),
      ledger_commit_id: row.ledger_commit_id === null ? null : String(row.ledger_commit_id),
      ledger_sequence: row.ledger_sequence === null ? null : Number(row.ledger_sequence),
      item_revision: Number(row.item_revision),
      source_id: String(row.source_id),
      evidence: evidence as unknown[],
      confidence: row.confidence === null ? null : Number(row.confidence),
      superseded_by: row.superseded_by === null ? null : String(row.superseded_by),
      is_locked: Number(row.is_locked) as 0,
      account_generation: Number(row.account_generation),
      created_at: Number(row.created_at),
      deleted_at: row.deleted_at === null ? null : Number(row.deleted_at) as never,
    };
  } catch {
    return null;
  }
}

function sameArchiveRow(left: ArchiveRow, right: ArchiveRow): boolean {
  return stableJson(left) === stableJson(right);
}

async function apply(c: Ctx): Promise<Response> {
  const reviewId = c.req.param("reviewId");
  if (!reviewId || reviewId.length !== 36) return c.json({ error: "invalid_review_id" }, 422, noStore());
  const batch = await c.env.APP_DB.prepare(
    "SELECT review_id, uid, manifest_sha256, entry_count, status, expires_at FROM cf_memory_archive_review_batches WHERE review_id = ?",
  ).bind(reviewId).first<Record<string, unknown>>();
  if (!batch) return c.json({ error: "review_not_found" }, 404, noStore());
  const now = Math.floor(Date.now() / 1000);
  if (batch.status === "revoked" || Number(batch.expires_at) <= now) return c.json({ error: "review_expired" }, 409, noStore());
  const itemRows = await c.env.APP_DB.prepare(
    "SELECT uid, memory_id, import_id, source_fingerprint, source_row_sha256, plan_hash, account_generation, row_json FROM cf_memory_archive_review_items WHERE review_id = ? ORDER BY memory_id",
  ).bind(reviewId).all<Record<string, unknown>>();
  if (!itemRows.results || itemRows.results.length !== Number(batch.entry_count)) return c.json({ error: "review_incomplete" }, 409, noStore());
  let entries: Entry[];
  try {
    entries = await Promise.all(itemRows.results.map(async (row) => normalizeEntry({
      uid: row.uid,
      memory_id: row.memory_id,
      source_fingerprint: row.source_fingerprint,
      source_row_sha256: row.source_row_sha256,
      import_id: row.import_id,
      plan_hash: row.plan_hash,
      account_generation: Number(row.account_generation),
      row: JSON.parse(String(row.row_json)),
      action: "stage",
      status: "planned",
      last_error: null,
    })));
  } catch {
    return c.json({ error: "review_item_invalid" }, 409, noStore());
  }
  const plan: Plan = {
    manifest_sha256: String(batch.manifest_sha256),
    source: {},
    entries,
  };
  if (!(await verifySignature(c, stableJson({
    review_id: reviewId,
    manifest_sha256: plan.manifest_sha256,
    entries: entries.map((entry) => ({
      uid: entry.uid,
      memory_id: entry.memory_id,
      source_fingerprint: entry.source_fingerprint,
      source_row_sha256: entry.source_row_sha256,
      import_id: entry.import_id,
      plan_hash: entry.plan_hash,
      account_generation: entry.account_generation,
    })),
  })))) {
    return c.json({ error: "plan_signature_invalid" }, 403, noStore());
  }
  const uid = String(batch.uid);
  if (entries.some((entry) => entry.uid !== uid) || !(await authority(c.env, uid, entries[0].account_generation))) {
    return c.json({ error: "memory_archive_authority_changed" }, 409, noStore());
  }
  const keys = entries.map((entry) => entry.memory_id);
  const existingRows = await c.env.APP_DB.prepare(
    "SELECT uid, memory_id, memory_tier, content, version, status, processing_state, source_state, sensitivity_labels_json, visibility, user_asserted, captured_at, updated_at, expires_at, ledger_commit_id, ledger_sequence, item_revision, source_id, evidence_json, confidence, superseded_by, is_locked, account_generation, created_at, deleted_at FROM cf_memory_archive_items WHERE uid = ? AND memory_id IN (" + keys.map(() => "?").join(",") + ")",
  ).bind(uid, ...keys).all<Record<string, unknown>>();
  const existingById = new Map((existingRows.results || []).map((row) => [String(row.memory_id), row]));
  const applyRows = await c.env.APP_DB.prepare(
    "SELECT uid, memory_id, review_id, import_id, source_row_sha256, plan_hash, account_generation, status FROM cf_memory_archive_applies WHERE uid = ? AND memory_id IN (" + keys.map(() => "?").join(",") + ")",
  ).bind(uid, ...keys).all<Record<string, unknown>>();
  const appliedById = new Map((applyRows.results || []).map((row) => [String(row.memory_id), row]));
  const pending: Entry[] = [];
  let alreadyApplied = 0;
  for (const entry of entries) {
    const existingApply = appliedById.get(entry.memory_id);
    if (existingApply) {
      if (existingApply.review_id !== reviewId || existingApply.import_id !== entry.import_id || existingApply.source_row_sha256 !== entry.source_row_sha256 || existingApply.plan_hash !== entry.plan_hash || Number(existingApply.account_generation) !== entry.account_generation || existingApply.status !== "applied") {
        return c.json({ error: "memory_archive_apply_conflict" }, 409, noStore());
      }
      alreadyApplied += 1;
      continue;
    }
    const existing = existingById.get(entry.memory_id);
    if (existing) {
      const parsed = archiveRowFromDatabase(existing);
      if (!parsed || !sameArchiveRow(parsed, entry.normalized)) return c.json({ error: "memory_archive_target_conflict" }, 409, noStore());
    }
    pending.push(entry);
  }
  const statements = [];
  for (const entry of pending) {
    const row = entry.normalized;
    statements.push(c.env.APP_DB.prepare(
      "INSERT INTO cf_memory_archive_items (uid, memory_id, memory_tier, content, version, status, processing_state, source_state, sensitivity_labels_json, visibility, user_asserted, captured_at, updated_at, expires_at, ledger_commit_id, ledger_sequence, item_revision, source_id, evidence_json, confidence, superseded_by, is_locked, account_generation, created_at, deleted_at) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?) ON CONFLICT(uid, memory_id) DO NOTHING",
    ).bind(
      row.uid, row.memory_id, row.memory_tier, row.content, row.version, row.status, row.processing_state, row.source_state,
      JSON.stringify(row.sensitivity_labels), row.visibility, row.user_asserted, row.captured_at, row.updated_at, row.expires_at,
      row.ledger_commit_id, row.ledger_sequence, row.item_revision, row.source_id, JSON.stringify(row.evidence), row.confidence,
      row.superseded_by, row.is_locked, row.account_generation, row.created_at, row.deleted_at, row.uid, row.uid,
    ));
    statements.push(c.env.APP_DB.prepare(
      "INSERT INTO cf_memory_archive_applies (uid, memory_id, review_id, import_id, source_row_sha256, plan_hash, account_generation, status, applied_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?) ON CONFLICT(uid, memory_id) DO NOTHING",
    ).bind(entry.uid, entry.memory_id, reviewId, entry.import_id, entry.source_row_sha256, entry.plan_hash, entry.account_generation, now, now));
  }
  statements.push(c.env.APP_DB.prepare(
    "UPDATE cf_memory_archive_review_batches SET status = 'applied', updated_at = ? WHERE review_id = ? AND status = 'approved'",
  ).bind(now, reviewId));
  try {
    if (statements.length) await c.env.APP_DB.batch(statements);
    return c.json({
      review_id: reviewId,
      status: "applied",
      entry_count: entries.length,
      applied_count: pending.length,
      already_applied_count: alreadyApplied,
    }, 200, noStore());
  } catch (error) {
    if (error instanceof Error && (error.message.includes("account deletion fence") || error.message.includes("authority changed"))) {
      return c.json({ error: "memory_archive_authority_changed" }, 409, noStore());
    }
    return c.json({ error: "memory_archive_apply_unavailable" }, 503, noStore());
  }
}

export function registerMemoryArchiveImportRoutes(app: Hono<{ Bindings: JobsEnv }>): void {
  app.post(REVIEW_PATH, async (c) => {
    if (c.env.MEMORY_ARCHIVE_IMPORT_STAGING_ENABLED !== "true") return c.json({ error: "memory_archive_import_unavailable" }, 503, noStore());
    if (!adminAuthorized(c)) return c.json({ error: "forbidden" }, 403, noStore());
    return review(c);
  });
  app.post(REVIEW_PATH + "/:reviewId/apply", async (c) => {
    if (c.env.MEMORY_ARCHIVE_IMPORT_STAGING_ENABLED !== "true") return c.json({ error: "memory_archive_import_unavailable" }, 503, noStore());
    if (!adminAuthorized(c)) return c.json({ error: "forbidden" }, 403, noStore());
    return apply(c);
  });
}

export const memoryArchiveImportConstants = Object.freeze({
  reviewPath: REVIEW_PATH,
  maxEntries: MAX_ENTRIES,
  maxBodyBytes: MAX_BODY_BYTES,
});
