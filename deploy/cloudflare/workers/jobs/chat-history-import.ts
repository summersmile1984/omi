import type { Context, Hono } from "hono";
import type { JobsEnv } from "./env";

const APPLY_PATH = "/internal/chat-history/apply";
const MAX_BODY_BYTES = 1_000_000;
// Four D1 statements are emitted per row. Keep one apply below the D1 batch
// statement ceiling while retaining one transaction for the reviewed plan.
const MAX_ENTRIES = 20;
const MAX_ROW_BYTES = 256 * 1024;
const MAX_TEXT_BYTES = 200_000;
const MAX_ID_LENGTH = 256;
const MAX_JSON_DEPTH = 16;
const MAX_JSON_NODES = 2_048;
const SHA256 = /^[0-9a-f]{64}$/;
const UID = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;
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
  "raw_uid",
  "refresh_token",
  "secret",
  "secret_key",
  "token",
]);

type JobsContext = Context<{ Bindings: JobsEnv }>;
type Entry = {
  uid: string;
  import_id: string;
  entity_kind: "session" | "message";
  entity_id: string;
  account_generation: number;
  source_fingerprint: string;
  source_export_sha256: string;
  source_row_sha256: string;
  plan_hash: string;
  action: "stage";
  last_error: null;
  row: Record<string, unknown>;
};
type NormalizedEntry = Entry & {
  normalizedRow: Record<string, unknown>;
  expectedImportId: string;
  expectedSourceRowSha256: string;
  expectedPlanHash: string;
};

function fail(message: string): never {
  throw new Error(`chat history apply: ${message}`);
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`)
      .join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) fail("value is not JSON serializable");
  return encoded;
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = new TextEncoder().encode(left);
  const rightBytes = new TextEncoder().encode(right);
  let difference = leftBytes.length ^ rightBytes.length;
  const length = Math.max(leftBytes.length, rightBytes.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (leftBytes[index] || 0) ^ (rightBytes[index] || 0);
  }
  return difference === 0;
}

function noStoreHeaders(): Record<string, string> {
  return { "cache-control": "no-store" };
}

function adminAuthorized(c: JobsContext): boolean {
  const expected = c.env.ADMIN_KEY;
  const provided = c.req.header("secret-key");
  return Boolean(expected && provided && constantTimeEqual(provided, expected));
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
  for (const [key, nested] of Object.entries(value as Record<string, unknown>)) {
    const fieldPath = path ? `${path}.${key}` : key;
    if (SENSITIVE_KEYS.has(key.toLowerCase())) return fieldPath;
    const found = sensitiveField(nested, fieldPath);
    if (found) return found;
  }
  return null;
}

function assertJsonShape(value: unknown, depth = 0, state = { nodes: 0 }): void {
  state.nodes += 1;
  if (state.nodes > MAX_JSON_NODES || depth > MAX_JSON_DEPTH) fail("JSON structure is too deep or large");
  if (!value || typeof value !== "object") return;
  if (Array.isArray(value)) {
    for (const item of value) assertJsonShape(item, depth + 1, state);
    return;
  }
  for (const nested of Object.values(value as Record<string, unknown>)) {
    assertJsonShape(nested, depth + 1, state);
  }
}

function allowedKeys(value: Record<string, unknown>, allowed: Set<string>, field: string): void {
  if (Object.keys(value).some((key) => !allowed.has(key))) fail(`${field} contains unsupported fields`);
}

function requiredHash(value: unknown, field: string): string {
  if (typeof value !== "string" || !SHA256.test(value)) fail(`${field} is invalid`);
  return value;
}

function requiredId(value: unknown, field: string): string {
  if (typeof value !== "string" || value.length < 1 || value.length > MAX_ID_LENGTH || !UID.test(value)) {
    fail(`${field} is invalid`);
  }
  return value;
}

function requiredInteger(value: unknown, field: string, maximum = Number.MAX_SAFE_INTEGER): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0 || parsed > maximum) fail(`${field} is invalid`);
  return parsed;
}

function normalizedSource(value: unknown, fallbackExport: unknown): Record<string, unknown> {
  if (value === undefined || value === null) {
    return {
      kind: "firestore",
      collections: ["users/{uid}/chat_sessions", "users/{uid}/messages"],
      export_sha256: requiredHash(fallbackExport, "source_export_sha256"),
    };
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) fail("source is invalid");
  const source = value as Record<string, unknown>;
  assertJsonShape(source);
  const sensitive = sensitiveField(source);
  if (sensitive) fail(`source contains sensitive field ${sensitive}`);
  const allowed = new Set(["kind", "collections", "export_sha256", "exported_at"]);
  if (Object.keys(source).some((key) => !allowed.has(key))) fail("source contains unsupported fields");
  if (source.kind !== "firestore") fail("source.kind is invalid");
  if (
    !Array.isArray(source.collections) ||
    source.collections.length !== 2 ||
    new Set(source.collections).size !== 2 ||
    !source.collections.includes("users/{uid}/chat_sessions") ||
    !source.collections.includes("users/{uid}/messages")
  ) {
    fail("source.collections is invalid");
  }
  const exportSha256 = requiredHash(source.export_sha256 ?? fallbackExport, "source.export_sha256");
  if (fallbackExport !== undefined && requiredHash(fallbackExport, "source_export_sha256") !== exportSha256) {
    fail("source export hash does not match top-level export hash");
  }
  if (source.exported_at !== undefined) text(source.exported_at, "source.exported_at", 128);
  return {
    kind: "firestore",
    collections: ["users/{uid}/chat_sessions", "users/{uid}/messages"],
    export_sha256: exportSha256,
    ...(source.exported_at === undefined ? {} : { exported_at: source.exported_at }),
  };
}

function text(value: unknown, field: string, maximum: number, minimum = 0): string {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum || value.includes("\0")) {
    fail(`${field} is invalid`);
  }
  if (new TextEncoder().encode(value).byteLength > maximum) fail(`${field} is too large`);
  return value;
}

function normalizedSession(row: Record<string, unknown>, entry: Entry): Record<string, unknown> {
  const allowed = new Set(["uid", "id", "title", "preview", "created_at", "updated_at", "app_id", "message_count", "starred"]);
  allowedKeys(row, allowed, "session row");
  const uid = requiredId(row.uid, "session.uid");
  const id = requiredId(row.id, "session.id");
  if (uid !== entry.uid || id !== entry.entity_id) fail("session identity does not match entry");
  const title = text(row.title, "session.title", 500, 1);
  const preview = row.preview === null || row.preview === undefined ? null : text(row.preview, "session.preview", 1_000);
  const appId = row.app_id === null || row.app_id === undefined ? null : text(row.app_id, "session.app_id", 256, 1);
  return {
    uid,
    id,
    title,
    preview,
    created_at: requiredInteger(row.created_at, "session.created_at"),
    updated_at: requiredInteger(row.updated_at, "session.updated_at"),
    app_id: appId,
    message_count: requiredInteger(row.message_count ?? 0, "session.message_count", 1_000_000),
    starred: requiredInteger(row.starred ?? 0, "session.starred", 1),
  };
}

function normalizedMessage(row: Record<string, unknown>, entry: Entry): Record<string, unknown> {
  const allowed = new Set(["uid", "id", "app_id", "created_at", "message_json"]);
  allowedKeys(row, allowed, "message row");
  const uid = requiredId(row.uid, "message.uid");
  const id = requiredId(row.id, "message.id");
  if (uid !== entry.uid || id !== entry.entity_id) fail("message identity does not match entry");
  const appId = row.app_id === null || row.app_id === undefined ? null : text(row.app_id, "message.app_id", 256, 1);
  if (typeof row.message_json !== "string") fail("message_json must be a JSON string");
  if (new TextEncoder().encode(row.message_json).byteLength > MAX_ROW_BYTES) fail("message_json is too large");
  let message: unknown;
  try {
    message = JSON.parse(row.message_json);
  } catch {
    fail("message_json is invalid JSON");
  }
  assertJsonShape(message);
  const sensitive = sensitiveField(message);
  if (sensitive) fail(`message_json contains sensitive field ${sensitive}`);
  if (!message || typeof message !== "object" || Array.isArray(message)) fail("message_json must be an object");
  const object = message as Record<string, unknown>;
  if (object.id !== id) fail("message_json.id does not match message id");
  text(object.text, "message_json.text", MAX_TEXT_BYTES, 1);
  if (object.sender !== "human" && object.sender !== "ai") fail("message_json.sender is invalid");
  if (object.type !== "text" && object.type !== "day_summary") fail("message_json.type is invalid");
  if (object.files !== undefined || object.attachments !== undefined || object.file_ids !== undefined || object.files_id !== undefined) {
    fail("message file references require a separate verified import");
  }
  const sessionId = object.chat_session_id ?? object.session_id;
  if (typeof sessionId !== "string" || sessionId.length < 1 || sessionId.length > MAX_ID_LENGTH || !UID.test(sessionId)) {
    fail("message session id is invalid");
  }
  return {
    uid,
    id,
    app_id: appId,
    created_at: requiredInteger(row.created_at, "message.created_at"),
    message_json: JSON.stringify(object),
  };
}

async function normalizeEntry(value: unknown, index: number): Promise<NormalizedEntry> {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`entry ${index + 1} is invalid`);
  const raw = value as Record<string, unknown>;
  assertJsonShape(raw);
  allowedKeys(
    raw,
    new Set([
      "uid", "entityKind", "entity_kind", "entityId", "entity_id", "accountGeneration", "account_generation",
      "sourceFingerprint", "source_fingerprint", "sourceExportSha256", "source_export_sha256",
      "sourceRowSha256", "source_row_sha256", "importId", "import_id", "row", "fileIds", "file_ids",
      "action", "status", "lastError", "last_error", "planHash", "plan_hash",
    ]),
    `entry ${index + 1}`,
  );
  const sensitive = sensitiveField(raw);
  if (sensitive) fail(`entry ${index + 1} contains sensitive field ${sensitive}`);
  if (raw.action !== "stage" || (raw.last_error !== null && raw.last_error !== undefined) || (raw.lastError !== null && raw.lastError !== undefined)) fail(`entry ${index + 1} is not staged`);
  if (raw.status !== "planned") fail(`entry ${index + 1} is not planned`);
  if (raw.lastError !== null && raw.last_error !== null) fail(`entry ${index + 1} has duplicate last_error fields`);
  const fileIds = raw.fileIds ?? raw.file_ids;
  if (!Array.isArray(fileIds) || fileIds.length !== 0) fail(`entry ${index + 1} contains file references`);
  const field = (snake: string, camel: string): unknown => raw[snake] ?? raw[camel];
  const entityKind = field("entity_kind", "entityKind");
  if (entityKind !== "session" && entityKind !== "message") fail(`entry ${index + 1}.entity_kind is invalid`);
  const entry: Entry = {
    uid: requiredId(field("uid", "uid"), `entry ${index + 1}.uid`),
    import_id: requiredHash(field("import_id", "importId"), `entry ${index + 1}.import_id`),
    entity_kind: entityKind,
    entity_id: requiredId(field("entity_id", "entityId"), `entry ${index + 1}.entity_id`),
    account_generation: requiredInteger(field("account_generation", "accountGeneration"), `entry ${index + 1}.account_generation`),
    source_fingerprint: requiredHash(field("source_fingerprint", "sourceFingerprint"), `entry ${index + 1}.source_fingerprint`),
    source_export_sha256: requiredHash(field("source_export_sha256", "sourceExportSha256"), `entry ${index + 1}.source_export_sha256`),
    source_row_sha256: requiredHash(field("source_row_sha256", "sourceRowSha256"), `entry ${index + 1}.source_row_sha256`),
    plan_hash: requiredHash(field("plan_hash", "planHash"), `entry ${index + 1}.plan_hash`),
    action: "stage",
    last_error: null,
    row: raw.row && typeof raw.row === "object" && !Array.isArray(raw.row) ? raw.row as Record<string, unknown> : (() => { fail(`entry ${index + 1}.row is invalid`); })(),
  };
  const normalizedRow = entry.entity_kind === "session" ? normalizedSession(entry.row, entry) : normalizedMessage(entry.row, entry);
  const expectedSourceRowSha256 = await sha256(stableJson({
    kind: entry.entity_kind,
    source_export_sha256: entry.source_export_sha256,
    row: normalizedRow,
  }));
  const expectedImportId = await sha256(`${entry.uid}\0${entry.entity_kind}\0${entry.entity_id}\0${entry.source_fingerprint}\0${expectedSourceRowSha256}`);
  const expectedPlanHash = await sha256(stableJson({
    uid: entry.uid,
    entity_kind: entry.entity_kind,
    entity_id: entry.entity_id,
    account_generation: entry.account_generation,
    source_fingerprint: entry.source_fingerprint,
    source_row_sha256: expectedSourceRowSha256,
    import_id: expectedImportId,
    action: "stage",
    last_error: null,
  }));
  if (entry.source_row_sha256 !== expectedSourceRowSha256) fail(`entry ${index + 1}.source_row_sha256 does not match row`);
  if (entry.import_id !== expectedImportId) fail(`entry ${index + 1}.import_id does not match row`);
  if (entry.plan_hash !== expectedPlanHash) fail(`entry ${index + 1}.plan_hash does not match row`);
  return { ...entry, normalizedRow, expectedImportId, expectedSourceRowSha256, expectedPlanHash };
}

async function readBody(c: JobsContext): Promise<Record<string, unknown> | null> {
  const declared = Number(c.req.header("content-length"));
  if (Number.isFinite(declared) && (declared < 0 || declared > MAX_BODY_BYTES)) return null;
  const bytes = new Uint8Array(await c.req.raw.arrayBuffer());
  if (bytes.byteLength > MAX_BODY_BYTES) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return null;
  }
  return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : null;
}

function decodeBase64Url(value: string): Uint8Array | null {
  try {
    if (!/^[A-Za-z0-9_-]+$/.test(value)) return null;
    const padded = `${value}${"=".repeat((4 - value.length % 4) % 4)}`;
    const binary = atob(padded.replaceAll("-", "+").replaceAll("_", "/"));
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

function asArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return copy.buffer;
}

async function validPlanSignature(c: JobsContext, payload: string): Promise<boolean> {
  const secret = String(c.env.CHAT_HISTORY_IMPORT_SIGNING_SECRET || "");
  const encoded = c.req.header("x-chat-history-plan-signature") || "";
  if (secret.length < 32 || !encoded) return false;
  const signature = decodeBase64Url(encoded);
  if (!signature) return false;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  return crypto.subtle.verify("HMAC", key, asArrayBuffer(signature), new TextEncoder().encode(payload));
}

function planSignaturePayload(
  batchId: string,
  manifestSha256: string,
  entries: NormalizedEntry[],
): string {
  return stableJson({
    batch_id: batchId,
    manifest_sha256: manifestSha256,
    entries: entries.map((entry) => ({
      uid: entry.uid,
      entity_kind: entry.entity_kind,
      entity_id: entry.entity_id,
      account_generation: entry.account_generation,
      source_fingerprint: entry.source_fingerprint,
      source_export_sha256: entry.source_export_sha256,
      source_row_sha256: entry.expectedSourceRowSha256,
      import_id: entry.expectedImportId,
      plan_hash: entry.expectedPlanHash,
      action: "stage",
      status: "planned",
      last_error: null,
    })),
  });
}

async function apply(c: JobsContext): Promise<Response> {
  const body = await readBody(c);
  if (!body || !Array.isArray(body.entries) || body.entries.length < 1 || body.entries.length > MAX_ENTRIES) {
    return c.json({ error: "invalid_request" }, 422, noStoreHeaders());
  }
  let manifest: string;
  let source: Record<string, unknown>;
  let sourceExport: string;
  let entries: NormalizedEntry[];
  try {
    assertJsonShape(body);
    allowedKeys(
      body,
      new Set([
        "mode", "schema_version", "schemaVersion", "source", "manifest_sha256", "manifestHash",
        "source_export_sha256", "sourceExportSha256", "batch_id", "batchId", "total", "stage", "blocked", "entries",
      ]),
      "plan",
    );
    if (body.mode !== "reviewed-plan") fail("plan mode is invalid");
    const schemaVersion = body.schema_version ?? body.schemaVersion;
    if (requiredInteger(schemaVersion, "schema_version", 1) !== 1) fail("schema_version is invalid");
    if (requiredInteger(body.total, "total", MAX_ENTRIES) !== body.entries.length) fail("plan total is invalid");
    if (requiredInteger(body.stage, "stage", MAX_ENTRIES) !== body.entries.length) fail("plan stage is invalid");
    if (requiredInteger(body.blocked, "blocked", MAX_ENTRIES) !== 0) fail("plan contains blocked entries");
    manifest = requiredHash(body.manifest_sha256 ?? body.manifestHash, "manifest_sha256");
    const sourceObject = body.source;
    source = normalizedSource(
      sourceObject,
      body.source_export_sha256 ?? body.sourceExportSha256,
    );
    sourceExport = String(source.export_sha256);
    entries = await Promise.all(body.entries.map((entry, index) => normalizeEntry(entry, index)));
    const importIds = new Set<string>();
    const entityKeys = new Set<string>();
    for (const entry of entries) {
      const entityKey = `${entry.uid}\0${entry.entity_kind}\0${entry.entity_id}`;
      if (importIds.has(entry.import_id) || entityKeys.has(entityKey)) fail("plan contains duplicate entities");
      importIds.add(entry.import_id);
      entityKeys.add(entityKey);
    }
  } catch {
    return c.json({ error: "invalid_request" }, 422, noStoreHeaders());
  }
  if (entries.some((entry) => entry.source_export_sha256 !== sourceExport)) {
    return c.json({ error: "plan_source_conflict" }, 409, noStoreHeaders());
  }
  const ordered = [...entries].sort((left, right) => `${left.uid}\0${left.entity_kind}\0${left.entity_id}`.localeCompare(`${right.uid}\0${right.entity_kind}\0${right.entity_id}`));
  const expectedManifest = await sha256(stableJson({
    schema_version: 1,
    source,
    entries: ordered.map((entry) => entry.expectedSourceRowSha256),
  }));
  if (manifest !== expectedManifest) return c.json({ error: "manifest_mismatch" }, 409, noStoreHeaders());
  const batchId = await sha256(`${manifest}\0${ordered.map((entry) => entry.import_id).join("\0")}`);
  if (body.batch_id !== batchId && body.batchId !== batchId) return c.json({ error: "batch_id_mismatch" }, 409, noStoreHeaders());
  if (!(await validPlanSignature(c, planSignaturePayload(batchId, manifest, ordered)))) return c.json({ error: "plan_signature_invalid" }, 403, noStoreHeaders());

  const now = Math.floor(Date.now() / 1_000);
  try {
    const receiptPredicates = ordered.map(() => "(uid = ? AND import_id = ?)").join(" OR ");
    const receiptRows = await c.env.APP_DB.prepare(
      `SELECT batch_id, manifest_sha256, uid, import_id, account_generation, source_row_sha256, plan_hash, status FROM cf_chat_history_apply_receipts WHERE ${receiptPredicates}`,
    ).bind(...ordered.flatMap((entry) => [entry.uid, entry.import_id])).all<Record<string, unknown>>();
    const receiptByKey = new Map(
      (receiptRows.results || []).map((receipt) => [`${receipt.uid}\0${receipt.import_id}`, receipt]),
    );
    const alreadyApplied = new Set<number>();
    ordered.forEach((entry, index) => {
      const receipt = receiptByKey.get(`${entry.uid}\0${entry.import_id}`);
      if (!receipt) return;
      if (receipt.batch_id !== batchId || receipt.manifest_sha256 !== manifest || Number(receipt.account_generation) !== entry.account_generation || receipt.source_row_sha256 !== entry.expectedSourceRowSha256 || receipt.plan_hash !== entry.expectedPlanHash || receipt.status !== "applied") {
        throw new Error("chat history apply receipt conflict");
      }
      alreadyApplied.add(index);
    });
    const uids = [...new Set(ordered.map((entry) => entry.uid))];
    const accountRows = await c.env.APP_DB.prepare(
      `SELECT uid, state, checkpoint_phase, destination_backend_bound, account_generation FROM cf_account_cutover WHERE uid IN (${uids.map(() => "?").join(",")})`,
    ).bind(...uids).all<Record<string, unknown>>();
    const accountByUid = new Map((accountRows.results || []).map((row) => [String(row.uid), row]));
    const fenceRows = await c.env.APP_DB.prepare(
      `SELECT uid FROM cf_account_deletion_intents WHERE uid IN (${uids.map(() => "?").join(",")}) UNION SELECT uid FROM cf_account_deletion_tombstones WHERE uid IN (${uids.map(() => "?").join(",")})`,
    ).bind(...uids, ...uids).all<Record<string, unknown>>();
    const fencedUids = new Set((fenceRows.results || []).map((row) => String(row.uid)));
    if (ordered.some((entry, index) => {
      const account = accountByUid.get(entry.uid);
      return fencedUids.has(entry.uid) || !account || account.state !== "new" || account.checkpoint_phase !== "completed" || Number(account.destination_backend_bound) !== 1 || Number(account.account_generation) !== entry.account_generation;
    })) return c.json({ error: "chat_history_authority_changed" }, 409, noStoreHeaders());
    const active = ordered.filter((_entry, index) => !alreadyApplied.has(index));

    const stagedSessions = new Set(
      active
        .filter((entry) => entry.entity_kind === "session")
        .map((entry) => `${entry.uid}\0${entry.entity_id}`),
    );
    const messagesNeedingSession = active.filter((entry) => {
      if (entry.entity_kind !== "message") return false;
      const sessionId = (JSON.parse(String(entry.normalizedRow.message_json)) as Record<string, unknown>).chat_session_id ??
        (JSON.parse(String(entry.normalizedRow.message_json)) as Record<string, unknown>).session_id;
      return !stagedSessions.has(`${entry.uid}\0${String(sessionId)}`);
    });
    const requiredSessions = messagesNeedingSession.map((entry) => {
      const message = JSON.parse(String(entry.normalizedRow.message_json)) as Record<string, unknown>;
      return { uid: entry.uid, id: String(message.chat_session_id ?? message.session_id) };
    });
    const sessionByKey = new Map<string, Record<string, unknown>>();
    if (requiredSessions.length) {
      const sessionPredicates = requiredSessions.map(() => "(uid = ? AND id = ?)").join(" OR ");
      const sessionRows = await c.env.APP_DB.prepare(
        `SELECT uid, id FROM cf_chat_sessions WHERE ${sessionPredicates}`,
      ).bind(...requiredSessions.flatMap((session) => [session.uid, session.id])).all<Record<string, unknown>>();
      for (const row of sessionRows.results || []) sessionByKey.set(`${row.uid}\0${row.id}`, row);
    }
    if (requiredSessions.some((session) => !sessionByKey.has(`${session.uid}\0${session.id}`))) {
      return c.json({ error: "chat_history_session_missing" }, 409, noStoreHeaders());
    }

    const statements = [];
    for (const entry of active.filter((item) => item.entity_kind === "session")) {
      const row = entry.normalizedRow;
      statements.push(c.env.APP_DB.prepare(
        "INSERT INTO cf_chat_history_import_ledger (uid, import_id, entity_kind, entity_id, source_export_sha256, source_row_sha256, account_generation, plan_hash, action, status, last_error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'stage', 'planned', NULL, ?, ?) ON CONFLICT(uid, import_id) DO UPDATE SET status = 'planned', last_error = NULL, updated_at = excluded.updated_at WHERE cf_chat_history_import_ledger.source_row_sha256 = excluded.source_row_sha256 AND cf_chat_history_import_ledger.plan_hash = excluded.plan_hash AND cf_chat_history_import_ledger.status <> 'applied'",
      ).bind(entry.uid, entry.import_id, entry.entity_kind, entry.entity_id, sourceExport, entry.expectedSourceRowSha256, entry.account_generation, entry.expectedPlanHash, now, now));
      statements.push(c.env.APP_DB.prepare(
        "INSERT INTO cf_chat_sessions (uid, id, title, preview, created_at, updated_at, app_id, message_count, starred, history_import_id, history_source_row_sha256, history_account_generation) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ?) ON CONFLICT(uid, id) DO NOTHING",
      ).bind(entry.uid, row.id, row.title, row.preview, row.created_at, row.updated_at, row.app_id, row.message_count, row.starred, entry.import_id, entry.expectedSourceRowSha256, entry.account_generation, entry.uid, entry.uid, now));
      statements.push(c.env.APP_DB.prepare(
        "UPDATE cf_chat_history_import_ledger SET status = CASE WHEN EXISTS (SELECT 1 FROM cf_chat_sessions WHERE uid = ? AND id = ? AND history_import_id = ? AND history_source_row_sha256 = ? AND history_account_generation = ?) THEN 'applied' ELSE 'failed' END, last_error = CASE WHEN EXISTS (SELECT 1 FROM cf_chat_sessions WHERE uid = ? AND id = ? AND history_import_id = ? AND history_source_row_sha256 = ? AND history_account_generation = ?) THEN NULL ELSE 'destination_conflict_or_generation_mismatch' END, updated_at = ? WHERE uid = ? AND import_id = ? AND status = 'planned'",
      ).bind(entry.uid, entry.entity_id, entry.import_id, entry.expectedSourceRowSha256, entry.account_generation, entry.uid, entry.entity_id, entry.import_id, entry.expectedSourceRowSha256, entry.account_generation, now, entry.uid, entry.import_id));
      statements.push(c.env.APP_DB.prepare(
        "INSERT INTO cf_chat_history_apply_receipts (batch_id, manifest_sha256, uid, import_id, entity_kind, entity_id, account_generation, source_row_sha256, plan_hash, status, applied_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?) ON CONFLICT(uid, import_id) DO NOTHING",
      ).bind(batchId, manifest, entry.uid, entry.import_id, entry.entity_kind, entry.entity_id, entry.account_generation, entry.expectedSourceRowSha256, entry.expectedPlanHash, now, now));
    }
    for (const entry of active.filter((item) => item.entity_kind === "message")) {
      const row = entry.normalizedRow;
      statements.push(c.env.APP_DB.prepare(
        "INSERT INTO cf_chat_history_import_ledger (uid, import_id, entity_kind, entity_id, source_export_sha256, source_row_sha256, account_generation, plan_hash, action, status, last_error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'stage', 'planned', NULL, ?, ?) ON CONFLICT(uid, import_id) DO UPDATE SET status = 'planned', last_error = NULL, updated_at = excluded.updated_at WHERE cf_chat_history_import_ledger.source_row_sha256 = excluded.source_row_sha256 AND cf_chat_history_import_ledger.plan_hash = excluded.plan_hash AND cf_chat_history_import_ledger.status <> 'applied'",
      ).bind(entry.uid, entry.import_id, entry.entity_kind, entry.entity_id, sourceExport, entry.expectedSourceRowSha256, entry.account_generation, entry.expectedPlanHash, now, now));
      statements.push(c.env.APP_DB.prepare(
        "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json, history_import_id, history_source_row_sha256, history_account_generation) SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ?) ON CONFLICT(uid, id) DO NOTHING",
      ).bind(entry.uid, row.id, row.app_id, row.created_at, row.message_json, entry.import_id, entry.expectedSourceRowSha256, entry.account_generation, entry.uid, entry.uid, now));
      statements.push(c.env.APP_DB.prepare(
        "UPDATE cf_chat_history_import_ledger SET status = CASE WHEN EXISTS (SELECT 1 FROM cf_chat_messages WHERE uid = ? AND id = ? AND history_import_id = ? AND history_source_row_sha256 = ? AND history_account_generation = ?) THEN 'applied' ELSE 'failed' END, last_error = CASE WHEN EXISTS (SELECT 1 FROM cf_chat_messages WHERE uid = ? AND id = ? AND history_import_id = ? AND history_source_row_sha256 = ? AND history_account_generation = ?) THEN NULL ELSE 'destination_conflict_or_generation_mismatch' END, updated_at = ? WHERE uid = ? AND import_id = ? AND status = 'planned'",
      ).bind(entry.uid, entry.entity_id, entry.import_id, entry.expectedSourceRowSha256, entry.account_generation, entry.uid, entry.entity_id, entry.import_id, entry.expectedSourceRowSha256, entry.account_generation, now, entry.uid, entry.import_id));
      statements.push(c.env.APP_DB.prepare(
        "INSERT INTO cf_chat_history_apply_receipts (batch_id, manifest_sha256, uid, import_id, entity_kind, entity_id, account_generation, source_row_sha256, plan_hash, status, applied_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?) ON CONFLICT(uid, import_id) DO NOTHING",
      ).bind(batchId, manifest, entry.uid, entry.import_id, entry.entity_kind, entry.entity_id, entry.account_generation, entry.expectedSourceRowSha256, entry.expectedPlanHash, now, now));
    }
    if (statements.length) await c.env.APP_DB.batch(statements);
    return c.json({ batch_id: batchId, manifest_sha256: manifest, status: "applied", entry_count: ordered.length, applied_count: active.length, already_applied_count: alreadyApplied.size }, 200, noStoreHeaders());
  } catch (error) {
    if (error instanceof Error && error.message.includes("conflict")) return c.json({ error: "chat_history_apply_conflict" }, 409, noStoreHeaders());
    if (error instanceof Error && (error.message.includes("account deletion fence") || error.message.includes("authority changed"))) return c.json({ error: "chat_history_authority_changed" }, 409, noStoreHeaders());
    return c.json({ error: "chat_history_apply_unavailable" }, 503, noStoreHeaders());
  }
}

export function registerChatHistoryImportRoutes(app: Hono<{ Bindings: JobsEnv }>): void {
  app.post(APPLY_PATH, async (c) => {
    if (c.env.CHAT_HISTORY_IMPORT_STAGING_ENABLED !== "true") {
      return c.json({ error: "chat_history_import_unavailable" }, 503, noStoreHeaders());
    }
    if (!adminAuthorized(c)) return c.json({ error: "forbidden" }, 403, noStoreHeaders());
    try {
      return await apply(c);
    } catch {
      return c.json({ error: "chat_history_apply_unavailable" }, 503, noStoreHeaders());
    }
  });
}

export const chatHistoryImportConstants = Object.freeze({ applyPath: APPLY_PATH, maxEntries: MAX_ENTRIES, maxBodyBytes: MAX_BODY_BYTES });
