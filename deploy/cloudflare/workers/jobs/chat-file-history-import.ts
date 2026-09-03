import type { Context, Hono } from "hono";
import type { JobsEnv } from "./env";

const REVIEW_PATH = "/internal/chat-file-history/reviews";
const MAX_BODY_BYTES = 4 * 1024 * 1024;
const MAX_ENTRIES = 50;
const REVIEW_TTL_SECONDS = 60 * 60;
const MAX_FILE_BYTES = 50 * 1024 * 1024;
const SHA256 = /^[0-9a-f]{64}$/;
const UID = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;
const FILE_ID = /^[^/\\\u0000-\u001f\u007f]{1,128}$/;
const PROVIDER_FILE_ID = /^file-[A-Za-z0-9_-]{1,256}$/;
const MIME_TYPE = /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/;

type Ctx = Context<{ Bindings: JobsEnv }>;

type FileItem = {
  uid: string;
  import_id: string;
  file_id: string;
  source_file_id: string;
  source_object_uri: string;
  source_generation: string | null;
  checksum_sha256: string;
  provider_file_id: string;
  name: string;
  mime_type: string;
  size: number;
  storage_key: string;
  request_fingerprint: string;
  plan_hash: string;
  account_generation: number;
  created_at: number;
  updated_at: number;
};

type ReviewPlan = {
  manifest_sha256: string;
  entries: FileItem[];
};

type ApplyBody = {
  review_id: string;
  manifest_sha256: string;
  attestations: Array<{ import_id: string; signature: string }>;
};

function noStore(): Record<string, string> {
  return { "cache-control": "no-store" };
}

function fail(message: string): never {
  throw new Error(`chat file history import: ${message}`);
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`).join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) fail("value is not serializable");
  return encoded;
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function constantTimeEqual(left: string, right: string): boolean {
  const a = new TextEncoder().encode(left);
  const b = new TextEncoder().encode(right);
  let difference = a.length ^ b.length;
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    difference |= (a[index] || 0) ^ (b[index] || 0);
  }
  return difference === 0;
}

function adminAuthorized(c: Ctx): boolean {
  const expected = c.env.ADMIN_KEY || "";
  const supplied = c.req.header("secret-key") || "";
  return Boolean(expected && supplied && constantTimeEqual(expected, supplied));
}

function assertHash(value: unknown, field: string): string {
  if (typeof value !== "string" || !SHA256.test(value)) fail(`${field} is invalid`);
  return value;
}

function assertId(value: unknown, field: string, pattern: RegExp = UID): string {
  if (typeof value !== "string" || !pattern.test(value)) fail(`${field} is invalid`);
  return value;
}

function assertText(value: unknown, field: string, maxBytes: number, minimum = 1): string {
  if (typeof value !== "string" || value.length < minimum || value.includes("\0")) fail(`${field} is invalid`);
  if (new TextEncoder().encode(value).byteLength > maxBytes) fail(`${field} is too large`);
  return value;
}

function assertInteger(value: unknown, field: string, minimum = 0): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) fail(`${field} is invalid`);
  return value;
}

function allowedKeys(value: Record<string, unknown>, allowed: string[], field: string): void {
  const accepted = new Set(allowed);
  if (Object.keys(value).some((key) => !accepted.has(key))) fail(`${field} contains unsupported fields`);
}

function field(value: Record<string, unknown>, snake: string, camel?: string): unknown {
  return value[snake] ?? (camel ? value[camel] : undefined);
}

function sourceUri(value: unknown): string {
  const uri = assertText(value, "source_object_uri", 1024);
  let parsed: URL;
  try {
    parsed = new URL(uri);
  } catch {
    fail("source_object_uri is invalid");
  }
  if (parsed.protocol !== "gs:" || !parsed.hostname || parsed.username || parsed.password || parsed.search || parsed.hash) {
    fail("source_object_uri is not a credential-free gs:// URI");
  }
  const parts = parsed.pathname.split("/").filter(Boolean);
  if (!parts.length || parts.some((part) => part === "." || part === ".." || /[\0\u0000-\u001f\u007f]/.test(part))) {
    fail("source_object_uri path is invalid");
  }
  return uri;
}

function planHashInput(item: Omit<FileItem, "file_id" | "account_generation" | "created_at" | "updated_at" | "request_fingerprint" | "import_id" | "plan_hash">): string {
  // This is the JSON ordering used by scripts/chat-file-reconcile.mjs.  It
  // lets the executor reject a hand-edited plan before it reaches D1.
  return JSON.stringify({
    uid: item.uid,
    sourceFileId: item.source_file_id,
    sourceObjectUri: item.source_object_uri,
    checksum: item.checksum_sha256,
    providerFileId: item.provider_file_id,
    name: item.name,
    mimeType: item.mime_type,
    size: item.size,
    storageKey: item.storage_key,
    action: "stage",
    errors: [],
  });
}

async function normalizeEntry(value: unknown): Promise<FileItem> {
  const raw = objectValue(value);
  if (!raw) fail("entry is invalid");
  allowedKeys(raw, [
    "uid", "importId", "import_id", "sourceFileId", "source_file_id",
    "sourceObjectUri", "source_object_uri", "sourceGeneration", "source_generation",
    "checksum", "checksum_sha256", "providerFileId", "provider_file_id", "name",
    "mimeType", "mime_type", "size", "storageKey", "storage_key", "requestFingerprint",
    "request_fingerprint", "createdAt", "created_at", "updatedAt", "updated_at",
    "action", "status", "lastError", "last_error", "planHash", "plan_hash",
    "accountGeneration", "account_generation",
  ], "entry");
  if (raw.action !== "stage" || raw.status !== "planned" || (raw.lastError ?? raw.last_error) != null) {
    fail("entry is not a planned stage");
  }
  const uid = assertId(raw.uid, "entry.uid");
  const sourceFileId = assertId(field(raw, "source_file_id", "sourceFileId"), "entry.source_file_id", FILE_ID);
  const sourceObjectUri = sourceUri(field(raw, "source_object_uri", "sourceObjectUri"));
  const checksum = assertHash(field(raw, "checksum_sha256", "checksum"), "entry.checksum_sha256");
  const providerFileId = assertId(field(raw, "provider_file_id", "providerFileId"), "entry.provider_file_id", PROVIDER_FILE_ID);
  const name = assertText(raw.name, "entry.name", 512);
  if (name.split(/[\\/]/).pop()?.trim() !== name) fail("entry.name must be a basename");
  const mimeType = assertText(field(raw, "mime_type", "mimeType"), "entry.mime_type", 200).toLowerCase();
  if (!MIME_TYPE.test(mimeType)) fail("entry.mime_type is invalid");
  const size = assertInteger(raw.size, "entry.size", 1);
  if (size > MAX_FILE_BYTES) fail("entry.size is too large");
  const storageKey = assertText(field(raw, "storage_key", "storageKey"), "entry.storage_key", 512);
  if (storageKey !== `${uid}/${sourceFileId}`) fail("entry.storage_key is not uid-scoped");
  const requestFingerprint = assertHash(field(raw, "request_fingerprint", "requestFingerprint"), "entry.request_fingerprint");
  const expectedRequestFingerprint = await sha256(`${uid}\0${name}\0${mimeType}\0${checksum}`);
  if (requestFingerprint !== expectedRequestFingerprint) fail("entry.request_fingerprint does not match row");
  const importId = assertHash(field(raw, "import_id", "importId"), "entry.import_id");
  const expectedImportId = await sha256(`${uid}\0${sourceFileId}\0${checksum}`);
  if (importId !== expectedImportId) fail("entry.import_id does not match row");
  const sourceGenerationValue = field(raw, "source_generation", "sourceGeneration");
  const sourceGeneration = sourceGenerationValue == null ? null : assertText(sourceGenerationValue, "entry.source_generation", 256);
  const accountGeneration = assertInteger(field(raw, "account_generation", "accountGeneration"), "entry.account_generation");
  const createdAt = assertInteger(field(raw, "created_at", "createdAt"), "entry.created_at", 1);
  const updatedAt = assertInteger(field(raw, "updated_at", "updatedAt"), "entry.updated_at", 1);
  const fileItem = {
    uid,
    import_id: importId,
    file_id: sourceFileId,
    source_file_id: sourceFileId,
    source_object_uri: sourceObjectUri,
    source_generation: sourceGeneration,
    checksum_sha256: checksum,
    provider_file_id: providerFileId,
    name,
    mime_type: mimeType,
    size,
    storage_key: storageKey,
    request_fingerprint: requestFingerprint,
    plan_hash: "",
    account_generation: accountGeneration,
    created_at: createdAt,
    updated_at: updatedAt,
  } satisfies FileItem;
  const expectedPlanHash = await sha256(planHashInput(fileItem));
  const planHash = assertHash(field(raw, "plan_hash", "planHash"), "entry.plan_hash");
  if (planHash !== expectedPlanHash) fail("entry.plan_hash does not match row");
  fileItem.plan_hash = planHash;
  return fileItem;
}

async function parseBody(c: Ctx): Promise<Record<string, unknown> | null> {
  const buffer = await c.req.raw.arrayBuffer();
  if (!buffer.byteLength || buffer.byteLength > MAX_BODY_BYTES) return null;
  try {
    const value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer)) as unknown;
    return objectValue(value);
  } catch {
    return null;
  }
}

async function normalizePlan(body: Record<string, unknown>): Promise<ReviewPlan> {
  allowedKeys(body, ["manifest_sha256", "manifestSha256", "entries"], "plan");
  const manifest = assertHash(field(body, "manifest_sha256", "manifestSha256"), "manifest_sha256");
  if (!Array.isArray(body.entries) || body.entries.length < 1 || body.entries.length > MAX_ENTRIES) fail("entries are invalid");
  const entries = await Promise.all(body.entries.map((entry) => normalizeEntry(entry)));
  const uid = entries[0].uid;
  const generation = entries[0].account_generation;
  if (entries.some((entry) => entry.uid !== uid || entry.account_generation !== generation)) fail("plan spans accounts or generations");
  const seen = new Set<string>();
  for (const entry of entries) {
    if (seen.has(entry.import_id)) fail("plan contains duplicate import ids");
    seen.add(entry.import_id);
  }
  const ordered = [...entries].sort((left, right) => `${left.uid}\0${left.import_id}`.localeCompare(`${right.uid}\0${right.import_id}`));
  const expectedManifest = await sha256(stableJson({
    schema_version: 1,
    entries: ordered.map((entry) => ({ uid: entry.uid, import_id: entry.import_id, plan_hash: entry.plan_hash, account_generation: entry.account_generation })),
  }));
  if (manifest !== expectedManifest) fail("manifest does not match entries");
  return { manifest_sha256: manifest, entries: ordered };
}

function decodeSignature(value: string): Uint8Array | null {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) return null;
  try {
    const binary = atob(value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - value.length % 4) % 4));
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

async function verifyHmac(secret: string, signatureValue: string, payload: string): Promise<boolean> {
  const signature = decodeSignature(signatureValue);
  if (secret.length < 32 || !signature) return false;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  return crypto.subtle.verify("HMAC", key, arrayBuffer(signature), new TextEncoder().encode(payload));
}

function reviewSignaturePayload(plan: ReviewPlan): string {
  return stableJson({
    manifest_sha256: plan.manifest_sha256,
    entries: plan.entries.map((entry) => ({
      uid: entry.uid,
      import_id: entry.import_id,
      file_id: entry.file_id,
      checksum_sha256: entry.checksum_sha256,
      provider_file_id: entry.provider_file_id,
      plan_hash: entry.plan_hash,
      account_generation: entry.account_generation,
    })),
  });
}

function providerAttestationPayload(item: Pick<FileItem, "uid" | "file_id" | "storage_key" | "checksum_sha256" | "size" | "provider_file_id" | "account_generation" | "plan_hash">): string {
  return [item.uid, item.file_id, item.storage_key, item.checksum_sha256, String(item.size), item.provider_file_id, String(item.account_generation), item.plan_hash].join("\0");
}

async function authority(env: JobsEnv, uid: string, generation: number): Promise<boolean> {
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
  let plan: ReviewPlan;
  try {
    plan = await normalizePlan(body);
  } catch {
    return c.json({ error: "invalid_plan" }, 422, noStore());
  }
  if (!(await verifyHmac(c.env.CHAT_FILE_HISTORY_IMPORT_SIGNING_SECRET || "", c.req.header("x-chat-file-plan-signature") || "", reviewSignaturePayload(plan)))) {
    return c.json({ error: "plan_signature_invalid" }, 403, noStore());
  }
  const first = plan.entries[0];
  if (!(await authority(c.env, first.uid, first.account_generation))) return c.json({ error: "chat_file_history_authority_changed" }, 409, noStore());
  const now = Math.floor(Date.now() / 1000);
  const reviewId = crypto.randomUUID();
  const statements = [c.env.APP_DB.prepare(
    "INSERT INTO cf_chat_file_history_review_batches (review_id, uid, manifest_sha256, account_generation, entry_count, status, reviewed_at, expires_at, updated_at) VALUES (?, ?, ?, ?, ?, 'approved', ?, ?, ?) ON CONFLICT(uid, manifest_sha256) DO NOTHING",
  ).bind(reviewId, first.uid, plan.manifest_sha256, first.account_generation, plan.entries.length, now, now + REVIEW_TTL_SECONDS, now)];
  for (const entry of plan.entries) {
    statements.push(c.env.APP_DB.prepare(
      "INSERT INTO cf_chat_file_import_ledger (uid, import_id, source_file_id, source_object_uri, source_generation, checksum_sha256, provider_file_id, name, mime_type, size, desired_storage_key, plan_hash, action, status, last_error, account_generation, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stage', 'planned', NULL, ?, ?, ?) ON CONFLICT(uid, import_id) DO UPDATE SET account_generation = excluded.account_generation, status = CASE WHEN cf_chat_file_import_ledger.plan_hash = excluded.plan_hash AND cf_chat_file_import_ledger.status <> 'applied' THEN 'planned' ELSE cf_chat_file_import_ledger.status END, last_error = CASE WHEN cf_chat_file_import_ledger.plan_hash = excluded.plan_hash AND cf_chat_file_import_ledger.status <> 'applied' THEN NULL ELSE cf_chat_file_import_ledger.last_error END, updated_at = excluded.updated_at WHERE cf_chat_file_import_ledger.plan_hash = excluded.plan_hash",
    ).bind(entry.uid, entry.import_id, entry.source_file_id, entry.source_object_uri, entry.source_generation, entry.checksum_sha256, entry.provider_file_id, entry.name, entry.mime_type, entry.size, entry.storage_key, entry.plan_hash, entry.account_generation, entry.created_at, now));
    statements.push(c.env.APP_DB.prepare(
      "INSERT INTO cf_chat_file_history_review_items (review_id, uid, import_id, file_id, source_file_id, source_object_uri, source_generation, checksum_sha256, provider_file_id, name, mime_type, size, storage_key, request_fingerprint, plan_hash, account_generation, created_at, updated_at) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM cf_chat_file_history_review_batches WHERE review_id = ? AND uid = ? AND manifest_sha256 = ?) ON CONFLICT(review_id, uid, import_id) DO NOTHING",
    ).bind(reviewId, entry.uid, entry.import_id, entry.file_id, entry.source_file_id, entry.source_object_uri, entry.source_generation, entry.checksum_sha256, entry.provider_file_id, entry.name, entry.mime_type, entry.size, entry.storage_key, entry.request_fingerprint, entry.plan_hash, entry.account_generation, entry.created_at, now, reviewId, first.uid, plan.manifest_sha256));
  }
  try {
    await c.env.APP_DB.batch(statements);
    const existing = await c.env.APP_DB.prepare(
      "SELECT review_id, entry_count, status, expires_at FROM cf_chat_file_history_review_batches WHERE uid = ? AND manifest_sha256 = ?",
    ).bind(first.uid, plan.manifest_sha256).first<Record<string, unknown>>();
    if (!existing) return c.json({ error: "chat_file_history_review_unavailable" }, 503, noStore());
    return c.json({ review_id: String(existing.review_id), manifest_sha256: plan.manifest_sha256, entry_count: Number(existing.entry_count), status: existing.status, expires_at: Number(existing.expires_at) }, String(existing.review_id) === reviewId ? 201 : 200, noStore());
  } catch (error) {
    if (error instanceof Error && error.message.includes("account deletion fence")) return c.json({ error: "chat_file_history_authority_changed" }, 409, noStore());
    if (error instanceof Error && error.message.includes("UNIQUE")) return c.json({ error: "chat_file_history_conflict" }, 409, noStore());
    return c.json({ error: "chat_file_history_review_unavailable" }, 503, noStore());
  }
}

function applySignaturePayload(body: ApplyBody): string {
  return stableJson(body);
}

async function apply(c: Ctx): Promise<Response> {
  const reviewId = c.req.param("reviewId");
  if (!reviewId || reviewId.length !== 36) return c.json({ error: "invalid_review_id" }, 422, noStore());
  const bodyValue = await parseBody(c);
  if (!bodyValue) return c.json({ error: "invalid_request" }, 422, noStore());
  let body: ApplyBody;
  try {
    allowedKeys(bodyValue, ["review_id", "manifest_sha256", "attestations"], "apply");
    const attestations = bodyValue.attestations;
    if (!Array.isArray(attestations) || attestations.length < 1 || attestations.length > MAX_ENTRIES) fail("attestations are invalid");
    body = {
      review_id: assertText(bodyValue.review_id, "review_id", 128),
      manifest_sha256: assertHash(bodyValue.manifest_sha256, "manifest_sha256"),
      attestations: attestations.map((value) => {
        const item = objectValue(value);
        if (!item) fail("attestation is invalid");
        allowedKeys(item, ["import_id", "signature"], "attestation");
        return { import_id: assertHash(item.import_id, "attestation.import_id"), signature: assertText(item.signature, "attestation.signature", 256) };
      }),
    };
    if (body.review_id !== reviewId) fail("review_id does not match path");
    if (new Set(body.attestations.map((item) => item.import_id)).size !== body.attestations.length) fail("duplicate attestations");
  } catch {
    return c.json({ error: "invalid_request" }, 422, noStore());
  }
  if (!(await verifyHmac(c.env.CHAT_FILE_HISTORY_IMPORT_SIGNING_SECRET || "", c.req.header("x-chat-file-plan-signature") || "", applySignaturePayload(body)))) {
    return c.json({ error: "plan_signature_invalid" }, 403, noStore());
  }
  const batch = await c.env.APP_DB.prepare(
    "SELECT review_id, uid, manifest_sha256, account_generation, entry_count, status, expires_at FROM cf_chat_file_history_review_batches WHERE review_id = ?",
  ).bind(reviewId).first<Record<string, unknown>>();
  if (!batch) return c.json({ error: "review_not_found" }, 404, noStore());
  const now = Math.floor(Date.now() / 1000);
  if (String(batch.manifest_sha256) !== body.manifest_sha256) return c.json({ error: "review_manifest_conflict" }, 409, noStore());
  if (batch.status === "revoked" || Number(batch.expires_at) <= now) return c.json({ error: "review_expired" }, 409, noStore());
  const rows = await c.env.APP_DB.prepare(
    "SELECT uid, import_id, file_id, source_file_id, source_object_uri, source_generation, checksum_sha256, provider_file_id, name, mime_type, size, storage_key, request_fingerprint, plan_hash, account_generation, created_at, updated_at FROM cf_chat_file_history_review_items WHERE review_id = ? ORDER BY import_id",
  ).bind(reviewId).all<Record<string, unknown>>();
  if (!rows.results || rows.results.length !== Number(batch.entry_count)) return c.json({ error: "review_incomplete" }, 409, noStore());
  const items = rows.results as unknown as FileItem[];
  const attestations = new Map(body.attestations.map((item) => [item.import_id, item.signature]));
  if (attestations.size !== items.length || items.some((item) => !attestations.has(item.import_id))) return c.json({ error: "provider_attestation_missing" }, 409, noStore());
  if (!(await authority(c.env, String(batch.uid), Number(batch.account_generation)))) return c.json({ error: "chat_file_history_authority_changed" }, 409, noStore());
  const providerSecret = c.env.CHAT_FILE_HISTORY_PROVIDER_ATTESTATION_SECRET || "";
  if (providerSecret.length < 32) return c.json({ error: "provider_attestation_unavailable" }, 503, noStore());
  for (const item of items) {
    if (item.uid !== String(batch.uid) || Number(item.account_generation) !== Number(batch.account_generation)) return c.json({ error: "chat_file_history_authority_changed" }, 409, noStore());
    if (!(await verifyHmac(providerSecret, attestations.get(item.import_id) || "", providerAttestationPayload(item)))) return c.json({ error: "provider_attestation_invalid" }, 409, noStore());
  }
  if (!c.env.CHAT_FILES) return c.json({ error: "chat_file_history_unavailable" }, 503, noStore());
  for (const item of items) {
    let object: R2Object | null;
    try {
      object = await c.env.CHAT_FILES.head(item.storage_key);
    } catch {
      return c.json({ error: "chat_file_history_unavailable" }, 503, noStore());
    }
    const metadata = object?.customMetadata || {};
    const objectChecksum = metadata.checksum || metadata.checksum_sha256;
    if (!object || Number(object.size) !== item.size || objectChecksum !== item.checksum_sha256) return c.json({ error: "chat_file_r2_mismatch" }, 409, noStore());
  }
  const ids = items.map((item) => item.import_id);
  const placeholders = ids.map(() => "?").join(",");
  const applyRows = await c.env.APP_DB.prepare(
    `SELECT import_id, review_id, file_id, provider_file_id, checksum_sha256, plan_hash, account_generation, status FROM cf_chat_file_history_applies WHERE uid = ? AND import_id IN (${placeholders})`,
  ).bind(String(batch.uid), ...ids).all<Record<string, unknown>>();
  const appliedById = new Map((applyRows.results || []).map((row) => [String(row.import_id), row]));
  const fileIds = items.map((item) => item.file_id);
  const filePlaceholders = fileIds.map(() => "?").join(",");
  const canonicalRows = await c.env.APP_DB.prepare(
    `SELECT uid, file_id, provider_file_id, name, mime_type, size, checksum_sha256, storage_key, status FROM cf_chat_files WHERE uid = ? AND file_id IN (${filePlaceholders})`,
  ).bind(String(batch.uid), ...fileIds).all<Record<string, unknown>>();
  const canonicalById = new Map((canonicalRows.results || []).map((row) => [String(row.file_id), row]));
  const pending: FileItem[] = [];
  let alreadyApplied = 0;
  for (const item of items) {
    const applied = appliedById.get(item.import_id);
    if (applied) {
      if (String(applied.review_id) !== reviewId || String(applied.file_id) !== item.file_id || String(applied.provider_file_id) !== item.provider_file_id || String(applied.checksum_sha256) !== item.checksum_sha256 || String(applied.plan_hash) !== item.plan_hash || Number(applied.account_generation) !== item.account_generation || applied.status !== "applied") return c.json({ error: "chat_file_history_apply_conflict" }, 409, noStore());
      alreadyApplied += 1;
      continue;
    }
    const existing = canonicalById.get(item.file_id);
    if (existing) {
      if (existing.status !== "ready" || String(existing.provider_file_id) !== item.provider_file_id || String(existing.name) !== item.name || String(existing.mime_type) !== item.mime_type || Number(existing.size) !== item.size || String(existing.checksum_sha256) !== item.checksum_sha256 || String(existing.storage_key) !== item.storage_key) return c.json({ error: "chat_file_history_target_conflict" }, 409, noStore());
      return c.json({ error: "chat_file_history_target_conflict" }, 409, noStore());
    }
    pending.push(item);
  }
  const statements = [] as Array<ReturnType<JobsEnv["APP_DB"]["prepare"]>>;
  for (const item of pending) {
    statements.push(c.env.APP_DB.prepare(
      "UPDATE cf_chat_file_import_ledger SET account_generation = ?, status = 'applied', last_error = NULL, updated_at = ? WHERE uid = ? AND import_id = ? AND plan_hash = ? AND status = 'planned' AND (account_generation IS NULL OR account_generation = ?)",
    ).bind(item.account_generation, now, item.uid, item.import_id, item.plan_hash, item.account_generation));
    statements.push(c.env.APP_DB.prepare(
      "INSERT INTO cf_chat_files (uid, file_id, request_fingerprint, provider, provider_file_id, name, mime_type, size, checksum_sha256, storage_key, thumbnail_key, status, thumbnail_status, created_at, updated_at, last_error) SELECT ?, ?, ?, 'openai', ?, ?, ?, ?, ?, ?, NULL, 'ready', 'unsupported', ?, ?, NULL WHERE EXISTS (SELECT 1 FROM cf_account_cutover WHERE uid = ? AND state = 'new' AND checkpoint_phase = 'completed' AND destination_backend_bound = 1 AND account_generation = ?) AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?)",
    ).bind(item.uid, item.file_id, item.request_fingerprint, item.provider_file_id, item.name, item.mime_type, item.size, item.checksum_sha256, item.storage_key, item.created_at, now, item.uid, item.account_generation, item.uid, item.uid));
    statements.push(c.env.APP_DB.prepare(
      "INSERT INTO cf_chat_file_history_applies (uid, import_id, review_id, file_id, provider_file_id, checksum_sha256, plan_hash, account_generation, status, applied_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)",
    ).bind(item.uid, item.import_id, reviewId, item.file_id, item.provider_file_id, item.checksum_sha256, item.plan_hash, item.account_generation, now, now));
  }
  statements.push(c.env.APP_DB.prepare("UPDATE cf_chat_file_history_review_batches SET status = 'applied', updated_at = ? WHERE review_id = ? AND status = 'approved' AND expires_at > ?").bind(now, reviewId, now));
  try {
    if (statements.length) {
      const results = await c.env.APP_DB.batch(statements);
      for (let index = 0; index < pending.length; index += 1) {
        const ledger = results[index * 3];
        if (!ledger?.meta || Number(ledger.meta.changes || 0) !== 1) throw new Error("chat file history ledger authority changed");
      }
    }
    return c.json({ review_id: reviewId, status: "applied", entry_count: items.length, applied_count: pending.length, already_applied_count: alreadyApplied }, 200, noStore());
  } catch (error) {
    if (error instanceof Error && (error.message.includes("account deletion fence") || error.message.includes("authority changed"))) return c.json({ error: "chat_file_history_authority_changed" }, 409, noStore());
    if (error instanceof Error && error.message.includes("UNIQUE")) return c.json({ error: "chat_file_history_target_conflict" }, 409, noStore());
    return c.json({ error: "chat_file_history_apply_unavailable" }, 503, noStore());
  }
}

export function registerChatFileHistoryImportRoutes(app: Hono<{ Bindings: JobsEnv }>): void {
  app.post(REVIEW_PATH, async (c) => {
    if (c.env.CHAT_FILE_HISTORY_IMPORT_STAGING_ENABLED !== "true") return c.json({ error: "chat_file_history_import_unavailable" }, 503, noStore());
    if (!adminAuthorized(c)) return c.json({ error: "forbidden" }, 403, noStore());
    try {
      return await review(c);
    } catch {
      return c.json({ error: "chat_file_history_review_unavailable" }, 503, noStore());
    }
  });
  app.post(`${REVIEW_PATH}/:reviewId/apply`, async (c) => {
    if (c.env.CHAT_FILE_HISTORY_IMPORT_STAGING_ENABLED !== "true") return c.json({ error: "chat_file_history_import_unavailable" }, 503, noStore());
    if (!adminAuthorized(c)) return c.json({ error: "forbidden" }, 403, noStore());
    try {
      return await apply(c);
    } catch {
      return c.json({ error: "chat_file_history_apply_unavailable" }, 503, noStore());
    }
  });
}

export const chatFileHistoryImportConstants = Object.freeze({
  reviewPath: REVIEW_PATH,
  maxEntries: MAX_ENTRIES,
  reviewTtlSeconds: REVIEW_TTL_SECONDS,
  providerAttestationPayload,
  reviewSignaturePayload,
});
