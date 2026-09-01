import type { Context, Hono } from "hono";
import type { JobsEnv } from "./env";

const REVIEW_PATH = "/internal/persona-app-history/reviews";
const MAX_BODY_BYTES = 1_000_000;
const MAX_ENTRIES = 50;
const REVIEW_TTL_SECONDS = 60 * 60;
const MAX_METADATA_BYTES = 500 * 1024;
const SHA256 = /^[0-9a-f]{64}$/;
const OPAQUE_SOURCE = /^fb-anon-([0-9a-f]{64})$/;
const SAFE_ID = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;
const REVISION = /^[0-9a-f]{64}$/;

const PRIVATE_KEYS = new Set([
  "access_token", "api_key", "authorization", "avatar", "bearer",
  "client_secret", "credential", "credentials", "custom_token", "email",
  "firebase_id_token", "firebase_uid", "id_token", "image", "image_url",
  "logo", "logo_url", "mcp_oauth_tokens", "memory_prompt", "openai_api_key",
  "password", "persona_prompt", "photo_url", "private_key", "refresh_token",
  "secret", "secret_key", "token", "twitter", "uid", "owner_uid",
]);

type JobsContext = Context<{ Bindings: JobsEnv }>;
type JsonObject = Record<string, unknown>;

type Source = {
  kind: "firestore";
  collection: "plugins_data";
  export_sha256: string;
  exported_at?: string;
};

type Entry = {
  sourceRef: string;
  sourceUidHash: string;
  uid: string;
  appId: string;
  sourceProjectionRevision: string;
  targetAccountGeneration: number;
  sourceFingerprint: string;
  sourceExportSha256: string;
  publicMetadataJson: string;
  privateEnvelope: null;
  imageObject: null;
  createdAt: number;
  updatedAt: number;
  requestFingerprint: string;
  idempotencyKey: string;
  sourceRowSha256: string;
  action: "stage";
  status: "planned";
  lastError: null;
};

type Plan = {
  mode: "dry-run";
  schema_version: 1;
  source: Source;
  total: number;
  stage: number;
  blocked: number;
  entries: Entry[];
  manifest_sha256: string;
};

function noStore(): Record<string, string> {
  return { "cache-control": "no-store" };
}

function fail(message: string): never {
  throw new Error(`persona app history: ${message}`);
}

function objectValue(value: unknown): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : null;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as JsonObject;
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

function equal(left: string, right: string): boolean {
  const a = new TextEncoder().encode(left);
  const b = new TextEncoder().encode(right);
  let difference = a.length ^ b.length;
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    difference |= (a[index] || 0) ^ (b[index] || 0);
  }
  return difference === 0;
}

function adminAuthorized(c: JobsContext): boolean {
  const expected = c.env.APPS_ADMIN_KEY || "";
  const supplied = c.req.header("secret-key") || "";
  return Boolean(expected && supplied && equal(expected, supplied));
}

function assertObject(value: unknown, field: string): JsonObject {
  const object = objectValue(value);
  if (!object) fail(`${field} is invalid`);
  return object;
}

function assertKeys(object: JsonObject, allowed: readonly string[], field: string): void {
  const accepted = new Set(allowed);
  if (Object.keys(object).some((key) => !accepted.has(key))) fail(`${field} contains unsupported fields`);
}

function assertHash(value: unknown, field: string): string {
  if (typeof value !== "string" || !SHA256.test(value)) fail(`${field} is invalid`);
  return value;
}

function assertId(value: unknown, field: string): string {
  if (typeof value !== "string" || !SAFE_ID.test(value)) fail(`${field} is invalid`);
  return value;
}

function assertRevision(value: unknown, field: string): string {
  if (typeof value !== "string" || !REVISION.test(value)) fail(`${field} is invalid`);
  return value;
}

function assertInteger(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) fail(`${field} is invalid`);
  return value;
}

function sensitiveField(value: unknown, path = ""): string | null {
  if (!value || typeof value !== "object") return null;
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      const found = sensitiveField(value[index], `${path}[${index}]`);
      if (found) return found;
    }
    return null;
  }
  for (const [key, nested] of Object.entries(value as JsonObject)) {
    const field = path ? `${path}.${key}` : key;
    if (PRIVATE_KEYS.has(key.toLowerCase())) return field;
    const found = sensitiveField(nested, field);
    if (found) return found;
  }
  return null;
}

function assertJson(value: unknown, depth = 0, nodes = { count: 0 }): void {
  nodes.count += 1;
  if (depth > 20 || nodes.count > 4096) fail("public metadata is too large");
  if (!value || typeof value !== "object") return;
  for (const nested of Array.isArray(value) ? value : Object.values(value as JsonObject)) {
    assertJson(nested, depth + 1, nodes);
  }
}

function normalizeSource(value: unknown): Source {
  const source = assertObject(value, "source");
  assertKeys(source, ["kind", "collection", "export_sha256", "exported_at"], "source");
  if (source.kind !== "firestore" || source.collection !== "plugins_data") fail("source is not plugins_data");
  const result: Source = {
    kind: "firestore",
    collection: "plugins_data",
    export_sha256: assertHash(source.export_sha256, "source.export_sha256"),
  };
  if (source.exported_at !== undefined) {
    if (typeof source.exported_at !== "string" || source.exported_at.length < 1 || source.exported_at.length > 256 || source.exported_at.includes("\0")) fail("source.exported_at is invalid");
    result.exported_at = source.exported_at;
  }
  return result;
}

function normalizePublicMetadata(value: unknown, appId: string): string {
  if (typeof value !== "string" || value.length < 2 || new TextEncoder().encode(value).byteLength > MAX_METADATA_BYTES) fail("public_metadata_json is invalid");
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    fail("public_metadata_json is invalid");
  }
  const metadata = assertObject(parsed, "public_metadata");
  assertJson(metadata);
  const sensitive = sensitiveField(metadata);
  if (sensitive) fail(`public metadata contains private field ${sensitive}`);
  if (metadata.id !== undefined && metadata.id !== appId) fail("public metadata id mismatch");
  if (metadata.capabilities !== undefined && (!Array.isArray(metadata.capabilities) || metadata.capabilities.length > 64 || metadata.capabilities.some((item) => typeof item !== "string" || !item || new TextEncoder().encode(item).byteLength > 128))) {
    fail("public metadata capabilities are invalid");
  }
  return stableJson(metadata);
}

function field(raw: JsonObject, camel: string): unknown {
  return raw[camel];
}

async function normalizeEntry(value: unknown): Promise<Entry> {
  const raw = assertObject(value, "entry");
  assertKeys(raw, [
    "sourceRef", "sourceUidHash", "uid", "appId", "sourceProjectionRevision",
    "targetAccountGeneration", "sourceFingerprint", "sourceExportSha256",
    "publicMetadataJson", "privateEnvelope", "imageObject", "createdAt", "updatedAt",
    "requestFingerprint", "idempotencyKey", "sourceRowSha256", "action", "status", "lastError",
  ], "entry");
  if (raw.action !== "stage" || raw.status !== "planned" || raw.lastError !== null) fail("entry is not planned");
  const sourceRef = assertId(field(raw, "sourceRef"), "entry.sourceRef");
  const sourceMatch = OPAQUE_SOURCE.exec(sourceRef);
  if (!sourceMatch) fail("entry.sourceRef is not opaque");
  const sourceUidHash = assertHash(field(raw, "sourceUidHash"), "entry.sourceUidHash");
  if (sourceMatch[1] !== sourceUidHash) fail("entry source identity mismatch");
  const uid = assertId(field(raw, "uid"), "entry.uid");
  const appId = assertId(field(raw, "appId"), "entry.appId");
  const sourceProjectionRevision = assertRevision(field(raw, "sourceProjectionRevision"), "entry.sourceProjectionRevision");
  const targetAccountGeneration = assertInteger(field(raw, "targetAccountGeneration"), "entry.targetAccountGeneration");
  const sourceFingerprint = assertHash(field(raw, "sourceFingerprint"), "entry.sourceFingerprint");
  const sourceExportSha256 = assertHash(field(raw, "sourceExportSha256"), "entry.sourceExportSha256");
  if (raw.privateEnvelope !== null || raw.imageObject !== null) fail("private or image projection requires a separate executor");
  const publicMetadataJson = normalizePublicMetadata(field(raw, "publicMetadataJson"), appId);
  const createdAt = assertInteger(field(raw, "createdAt"), "entry.createdAt");
  const updatedAt = assertInteger(field(raw, "updatedAt"), "entry.updatedAt");
  const base = {
    sourceRef,
    sourceUidHash,
    uid,
    appId,
    sourceProjectionRevision,
    targetAccountGeneration,
    sourceFingerprint,
    sourceExportSha256,
    publicMetadataJson,
    privateEnvelope: null,
    imageObject: null,
    createdAt,
    updatedAt,
  };
  const sourceRowSha256 = await sha256(stableJson(base));
  const requestFingerprint = await sha256(`persona-app-history\0${sourceRef}\0${uid}\0${appId}\0${sourceRowSha256}`);
  const idempotencyKey = `persona-app-history-${requestFingerprint.slice(0, 40)}`;
  if (assertHash(field(raw, "sourceRowSha256"), "entry.sourceRowSha256") !== sourceRowSha256) fail("entry.sourceRowSha256 mismatch");
  if (assertHash(field(raw, "requestFingerprint"), "entry.requestFingerprint") !== requestFingerprint) fail("entry.requestFingerprint mismatch");
  if (typeof raw.idempotencyKey !== "string" || raw.idempotencyKey !== idempotencyKey) fail("entry.idempotencyKey mismatch");
  return {
    ...base,
    requestFingerprint,
    idempotencyKey,
    sourceRowSha256,
    action: "stage",
    status: "planned",
    lastError: null,
  };
}

async function normalizePlan(value: unknown): Promise<Plan> {
  const body = assertObject(value, "plan");
  assertKeys(body, ["mode", "schema_version", "source", "total", "stage", "blocked", "entries", "manifest_sha256"], "plan");
  if (body.mode !== "dry-run" || body.schema_version !== 1) fail("plan mode/schema is invalid");
  if (!Array.isArray(body.entries) || body.entries.length < 1 || body.entries.length > MAX_ENTRIES) fail("plan entries are invalid");
  const source = normalizeSource(body.source);
  const total = assertInteger(body.total, "plan.total");
  const stage = assertInteger(body.stage, "plan.stage");
  const blocked = assertInteger(body.blocked, "plan.blocked");
  if (total !== body.entries.length || stage !== body.entries.length || blocked !== 0) fail("plan counts are invalid");
  const entries = await Promise.all(body.entries.map((entry) => normalizeEntry(entry)));
  const first = entries[0];
  if (entries.some((entry) => entry.uid !== first.uid || entry.sourceRef !== first.sourceRef || entry.targetAccountGeneration !== first.targetAccountGeneration || entry.sourceExportSha256 !== source.export_sha256)) fail("plan spans identities or exports");
  const keys = new Set<string>();
  for (const entry of entries) {
    const key = `${entry.uid}\0${entry.appId}`;
    if (keys.has(key)) fail("plan contains duplicate app ids");
    keys.add(key);
  }
  const ordered = [...entries].sort((left, right) => `${left.uid}\0${left.appId}`.localeCompare(`${right.uid}\0${right.appId}`));
  const manifest = await sha256(stableJson({ schema_version: 1, source, rows: ordered.map((entry) => entry.sourceRowSha256) }));
  if (assertHash(body.manifest_sha256, "plan.manifest_sha256") !== manifest) fail("plan manifest mismatch");
  return { mode: "dry-run", schema_version: 1, source, total, stage, blocked, entries: ordered, manifest_sha256: manifest };
}

function reviewSignaturePayload(plan: Plan): string {
  return stableJson({
    manifest_sha256: plan.manifest_sha256,
    source: plan.source,
    entries: plan.entries.map((entry) => ({
      sourceRef: entry.sourceRef,
      sourceUidHash: entry.sourceUidHash,
      uid: entry.uid,
      appId: entry.appId,
      sourceProjectionRevision: entry.sourceProjectionRevision,
      targetAccountGeneration: entry.targetAccountGeneration,
      sourceFingerprint: entry.sourceFingerprint,
      sourceExportSha256: entry.sourceExportSha256,
      publicMetadataJson: entry.publicMetadataJson,
      privateEnvelope: null,
      imageObject: null,
      createdAt: entry.createdAt,
      updatedAt: entry.updatedAt,
      requestFingerprint: entry.requestFingerprint,
      idempotencyKey: entry.idempotencyKey,
      sourceRowSha256: entry.sourceRowSha256,
      action: "stage",
      status: "planned",
      lastError: null,
    })),
  });
}

function applySignaturePayload(reviewId: string, plan: Plan): string {
  return stableJson({ review_id: reviewId, ...JSON.parse(reviewSignaturePayload(plan)) });
}

function decodeBase64Url(value: string): Uint8Array | null {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) return null;
  try {
    const padded = value + "=".repeat((4 - value.length % 4) % 4);
    const binary = atob(padded.replaceAll("-", "+").replaceAll("_", "/"));
    return Uint8Array.from(binary, (char) => char.charCodeAt(0));
  } catch {
    return null;
  }
}

async function validSignature(c: JobsContext, payload: string): Promise<boolean> {
  const secret = c.env.PERSONA_APP_HISTORY_IMPORT_SIGNING_SECRET || "";
  const encoded = c.req.header("x-persona-app-plan-signature") || "";
  const signature = decodeBase64Url(encoded);
  if (secret.length < 32 || !signature) return false;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  const signatureBuffer = new Uint8Array(signature.byteLength);
  signatureBuffer.set(signature);
  return crypto.subtle.verify("HMAC", key, signatureBuffer.buffer, new TextEncoder().encode(payload));
}

async function sourceAuthority(env: JobsEnv, entry: Entry, now: number): Promise<boolean> {
  const row = await env.APP_DB.prepare(
    "SELECT source_uid, source_uid_hash, source_projection_revision, projection_status, target_uid, target_account_generation, attestation_expires_at, data_projection_status FROM cf_app_owner_migration_sources WHERE source_uid = ? LIMIT 1",
  ).bind(entry.sourceRef).first<Record<string, unknown>>();
  if (!row || row.source_uid !== entry.sourceRef || row.source_uid_hash !== entry.sourceUidHash || row.source_projection_revision !== entry.sourceProjectionRevision || row.projection_status !== "imported" || row.target_uid !== entry.uid || Number(row.target_account_generation) !== entry.targetAccountGeneration || Number(row.attestation_expires_at) <= now || row.data_projection_status !== "verified") return false;
  const cutover = await env.APP_DB.prepare(
    "SELECT state, checkpoint_phase, destination_backend_bound, account_generation FROM cf_account_cutover WHERE uid = ? LIMIT 1",
  ).bind(entry.uid).first<Record<string, unknown>>();
  if (!cutover || cutover.state !== "new" || cutover.checkpoint_phase !== "completed" || Number(cutover.destination_backend_bound) !== 1 || Number(cutover.account_generation) !== entry.targetAccountGeneration) return false;
  const fence = await env.APP_DB.prepare(
    "SELECT uid FROM cf_account_deletion_intents WHERE uid = ? UNION SELECT uid FROM cf_account_deletion_tombstones WHERE uid = ?",
  ).bind(entry.uid, entry.uid).first<Record<string, unknown>>();
  return !fence;
}

async function readBody(c: JobsContext): Promise<unknown | null> {
  const declared = Number(c.req.header("content-length"));
  if (Number.isFinite(declared) && (declared < 1 || declared > MAX_BODY_BYTES)) return null;
  const bytes = new Uint8Array(await c.req.raw.arrayBuffer());
  if (bytes.byteLength < 1 || bytes.byteLength > MAX_BODY_BYTES) return null;
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return null;
  }
}

async function review(c: JobsContext): Promise<Response> {
  const body = await readBody(c);
  let plan: Plan;
  try { plan = await normalizePlan(body); } catch { return c.json({ error: "invalid_plan" }, 422, noStore()); }
  if (!(await validSignature(c, reviewSignaturePayload(plan)))) return c.json({ error: "plan_signature_invalid" }, 403, noStore());
  const now = Math.floor(Date.now() / 1_000);
  if (!(await sourceAuthority(c.env, plan.entries[0], now))) return c.json({ error: "persona_app_history_authority_changed" }, 409, noStore());
  const reviewId = crypto.randomUUID();
  const statements = [c.env.APP_DB.prepare(
    "INSERT INTO cf_persona_app_history_review_batches (review_id, uid, source_ref, source_projection_revision, source_export_sha256, target_account_generation, manifest_sha256, entry_count, status, reviewed_at, expires_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?) ON CONFLICT(uid, manifest_sha256) DO NOTHING",
  ).bind(reviewId, plan.entries[0].uid, plan.entries[0].sourceRef, plan.entries[0].sourceProjectionRevision, plan.source.export_sha256, plan.entries[0].targetAccountGeneration, plan.manifest_sha256, plan.entries.length, now, now + REVIEW_TTL_SECONDS, now)];
  for (const entry of plan.entries) statements.push(c.env.APP_DB.prepare(
    "INSERT INTO cf_persona_app_history_review_items (review_id, uid, app_id, source_ref, source_fingerprint, source_row_sha256, request_fingerprint, source_projection_revision, source_export_sha256, target_account_generation, public_metadata_json, created_at, updated_at) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM cf_persona_app_history_review_batches WHERE review_id = ? AND uid = ? AND manifest_sha256 = ?) ON CONFLICT(review_id, uid, app_id) DO NOTHING",
  ).bind(reviewId, entry.uid, entry.appId, entry.sourceRef, entry.sourceFingerprint, entry.sourceRowSha256, entry.requestFingerprint, entry.sourceProjectionRevision, entry.sourceExportSha256, entry.targetAccountGeneration, entry.publicMetadataJson, now, now, reviewId, entry.uid, plan.manifest_sha256));
  try {
    await c.env.APP_DB.batch(statements);
    const existing = await c.env.APP_DB.prepare("SELECT review_id, entry_count, status, expires_at FROM cf_persona_app_history_review_batches WHERE uid = ? AND manifest_sha256 = ?").bind(plan.entries[0].uid, plan.manifest_sha256).first<Record<string, unknown>>();
    if (!existing) return c.json({ error: "persona_app_history_review_unavailable" }, 503, noStore());
    return c.json({ review_id: String(existing.review_id), manifest_sha256: plan.manifest_sha256, entry_count: Number(existing.entry_count), status: existing.status, expires_at: Number(existing.expires_at) }, String(existing.review_id) === reviewId ? 201 : 200, noStore());
  } catch (error) {
    if (error instanceof Error && error.message.includes("account deletion fence")) return c.json({ error: "persona_app_history_authority_changed" }, 409, noStore());
    return c.json({ error: "persona_app_history_review_unavailable" }, 503, noStore());
  }
}

async function apply(c: JobsContext): Promise<Response> {
  const reviewId = c.req.param("reviewId");
  if (!reviewId || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(reviewId)) return c.json({ error: "invalid_review_id" }, 422, noStore());
  const body = await readBody(c);
  const bodyObject = objectValue(body);
  if (!bodyObject || bodyObject.review_id !== reviewId) return c.json({ error: "invalid_request" }, 422, noStore());
  const planBody = { ...bodyObject };
  delete planBody.review_id;
  let plan: Plan;
  try { plan = await normalizePlan(planBody); } catch { return c.json({ error: "invalid_plan" }, 422, noStore()); }
  if (!(await validSignature(c, applySignaturePayload(reviewId, plan)))) return c.json({ error: "plan_signature_invalid" }, 403, noStore());
  const batch = await c.env.APP_DB.prepare("SELECT review_id, uid, source_ref, source_projection_revision, source_export_sha256, target_account_generation, manifest_sha256, entry_count, status, expires_at FROM cf_persona_app_history_review_batches WHERE review_id = ? LIMIT 1").bind(reviewId).first<Record<string, unknown>>();
  if (!batch) return c.json({ error: "review_not_found" }, 404, noStore());
  const now = Math.floor(Date.now() / 1_000);
  if (batch.status === "revoked" || Number(batch.expires_at) <= now) return c.json({ error: "review_expired" }, 409, noStore());
  if (String(batch.uid) !== plan.entries[0].uid || String(batch.source_ref) !== plan.entries[0].sourceRef || String(batch.source_projection_revision) !== plan.entries[0].sourceProjectionRevision || String(batch.source_export_sha256) !== plan.source.export_sha256 || Number(batch.target_account_generation) !== plan.entries[0].targetAccountGeneration || String(batch.manifest_sha256) !== plan.manifest_sha256 || Number(batch.entry_count) !== plan.entries.length) return c.json({ error: "persona_app_history_apply_conflict" }, 409, noStore());
  const itemRows = await c.env.APP_DB.prepare("SELECT uid, app_id, source_ref, source_fingerprint, source_row_sha256, request_fingerprint, source_projection_revision, source_export_sha256, target_account_generation, public_metadata_json FROM cf_persona_app_history_review_items WHERE review_id = ? ORDER BY app_id").bind(reviewId).all<Record<string, unknown>>();
  if (!itemRows.results || itemRows.results.length !== plan.entries.length) return c.json({ error: "review_incomplete" }, 409, noStore());
  const itemById = new Map(itemRows.results.map((item) => [String(item.app_id), item]));
  if (plan.entries.some((entry) => {
    const item = itemById.get(entry.appId);
    return !item || item.uid !== entry.uid || item.source_ref !== entry.sourceRef || item.source_fingerprint !== entry.sourceFingerprint || item.source_row_sha256 !== entry.sourceRowSha256 || item.request_fingerprint !== entry.requestFingerprint || item.source_projection_revision !== entry.sourceProjectionRevision || item.source_export_sha256 !== entry.sourceExportSha256 || Number(item.target_account_generation) !== entry.targetAccountGeneration || item.public_metadata_json !== entry.publicMetadataJson;
  })) return c.json({ error: "persona_app_history_apply_conflict" }, 409, noStore());
  if (!(await sourceAuthority(c.env, plan.entries[0], now))) return c.json({ error: "persona_app_history_authority_changed" }, 409, noStore());
  const ids = plan.entries.map((entry) => entry.appId);
  const placeholders = ids.map(() => "?").join(",");
  const catalogRows = await c.env.APP_DB.prepare(`SELECT id, owner_uid, owner_account_generation, owner_migration_job_id, approved, data_json FROM cf_app_catalog WHERE id IN (${placeholders})`).bind(...ids).all<Record<string, unknown>>();
  const catalogById = new Map((catalogRows.results || []).map((row) => [String(row.id), row]));
  const appliedRows = await c.env.APP_DB.prepare(`SELECT uid, app_id, review_id, manifest_sha256, source_ref, source_row_sha256, request_fingerprint, target_account_generation, status FROM cf_persona_app_history_applies WHERE uid = ? AND app_id IN (${placeholders})`).bind(plan.entries[0].uid, ...ids).all<Record<string, unknown>>();
  const appliedById = new Map((appliedRows.results || []).map((row) => [String(row.app_id), row]));
  let alreadyApplied = 0;
  for (const entry of plan.entries) {
    const existingApply = appliedById.get(entry.appId);
    if (existingApply) {
      if (existingApply.review_id !== reviewId || existingApply.manifest_sha256 !== plan.manifest_sha256 || existingApply.source_ref !== entry.sourceRef || existingApply.source_row_sha256 !== entry.sourceRowSha256 || existingApply.request_fingerprint !== entry.requestFingerprint || Number(existingApply.target_account_generation) !== entry.targetAccountGeneration || existingApply.status !== "applied") return c.json({ error: "persona_app_history_apply_conflict" }, 409, noStore());
      const catalog = catalogById.get(entry.appId);
      if (!catalog || catalog.owner_uid !== entry.uid || Number(catalog.owner_account_generation) !== entry.targetAccountGeneration || Number(catalog.approved) !== 0 || catalog.data_json !== entry.publicMetadataJson) return c.json({ error: "persona_app_history_target_conflict" }, 409, noStore());
      alreadyApplied += 1;
      continue;
    }
    const catalog = catalogById.get(entry.appId);
    if (catalog && (catalog.owner_uid !== entry.uid || Number(catalog.owner_account_generation) !== entry.targetAccountGeneration || Number(catalog.approved) !== 0 || catalog.data_json !== entry.publicMetadataJson)) return c.json({ error: "persona_app_history_target_conflict" }, 409, noStore());
  }
  const pending = plan.entries.filter((entry) => !appliedById.has(entry.appId));
  const statements = [];
  for (const entry of pending) {
    statements.push(c.env.APP_DB.prepare("INSERT INTO cf_app_catalog (id, approved, status, disabled, is_popular, installs, rating_avg, rating_count, data_json, updated_at, owner_uid, owner_account_generation, owner_migration_job_id) SELECT ?, 0, 'historical_import', 0, 0, 0, NULL, 0, ?, ?, ?, ?, NULL WHERE NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?) ON CONFLICT(id) DO NOTHING").bind(entry.appId, entry.publicMetadataJson, now, entry.uid, entry.targetAccountGeneration, entry.uid, entry.uid));
    statements.push(c.env.APP_DB.prepare("INSERT INTO cf_persona_app_history_applies (uid, app_id, review_id, manifest_sha256, source_ref, source_row_sha256, request_fingerprint, target_account_generation, status, applied_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?) ON CONFLICT(uid, app_id) DO NOTHING").bind(entry.uid, entry.appId, reviewId, plan.manifest_sha256, entry.sourceRef, entry.sourceRowSha256, entry.requestFingerprint, entry.targetAccountGeneration, now, now));
  }
  statements.push(c.env.APP_DB.prepare("UPDATE cf_persona_app_history_review_batches SET status = 'applied', updated_at = ? WHERE review_id = ? AND status = 'approved'").bind(now, reviewId));
  try {
    if (statements.length) await c.env.APP_DB.batch(statements);
    return c.json({ review_id: reviewId, status: "applied", entry_count: plan.entries.length, applied_count: pending.length, already_applied_count: alreadyApplied }, 200, noStore());
  } catch (error) {
    if (error instanceof Error && (error.message.includes("account deletion fence") || error.message.includes("authority changed"))) return c.json({ error: "persona_app_history_authority_changed" }, 409, noStore());
    return c.json({ error: "persona_app_history_apply_unavailable" }, 503, noStore());
  }
}

export function registerPersonaAppHistoryImportRoutes(app: Hono<{ Bindings: JobsEnv }>): void {
  app.post(REVIEW_PATH, async (c) => {
    if (c.env.PERSONA_APP_HISTORY_IMPORT_STAGING_ENABLED !== "true") return c.json({ error: "persona_app_history_import_unavailable" }, 503, noStore());
    if (!adminAuthorized(c)) return c.json({ error: "forbidden" }, 403, noStore());
    return review(c);
  });
  app.post(`${REVIEW_PATH}/:reviewId/apply`, async (c) => {
    if (c.env.PERSONA_APP_HISTORY_IMPORT_STAGING_ENABLED !== "true") return c.json({ error: "persona_app_history_import_unavailable" }, 503, noStore());
    if (!adminAuthorized(c)) return c.json({ error: "forbidden" }, 403, noStore());
    return apply(c);
  });
}

export const personaAppHistoryImportConstants = Object.freeze({
  reviewPath: REVIEW_PATH,
  maxEntries: MAX_ENTRIES,
  maxBodyBytes: MAX_BODY_BYTES,
  reviewSignaturePayload,
  applySignaturePayload,
});
