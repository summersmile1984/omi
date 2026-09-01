import type { Message } from "@cloudflare/workers-types";
import type { Context, Hono } from "hono";
import type { JobMessage, JobsEnv } from "./env";

/**
 * Staging-only preparation of legacy-compatible encrypted payloads.
 *
 * This is intentionally not the owner of /v1/users/migration/* yet.  It reads
 * an already-projected D1 source row, binds the cleartext to a source hash, and
 * stores only the resulting legacy-shaped ciphertext in the existing
 * migration receipt.  Canonical source rows are not changed until every D1
 * reader and derived index has an agreed decrypt contract.
 */

const ROUTE_PATH = "/internal/data-protection/migrations";
const MAX_BODY_BYTES = 64 * 1024;
const MAX_ITEMS = 25;
const MAX_ID_LENGTH = 256;
const MAX_UID_LENGTH = 256;
const MAX_FIELD_BYTES = 50_000;
const MAX_RESULT_BYTES = 64 * 1024;
const LEASE_SECONDS = 15 * 60;
const RETRY_DELAY_SECONDS = 10;
const TARGET_LEVEL = "enhanced";
const ENVELOPE_SCHEME = "legacy-hkdf-sha256-aes-256-gcm-v1";
const IDENTIFIER = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;
const RUN_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_ERROR_LENGTH = 256;

type JobsContext = Context<{ Bindings: JobsEnv }>;

type MigrationItem = {
  type: "conversation" | "memory" | "chat";
  id: string;
};

type SourceRef = MigrationItem & {
  sourceSha256: string;
};

type PreparationRequest = {
  uid: string;
  accountGeneration: number;
  operation: "single" | "batch";
  targetLevel: "enhanced";
  items: MigrationItem[];
};

type PreparationPlan = PreparationRequest & {
  sourceRefs: SourceRef[];
};

type SourceRow = {
  type: MigrationItem["type"];
  id: string;
  dataProtectionLevel: string | null;
  source: Record<string, unknown>;
};

type PreparedItem = {
  type: MigrationItem["type"];
  id: string;
  source_sha256: string;
  fields: Record<string, string>;
};

class PreparationError extends Error {}

function noStore(): Record<string, string> {
  return { "cache-control": "no-store" };
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

function adminAuthorized(c: JobsContext): boolean {
  const expected = c.env.ADMIN_KEY || "";
  const supplied = c.req.header("secret-key") || "";
  return Boolean(expected && supplied && constantTimeEqual(expected, supplied));
}

function gate(c: JobsContext): Response | null {
  if (c.env.DATA_PROTECTION_EXECUTOR_STAGING_ENABLED !== "true") {
    return c.json({ error: "data_protection_executor_unavailable" }, 503, noStore());
  }
  if (!adminAuthorized(c)) return c.json({ error: "forbidden" }, 403, noStore());
  const secret = new TextEncoder().encode(String(c.env.DATA_PROTECTION_ENCRYPTION_SECRET || ""));
  if (secret.byteLength < 32) {
    return c.json({ error: "data_protection_executor_unavailable", reason: "encryption_key_unavailable" }, 503, noStore());
  }
  return null;
}

function validIdentifier(value: unknown, maximum = MAX_ID_LENGTH): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maximum && IDENTIFIER.test(value);
}

function validGeneration(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function stableJson(value: unknown): string {
  if (value === null || typeof value !== "object") {
    const encoded = JSON.stringify(value);
    if (encoded === undefined) throw new PreparationError("value is not serializable");
    return encoded;
  }
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`).join(",")}}`;
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function base64Standard(value: Uint8Array): string {
  let binary = "";
  for (let offset = 0; offset < value.byteLength; offset += 0x8000) {
    binary += String.fromCharCode(...value.subarray(offset, Math.min(offset + 0x8000, value.byteLength)));
  }
  return btoa(binary);
}

function bytesHex(value: Uint8Array): string {
  return Array.from(value, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** Match backend/database/conversations.py's zlib-wrapped transcript format. */
async function compressTranscriptJson(value: string): Promise<string> {
  const input = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(value));
      controller.close();
    },
  });
  const compressed = input.pipeThrough(
    new CompressionStream("deflate") as unknown as TransformStream<
      Uint8Array,
      Uint8Array
    >,
  );
  return bytesHex(new Uint8Array(await new Response(compressed).arrayBuffer()));
}

async function encryptionKey(env: JobsEnv, uid: string): Promise<CryptoKey> {
  const secret = new TextEncoder().encode(String(env.DATA_PROTECTION_ENCRYPTION_SECRET || ""));
  if (secret.byteLength < 32) throw new PreparationError("encryption key unavailable");
  const base = await crypto.subtle.importKey("raw", secret, "HKDF", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    {
      name: "HKDF",
      hash: "SHA-256",
      salt: new TextEncoder().encode(uid),
      info: new TextEncoder().encode("user-data-encryption"),
    },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt"],
  );
}

async function encryptLegacyString(key: CryptoKey, value: string): Promise<string> {
  if (new TextEncoder().encode(value).byteLength > MAX_FIELD_BYTES) {
    throw new PreparationError("source field is too large");
  }
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce },
    key,
    new TextEncoder().encode(value),
  );
  const payload = new Uint8Array(nonce.byteLength + ciphertext.byteLength);
  payload.set(nonce, 0);
  payload.set(new Uint8Array(ciphertext), nonce.byteLength);
  return base64Standard(payload);
}

function jsonString(value: unknown, field: string): string {
  if (typeof value !== "string" || !value || new TextEncoder().encode(value).byteLength > MAX_FIELD_BYTES) {
    throw new PreparationError(`invalid ${field}`);
  }
  try {
    JSON.parse(value);
  } catch {
    throw new PreparationError(`invalid ${field} JSON`);
  }
  return value;
}

function parseMessage(value: unknown): Record<string, unknown> {
  if (typeof value !== "string" || new TextEncoder().encode(value).byteLength > MAX_FIELD_BYTES) {
    throw new PreparationError("invalid message_json");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new PreparationError("invalid message_json JSON");
  }
  const object = objectValue(parsed);
  if (!object) throw new PreparationError("message_json must be an object");
  if (typeof object.text !== "string" || !object.text) throw new PreparationError("message_json.text is missing");
  return object;
}

function parsePhotos(value: string): Array<Record<string, unknown>> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new PreparationError("invalid photos_json JSON");
  }
  if (!Array.isArray(parsed)) throw new PreparationError("photos_json must be an array");
  return parsed.map((item) => {
    const object = objectValue(item);
    if (!object) throw new PreparationError("photos_json contains an invalid item");
    return object;
  });
}

async function sourceRow(env: JobsEnv, uid: string, item: MigrationItem): Promise<SourceRow> {
  if (item.type === "memory") {
    const row = await env.APP_DB.prepare(
      "SELECT id, content, evidence_json, data_protection_level, updated_at FROM cf_memories WHERE uid = ? AND id = ? AND deleted_at IS NULL LIMIT 1",
    ).bind(uid, item.id).first<Record<string, unknown>>();
    if (!row) throw new PreparationError("source row not found");
    if (row.data_protection_level === TARGET_LEVEL) throw new PreparationError("source row is already enhanced");
    if (typeof row.content !== "string" || !row.content) throw new PreparationError("memory content projection missing");
    if (typeof row.evidence_json !== "string" || !row.evidence_json) throw new PreparationError("memory evidence projection missing");
    jsonString(row.evidence_json, "evidence_json");
    return {
      type: item.type,
      id: item.id,
      dataProtectionLevel: typeof row.data_protection_level === "string" ? row.data_protection_level : null,
      source: { content: row.content, evidence_json: row.evidence_json, updated_at: row.updated_at ?? null },
    };
  }
  if (item.type === "conversation") {
    const row = await env.APP_DB.prepare(
      "SELECT id, visibility, structured_json, transcript_segments_json, photos_json, data_protection_level, updated_at FROM cf_conversations WHERE uid = ? AND id = ? LIMIT 1",
    ).bind(uid, item.id).first<Record<string, unknown>>();
    if (!row) throw new PreparationError("source row not found");
    if (row.visibility === "public" || row.visibility === "shared") throw new PreparationError("public conversation is not migratable");
    if (row.data_protection_level === TARGET_LEVEL) throw new PreparationError("source row is already enhanced");
    const transcript = jsonString(row.transcript_segments_json, "transcript_segments_json");
    const photos = jsonString(row.photos_json, "photos_json");
    parsePhotos(photos);
    return {
      type: item.type,
      id: item.id,
      dataProtectionLevel: typeof row.data_protection_level === "string" ? row.data_protection_level : null,
      source: {
        structured_json: jsonString(row.structured_json, "structured_json"),
        transcript_segments_json: transcript,
        photos_json: photos,
        visibility: row.visibility ?? "private",
        updated_at: row.updated_at ?? null,
      },
    };
  }
  const row = await env.APP_DB.prepare(
    "SELECT id, message_json, data_protection_level, created_at FROM cf_chat_messages WHERE uid = ? AND id = ? LIMIT 1",
  ).bind(uid, item.id).first<Record<string, unknown>>();
  if (!row) throw new PreparationError("source row not found");
  if (row.data_protection_level === TARGET_LEVEL) throw new PreparationError("source row is already enhanced");
  const message = parseMessage(row.message_json);
  return {
    type: item.type,
    id: item.id,
    dataProtectionLevel: typeof row.data_protection_level === "string" ? row.data_protection_level : null,
    source: { message_json: row.message_json, created_at: row.created_at ?? null, text: message.text },
  };
}

async function sourceHash(row: SourceRow): Promise<string> {
  return sha256Hex(stableJson({ type: row.type, id: row.id, source: row.source, data_protection_level: row.dataProtectionLevel }));
}

function parseItem(value: unknown): MigrationItem | null {
  const object = objectValue(value);
  if (!object || (object.type !== "memory" && object.type !== "conversation" && object.type !== "chat") || !validIdentifier(object.id)) return null;
  return { type: object.type, id: object.id };
}

async function readBody(c: JobsContext): Promise<unknown | null> {
  const declared = Number(c.req.header("content-length"));
  if (Number.isFinite(declared) && (declared < 1 || declared > MAX_BODY_BYTES)) return null;
  const raw = await c.req.text();
  if (!raw || new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES) return null;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return null;
  }
}

async function accountGeneration(env: JobsEnv, uid: string, requested: number): Promise<void> {
  const fence = await env.APP_DB.prepare(
    "SELECT uid FROM cf_account_deletion_intents WHERE uid = ? UNION ALL SELECT uid FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > unixepoch() LIMIT 1",
  ).bind(uid, uid).first();
  if (fence) throw new PreparationError("account deletion fence");
  const cutover = await env.APP_DB.prepare(
    "SELECT state, checkpoint_phase, destination_backend_bound, account_generation FROM cf_account_cutover WHERE uid = ? LIMIT 1",
  ).bind(uid).first<Record<string, unknown>>();
  if (!cutover || cutover.state !== "new" || cutover.checkpoint_phase !== "completed" || Number(cutover.destination_backend_bound) !== 1 || Number(cutover.account_generation) !== requested) {
    throw new PreparationError("completed cutover unavailable");
  }
}

async function parseRequest(c: JobsContext, value: unknown): Promise<PreparationRequest | null> {
  const object = objectValue(value);
  const uid = object?.uid;
  const accountGeneration = object?.account_generation;
  const operation = object?.operation;
  const targetLevel = object?.target_level;
  const rawItems = object?.items;
  if (!validIdentifier(uid, MAX_UID_LENGTH) || !validGeneration(accountGeneration) || (operation !== "single" && operation !== "batch") || targetLevel !== TARGET_LEVEL || !Array.isArray(rawItems) || rawItems.length < 1 || rawItems.length > MAX_ITEMS) return null;
  const items = rawItems.map(parseItem);
  if (items.some((item) => item === null)) return null;
  const normalized = items as MigrationItem[];
  if (operation === "single" && normalized.length !== 1) return null;
  if (new Set(normalized.map((item) => `${item.type}:${item.id}`)).size !== normalized.length) return null;
  return { uid, accountGeneration, operation, targetLevel, items: normalized };
}

async function fingerprint(value: unknown): Promise<string> {
  return sha256Hex(stableJson(value));
}

async function admission(c: JobsContext, request: PreparationRequest): Promise<Response> {
  try {
    await accountGeneration(c.env, request.uid, request.accountGeneration);
    const sourceRefs: SourceRef[] = [];
    for (const item of request.items) {
      const row = await sourceRow(c.env, request.uid, item);
      sourceRefs.push({ ...item, sourceSha256: await sourceHash(row) });
    }
    const plan: PreparationPlan = { ...request, sourceRefs };
    const requestFingerprint = await fingerprint(plan);
    const payloadJson = stableJson({ schema_version: 1, phase: "prepare", plan });
    if (new TextEncoder().encode(payloadJson).byteLength > MAX_RESULT_BYTES) {
      return c.json({ error: "invalid_request", detail: "migration plan is too large" }, 413, noStore());
    }
    const runId: string = crypto.randomUUID();
    const now = Math.floor(Date.now() / 1000);
    const inserted = await c.env.APP_DB.prepare(
      "INSERT INTO cf_data_protection_migration_runs (uid, run_id, operation, request_fingerprint, payload_json, target_level, status, attempts, next_attempt_at, account_generation, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?) ON CONFLICT(uid, request_fingerprint) DO NOTHING",
    ).bind(request.uid, runId, request.operation, requestFingerprint, payloadJson, TARGET_LEVEL, now, request.accountGeneration, now, now).run();
    let effectiveRunId = runId;
    let existing: Record<string, unknown> | null = null;
    if (inserted.meta?.changes !== 1) {
      existing = await c.env.APP_DB.prepare(
        "SELECT run_id, status, operation, target_level, account_generation FROM cf_data_protection_migration_runs WHERE uid = ? AND request_fingerprint = ? LIMIT 1",
      ).bind(request.uid, requestFingerprint).first<Record<string, unknown>>();
      if (!existing) return c.json({ error: "data_protection_executor_unavailable" }, 503, noStore());
      effectiveRunId = String(existing.run_id);
    }
    if (!existing) {
      try {
        await c.env.JOBS.send({ jobId: effectiveRunId, uid: request.uid, kind: "data_protection_migration", payload: { runId: effectiveRunId } });
      } catch {
        await c.env.APP_DB.prepare(
          "UPDATE cf_data_protection_migration_runs SET status = 'failed', last_error = ?, updated_at = ? WHERE uid = ? AND run_id = ? AND status = 'queued'",
        ).bind("queue unavailable", now, request.uid, effectiveRunId).run();
        return c.json({ error: "data_protection_executor_unavailable", reason: "queue_unavailable" }, 503, noStore());
      }
    }
    return c.json({ run_id: effectiveRunId, status: existing?.status || "queued", operation: existing?.operation || request.operation, target_level: existing?.target_level || TARGET_LEVEL, account_generation: Number(existing?.account_generation ?? request.accountGeneration) }, existing ? 200 : 202, noStore());
  } catch (error) {
    if (error instanceof PreparationError && error.message === "account deletion fence") return c.json({ error: "account_deletion_in_progress" }, 409, noStore());
    if (error instanceof PreparationError) return c.json({ error: "data_protection_source_unavailable", reason: error.message }, 409, noStore());
    return c.json({ error: "data_protection_executor_unavailable" }, 503, noStore());
  }
}

function requestRunId(value: string): boolean {
  return RUN_ID.test(value);
}

async function status(c: JobsContext): Promise<Response> {
  const uid = c.req.query("uid") || "";
  const runId = c.req.param("runId") || "";
  if (!validIdentifier(uid, MAX_UID_LENGTH) || !requestRunId(runId)) return c.json({ error: "invalid_request" }, 400, noStore());
  const row = await c.env.APP_DB.prepare(
    "SELECT uid, run_id, operation, target_level, status, attempts, account_generation, result_json, last_error, updated_at FROM cf_data_protection_migration_runs WHERE uid = ? AND run_id = ? LIMIT 1",
  ).bind(uid, runId).first<Record<string, unknown>>();
  if (!row) return c.json({ error: "not_found" }, 404, noStore());
  let preparedCount = 0;
  let phase: string | null = null;
  if (typeof row.result_json === "string") {
    try {
      const result = objectValue(JSON.parse(row.result_json));
      phase = typeof result?.phase === "string" ? result.phase : null;
      preparedCount = Array.isArray(result?.items) ? result.items.length : 0;
    } catch {
      return c.json({ error: "data_protection_executor_unavailable" }, 503, noStore());
    }
  }
  return c.json({
    run_id: String(row.run_id),
    uid,
    operation: String(row.operation),
    target_level: String(row.target_level),
    status: String(row.status),
    attempts: Number(row.attempts),
    account_generation: Number(row.account_generation),
    phase,
    prepared_count: preparedCount,
    last_error: row.last_error || null,
    updated_at: Number(row.updated_at),
  }, 200, noStore());
}

async function preparedFields(env: JobsEnv, uid: string, key: CryptoKey, row: SourceRow): Promise<Record<string, string>> {
  if (row.type === "memory") {
    return {
      content: await encryptLegacyString(key, String(row.source.content)),
      evidence_json: await encryptLegacyString(key, String(row.source.evidence_json)),
    };
  }
  if (row.type === "conversation") {
    const photos = parsePhotos(String(row.source.photos_json));
    const encryptedPhotos: Array<Record<string, unknown>> = [];
    for (const photo of photos) {
      const copy = { ...photo };
      if (typeof copy.base64 === "string" && copy.base64) copy.base64 = await encryptLegacyString(key, copy.base64);
      encryptedPhotos.push(copy);
    }
    return {
      // Python writes JSON -> zlib.compress -> bytes.hex() before AES-GCM and
      // sets this marker.  Encrypting the JSON directly would make the legacy
      // reader attempt zlib.decompress on an uncompressed payload.
      transcript_segments_json: await encryptLegacyString(
        key,
        await compressTranscriptJson(String(row.source.transcript_segments_json)),
      ),
      transcript_segments_compressed: "true",
      photos_json: JSON.stringify(encryptedPhotos),
    };
  }
  const message = parseMessage(String(row.source.message_json));
  message.text = await encryptLegacyString(key, String(message.text));
  return { message_json: JSON.stringify(message) };
}

async function process(cMessage: Message<JobMessage>, env: JobsEnv): Promise<void> {
  const runId = objectValue(cMessage.body.payload)?.runId;
  if (typeof runId !== "string" || !requestRunId(runId)) {
    cMessage.ack();
    return;
  }
  const row = await env.APP_DB.prepare(
    "SELECT uid, run_id, status, attempts, lease_token, lease_until, payload_json, account_generation FROM cf_data_protection_migration_runs WHERE run_id = ? LIMIT 1",
  ).bind(runId).first<Record<string, unknown>>();
  if (!row) {
    cMessage.ack();
    return;
  }
  if (row.status === "completed" || row.status === "failed") {
    cMessage.ack();
    return;
  }
  const now = Math.floor(Date.now() / 1000);
  const leaseToken = crypto.randomUUID();
  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_data_protection_migration_runs SET status = 'running', attempts = attempts + 1, lease_token = ?, lease_until = ?, updated_at = ? WHERE run_id = ? AND uid = ? AND (status = 'queued' OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?))",
  ).bind(leaseToken, now + LEASE_SECONDS, now, runId, row.uid, now).run();
  if (claimed.meta?.changes !== 1) {
    cMessage.retry({ delaySeconds: RETRY_DELAY_SECONDS });
    return;
  }
  try {
    const payload = objectValue(JSON.parse(String(row.payload_json)));
    const plan = objectValue(payload?.plan);
    const refs = Array.isArray(plan?.sourceRefs) ? plan.sourceRefs : [];
    const uid = String(row.uid);
    const generation = Number(row.account_generation);
    if (!plan || payload?.schema_version !== 1 || payload.phase !== "prepare" || !validGeneration(generation) || refs.length < 1 || refs.length > MAX_ITEMS) throw new PreparationError("invalid migration plan");
    await accountGeneration(env, uid, generation);
    const key = await encryptionKey(env, uid);
    const items: PreparedItem[] = [];
    for (const rawRef of refs) {
      const ref = objectValue(rawRef);
      const item = parseItem(ref);
      if (!item || typeof ref?.sourceSha256 !== "string" || !/^[0-9a-f]{64}$/.test(ref.sourceSha256)) throw new PreparationError("invalid source reference");
      const current = await sourceRow(env, uid, item);
      const currentHash = await sourceHash(current);
      if (currentHash !== ref.sourceSha256) throw new PreparationError("source changed");
      items.push({ type: item.type, id: item.id, source_sha256: currentHash, fields: await preparedFields(env, uid, key, current) });
    }
    const result = stableJson({ schema_version: 1, phase: "prepared", envelope_scheme: ENVELOPE_SCHEME, account_generation: generation, items });
    if (new TextEncoder().encode(result).byteLength > MAX_RESULT_BYTES) throw new PreparationError("prepared payload is too large");
    await env.APP_DB.prepare(
      "UPDATE cf_data_protection_migration_runs SET status = 'completed', result_json = ?, last_error = NULL, lease_token = NULL, lease_until = NULL, updated_at = ? WHERE run_id = ? AND uid = ? AND lease_token = ?",
    ).bind(result, now, runId, uid, leaseToken).run();
    cMessage.ack();
  } catch (error) {
    if (error instanceof PreparationError) {
      const detail = error.message.slice(0, MAX_ERROR_LENGTH);
      await env.APP_DB.prepare(
        "UPDATE cf_data_protection_migration_runs SET status = 'failed', last_error = ?, lease_token = NULL, lease_until = NULL, updated_at = ? WHERE run_id = ? AND uid = ? AND lease_token = ?",
      ).bind(detail, now, runId, String(row.uid), leaseToken).run();
      cMessage.ack();
      return;
    }
    throw error;
  }
}

export function registerDataProtectionMigrationRoutes(app: Hono<{ Bindings: JobsEnv }>): void {
  app.post(ROUTE_PATH, async (c) => {
    const denied = gate(c);
    if (denied) return denied;
    const body = await readBody(c);
    const parsed = await parseRequest(c, body);
    if (!parsed) return c.json({ error: "invalid_request" }, 400, noStore());
    return admission(c, parsed);
  });
  app.get(`${ROUTE_PATH}/:runId`, async (c) => {
    const denied = gate(c);
    if (denied) return denied;
    return status(c);
  });
}

export async function processDataProtectionMigrationMessage(message: Message<JobMessage>, env: JobsEnv): Promise<void> {
  await process(message, env);
}

export const dataProtectionExecutorConstants = Object.freeze({
  routePath: ROUTE_PATH,
  envelopeScheme: ENVELOPE_SCHEME,
  maxItems: MAX_ITEMS,
  leaseSeconds: LEASE_SECONDS,
});
