#!/usr/bin/env node
// LIFECYCLE: permanent

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { normalizeRow } from "./backfill-d1.mjs";

// This planner is intentionally the smallest historical chat slice.  It
// replays only session/message metadata that has already been exported into a
// de-identified manifest.  It does not read Firestore/GCS, call a provider,
// or import files/Assistants state.  The generated SQL is guarded by the D1
// account-generation and deletion fences and never overwrites an occupied
// destination row.
const MANIFEST_SCHEMA_VERSION = 1;
const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const MAX_ENTITIES = 5_000;
const SHA256 = /^[0-9a-f]{64}$/;
const UID = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;
const ENTITY_ID = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;
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
  "mcp_oauth_tokens",
  "openai_api_key",
  "openai_file_id",
  "password",
  "private_key",
  "raw_uid",
  "refresh_token",
  "secret",
  "secret_key",
  "token",
]);

function fail(message) {
  throw new Error(`chat history reconciliation: ${message}`);
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value
    : null;
}

function byteLength(value) {
  return Buffer.byteLength(value, "utf8");
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`)
      .join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) fail("manifest contains an unsupported value");
  return encoded;
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
    const fieldPath = path ? `${path}.${key}` : key;
    if (SENSITIVE_KEYS.has(key.toLowerCase())) return fieldPath;
    const found = sensitiveField(nested, fieldPath);
    if (found) return found;
  }
  return null;
}

function requiredSha(value, field) {
  if (typeof value !== "string" || !SHA256.test(value))
    fail(`${field} must be lowercase SHA-256`);
  return value;
}

function requiredId(value, field, pattern = ENTITY_ID) {
  if (typeof value !== "string" || !pattern.test(value))
    fail(`${field} is invalid`);
  return value;
}

function integerValue(value, field, { minimum = 0, maximum = Number.MAX_SAFE_INTEGER } = {}) {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum)
    fail(`${field} is invalid`);
  return parsed;
}

function sourceValue(value) {
  const source = objectValue(value);
  if (!source) fail("source must be an object");
  const sensitive = sensitiveField(source);
  if (sensitive) fail(`source contains sensitive field ${sensitive}`);
  const allowed = new Set(["kind", "collections", "export_sha256", "exported_at"]);
  const unknown = Object.keys(source).find((key) => !allowed.has(key));
  if (unknown) fail(`source contains unsupported field ${unknown}`);
  if (source.kind !== "firestore") fail("source.kind must be firestore");
  if (
    !Array.isArray(source.collections) ||
    source.collections.length !== 2 ||
    new Set(source.collections).size !== 2 ||
    !source.collections.includes("users/{uid}/chat_sessions") ||
    !source.collections.includes("users/{uid}/messages")
  ) {
    fail("source.collections must contain the chat_sessions and messages collections");
  }
  const exportSha256 = requiredSha(source.export_sha256, "source.export_sha256");
  if (source.exported_at !== undefined) {
    if (typeof source.exported_at !== "string" || byteLength(source.exported_at) > 128)
      fail("source.exported_at is invalid");
  }
  return {
    kind: "firestore",
    collections: ["users/{uid}/chat_sessions", "users/{uid}/messages"],
    export_sha256: exportSha256,
    ...(source.exported_at === undefined ? {} : { exported_at: source.exported_at }),
  };
}

function accountValue(value, index) {
  const account = objectValue(value);
  if (!account) fail(`accounts[${index + 1}] must be an object`);
  const sensitive = sensitiveField(account);
  if (sensitive) fail(`accounts[${index + 1}] contains sensitive field ${sensitive}`);
  const allowed = new Set(["uid", "account_generation", "source_fingerprint"]);
  const unknown = Object.keys(account).find((key) => !allowed.has(key));
  if (unknown) fail(`accounts[${index + 1}] contains unsupported field ${unknown}`);
  const uid = requiredId(account.uid, `accounts[${index + 1}].uid`, UID);
  const accountGeneration = integerValue(
    account.account_generation,
    `accounts[${index + 1}].account_generation`,
  );
  const sourceFingerprint = requiredSha(
    account.source_fingerprint,
    `accounts[${index + 1}].source_fingerprint`,
  );
  return { uid, accountGeneration, sourceFingerprint };
}

function parseMessageObject(value, field) {
  let parsed = value;
  if (typeof value === "string") {
    try {
      parsed = JSON.parse(value);
    } catch {
      fail(`${field} must contain valid JSON`);
    }
  }
  const sensitive = sensitiveField(parsed);
  if (sensitive) fail(`${field} contains sensitive field ${sensitive}`);
  return parsed;
}

function messageFileReferences(message) {
  if (message.files !== undefined || message.attachments !== undefined) return null;
  const files = message.files_id ?? message.file_ids;
  if (files === undefined || files === null) return [];
  if (!Array.isArray(files) || files.length > 20 || files.some((value) => typeof value !== "string" || !ENTITY_ID.test(value)))
    return null;
  return [...new Set(files)];
}

function normalizeEntity(kind, raw, account, sourceExportSha256, index) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw))
    fail(`${kind}[${index + 1}] must be an object`);
  const sensitive = sensitiveField(raw);
  if (sensitive) fail(`${kind}[${index + 1}] contains sensitive field ${sensitive}`);
  const row = { ...raw };
  row.uid = row.uid ?? account.uid;
  row.account_generation = row.account_generation ?? account.accountGeneration;
  if (row.uid !== account.uid) fail(`${kind}[${index + 1}] uid does not match account`);
  if (integerValue(row.account_generation, `${kind}[${index + 1}].account_generation`) !== account.accountGeneration)
    fail(`${kind}[${index + 1}] account_generation does not match account`);

  const table = kind === "sessions" ? "cf_chat_sessions" : "cf_chat_messages";
  const normalized = normalizeRow(table, row);
  const entityId = normalized.id;
  const parsedMessage = kind === "messages" ? parseMessageObject(normalized.message_json, `${kind}[${index + 1}].message_json`) : null;
  const fileIds = parsedMessage ? messageFileReferences(parsedMessage) : [];
  const errors = [];
  if (kind === "messages") {
    if (fileIds === null) errors.push("file_reference_invalid");
    else if (fileIds.length) errors.push("file_reference_requires_verified_file_rows");
    const sessionId = parsedMessage?.chat_session_id ?? parsedMessage?.session_id;
    if (typeof sessionId !== "string" || !ENTITY_ID.test(sessionId))
      errors.push("session_id_missing");
    normalized.message_json = JSON.stringify(parsedMessage);
  }
  const sourceRow = {
    kind: kind === "sessions" ? "session" : "message",
    source_export_sha256: sourceExportSha256,
    row: normalized,
  };
  const sourceRowSha256 = sha256(stableJson(sourceRow));
  const importId = sha256(
    `${account.uid}\0${sourceRow.kind}\0${entityId}\0${account.sourceFingerprint}\0${sourceRowSha256}`,
  );
  return {
    uid: account.uid,
    entityKind: sourceRow.kind,
    entityId,
    accountGeneration: account.accountGeneration,
    sourceFingerprint: account.sourceFingerprint,
    sourceRowSha256,
    importId,
    row: normalized,
    fileIds: fileIds || [],
    action: errors.length ? "blocked" : "stage",
    status: errors.length ? "blocked" : "planned",
    lastError: errors.length ? errors.join(",") : null,
  };
}

function block(entry, reason) {
  entry.action = "blocked";
  entry.status = "blocked";
  entry.lastError = entry.lastError ? `${entry.lastError},${reason}` : reason;
}

function planHash(entry) {
  return sha256(
    stableJson({
      uid: entry.uid,
      entity_kind: entry.entityKind,
      entity_id: entry.entityId,
      account_generation: entry.accountGeneration,
      source_fingerprint: entry.sourceFingerprint,
      source_row_sha256: entry.sourceRowSha256,
      import_id: entry.importId,
      action: entry.action,
      last_error: entry.lastError,
    }),
  );
}

export function planChatHistoryReconciliation(
  manifest,
  { maxEntities = MAX_ENTITIES, fencedUids = [] } = {},
) {
  const input = objectValue(manifest);
  if (!input) fail("manifest must be an object");
  if (input.schema_version !== MANIFEST_SCHEMA_VERSION)
    fail(`schema_version must be ${MANIFEST_SCHEMA_VERSION}`);
  const source = sourceValue(input.source);
  if (!Array.isArray(input.accounts) || input.accounts.length === 0)
    fail("accounts must be a non-empty array");
  const accounts = input.accounts.map(accountValue);
  if (new Set(accounts.map((account) => account.uid)).size !== accounts.length)
    fail("accounts contain duplicate uid");
  const accountByUid = new Map(accounts.map((account) => [account.uid, account]));
  const sessions = Array.isArray(input.sessions) ? input.sessions : null;
  const messages = Array.isArray(input.messages) ? input.messages : null;
  if (!sessions || !messages) fail("sessions and messages must be arrays");
  if (sessions.length + messages.length > Math.min(maxEntities, MAX_ENTITIES))
    fail(`maximum ${Math.min(maxEntities, MAX_ENTITIES)} entities per run`);
  const fenced = new Set(fencedUids);
  const entries = [];
  for (const [kind, rows] of [["sessions", sessions], ["messages", messages]]) {
    rows.forEach((raw, index) => {
      const uid = raw && typeof raw === "object" ? raw.uid : null;
      const account = accountByUid.get(uid);
      if (!account) {
        fail(`${kind}[${index + 1}] uid is not listed in accounts`);
      }
      const entry = normalizeEntity(kind, raw, account, source.export_sha256, index);
      if (fenced.has(entry.uid)) block(entry, "account_deletion_fence");
      entries.push(entry);
    });
  }

  const byEntity = new Map();
  for (const entry of entries) {
    const key = `${entry.uid}\0${entry.entityKind}\0${entry.entityId}`;
    const prior = byEntity.get(key);
    if (prior && prior.sourceRowSha256 !== entry.sourceRowSha256) {
      block(prior, "conflicting_duplicate_plan");
      block(entry, "conflicting_duplicate_plan");
    } else if (prior) {
      block(entry, "duplicate_entity");
    } else {
      byEntity.set(key, entry);
    }
  }
  const uniqueEntries = [...byEntity.values()];
  const sessionMap = new Map(
    uniqueEntries
      .filter((entry) => entry.entityKind === "session")
      .map((entry) => [`${entry.uid}\0${entry.entityId}`, entry]),
  );
  const messageCounts = new Map();
  for (const entry of uniqueEntries.filter((item) => item.entityKind === "message")) {
    const parsed = JSON.parse(entry.row.message_json);
    const sessionId = parsed.chat_session_id ?? parsed.session_id;
    const session = sessionMap.get(`${entry.uid}\0${sessionId}`);
    if (!session) {
      block(entry, "session_not_in_manifest");
      continue;
    }
    if (session.action === "blocked") block(entry, "session_blocked");
    if (session.row.app_id && entry.row.app_id && session.row.app_id !== entry.row.app_id)
      block(entry, "app_id_mismatch");
    const countKey = `${entry.uid}\0${session.entityId}`;
    messageCounts.set(countKey, (messageCounts.get(countKey) || 0) + 1);
    if (entry.action === "blocked") block(session, "message_blocked");
  }
  for (const session of uniqueEntries.filter((entry) => entry.entityKind === "session")) {
    const count = messageCounts.get(`${session.uid}\0${session.entityId}`) || 0;
    if (session.row.message_count !== count) block(session, "message_count_mismatch");
  }
  for (const entry of uniqueEntries) entry.planHash = planHash(entry);
  uniqueEntries.sort((left, right) =>
    `${left.uid}\0${left.entityKind}\0${left.entityId}`.localeCompare(
      `${right.uid}\0${right.entityKind}\0${right.entityId}`,
    ),
  );
  const manifestHash = sha256(
    stableJson({
      schema_version: MANIFEST_SCHEMA_VERSION,
      source,
      entries: uniqueEntries.map((entry) => entry.sourceRowSha256),
    }),
  );
  return {
    mode: "reviewed-plan",
    schemaVersion: MANIFEST_SCHEMA_VERSION,
    source,
    manifestHash,
    total: uniqueEntries.length,
    stage: uniqueEntries.filter((entry) => entry.action === "stage").length,
    blocked: uniqueEntries.filter((entry) => entry.action === "blocked").length,
    entries: uniqueEntries.map((entry) => ({
      uid: entry.uid,
      entityKind: entry.entityKind,
      entityId: entry.entityId,
      accountGeneration: entry.accountGeneration,
      sourceFingerprint: entry.sourceFingerprint,
      sourceExportSha256: source.export_sha256,
      sourceRowSha256: entry.sourceRowSha256,
      importId: entry.importId,
      row: entry.row,
      fileIds: entry.fileIds,
      action: entry.action,
      status: entry.status,
      lastError: entry.lastError,
      planHash: entry.planHash,
    })),
  };
}

function sqlLiteral(value) {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "number") return String(value);
  return `'${String(value).replaceAll("'", "''")}'`;
}

function fenceSql(entry) {
  return (
    `EXISTS (SELECT 1 FROM cf_account_cutover c WHERE c.uid = ${sqlLiteral(entry.uid)} ` +
    `AND c.account_generation = ${entry.accountGeneration} AND c.destination_backend_bound = 1 ` +
    `AND c.state = 'new' AND c.checkpoint_phase = 'completed') ` +
    `AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents d WHERE d.uid = ${sqlLiteral(entry.uid)}) ` +
    `AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones t WHERE t.uid = ${sqlLiteral(entry.uid)})`
  );
}

function ledgerInsert(entry, now) {
  const values = [
    entry.uid,
    entry.importId,
    entry.entityKind,
    entry.entityId,
    entry.sourceExportSha256,
    entry.sourceRowSha256,
    entry.accountGeneration,
    entry.planHash,
    "stage",
    "planned",
    null,
    now,
    now,
  ];
  return (
    `INSERT INTO cf_chat_history_import_ledger ` +
    `(uid, import_id, entity_kind, entity_id, source_export_sha256, source_row_sha256, account_generation, plan_hash, action, status, last_error, created_at, updated_at) ` +
    `SELECT ${values.map(sqlLiteral).join(", ")} WHERE ${fenceSql(entry)} ` +
    `ON CONFLICT(uid, import_id) DO UPDATE SET ` +
    `status = CASE WHEN cf_chat_history_import_ledger.source_row_sha256 = excluded.source_row_sha256 ` +
    `AND cf_chat_history_import_ledger.status <> 'applied' THEN 'planned' ELSE cf_chat_history_import_ledger.status END, ` +
    `last_error = CASE WHEN cf_chat_history_import_ledger.source_row_sha256 = excluded.source_row_sha256 ` +
    `AND cf_chat_history_import_ledger.status <> 'applied' THEN NULL ELSE cf_chat_history_import_ledger.last_error END, ` +
    `updated_at = excluded.updated_at;`
  );
}

function destinationInsert(entry) {
  const row = entry.row;
  if (entry.entityKind === "session") {
    const columns = [
      "uid", "id", "title", "preview", "created_at", "updated_at", "app_id", "message_count", "starred",
      "history_import_id", "history_source_row_sha256", "history_account_generation",
    ];
    const values = [
      row.uid, row.id, row.title, row.preview, row.created_at, row.updated_at, row.app_id,
      row.message_count, row.starred, entry.importId, entry.sourceRowSha256, entry.accountGeneration,
    ];
    return `INSERT INTO cf_chat_sessions (${columns.join(", ")}) SELECT ${values.map(sqlLiteral).join(", ")} WHERE ${fenceSql(entry)} ` +
      `AND EXISTS (SELECT 1 FROM cf_chat_history_import_ledger l WHERE l.uid = ${sqlLiteral(entry.uid)} AND l.import_id = ${sqlLiteral(entry.importId)} AND l.status = 'planned') ` +
      `ON CONFLICT(uid, id) DO NOTHING;`;
  }
  const columns = ["uid", "id", "app_id", "created_at", "message_json", "history_import_id", "history_source_row_sha256", "history_account_generation"];
  const values = [
    row.uid, row.id, row.app_id, row.created_at, row.message_json, entry.importId,
    entry.sourceRowSha256, entry.accountGeneration,
  ];
  return `INSERT INTO cf_chat_messages (${columns.join(", ")}) SELECT ${values.map(sqlLiteral).join(", ")} WHERE ${fenceSql(entry)} ` +
    `AND EXISTS (SELECT 1 FROM cf_chat_history_import_ledger l WHERE l.uid = ${sqlLiteral(entry.uid)} AND l.import_id = ${sqlLiteral(entry.importId)} AND l.status = 'planned') ` +
    `ON CONFLICT(uid, id) DO NOTHING;`;
}

function ledgerFinalize(entry) {
  const table = entry.entityKind === "session" ? "cf_chat_sessions" : "cf_chat_messages";
  return (
    `UPDATE cf_chat_history_import_ledger SET status = CASE WHEN EXISTS (` +
    `SELECT 1 FROM ${table} d WHERE d.uid = ${sqlLiteral(entry.uid)} AND d.id = ${sqlLiteral(entry.entityId)} ` +
    `AND d.history_import_id = ${sqlLiteral(entry.importId)} AND d.history_source_row_sha256 = ${sqlLiteral(entry.sourceRowSha256)} ` +
    `AND d.history_account_generation = ${entry.accountGeneration}) THEN 'applied' ELSE 'failed' END, ` +
    `last_error = CASE WHEN EXISTS (` +
    `SELECT 1 FROM ${table} d WHERE d.uid = ${sqlLiteral(entry.uid)} AND d.id = ${sqlLiteral(entry.entityId)} ` +
    `AND d.history_import_id = ${sqlLiteral(entry.importId)} AND d.history_source_row_sha256 = ${sqlLiteral(entry.sourceRowSha256)} ` +
    `AND d.history_account_generation = ${entry.accountGeneration}) THEN NULL ELSE 'destination_conflict_or_generation_mismatch' END ` +
    `WHERE uid = ${sqlLiteral(entry.uid)} AND import_id = ${sqlLiteral(entry.importId)} AND status = 'planned';`
  );
}

export function renderChatHistoryApplySql(plan, now = Math.floor(Date.now() / 1_000)) {
  if (!plan || !Array.isArray(plan.entries)) fail("plan is invalid");
  const staged = plan.entries.filter((entry) => entry.action === "stage");
  const sessions = staged.filter((entry) => entry.entityKind === "session");
  const messages = staged.filter((entry) => entry.entityKind === "message");
  return [
    "-- Generated by deploy/cloudflare/scripts/chat-history-reconcile.mjs; review before applying.",
    "-- This SQL never overwrites an existing session/message row.",
    "-- D1 remote file ingestion supplies the transaction; do not add BEGIN/COMMIT.",
    "PRAGMA foreign_keys = ON;",
    `-- source export: ${plan.source.export_sha256}`,
    `-- manifest hash: ${plan.manifestHash}`,
    ...[...sessions, ...messages].flatMap((entry) => [
      `-- ${entry.entityKind} ${entry.uid}/${entry.entityId} generation=${entry.accountGeneration}`,
      ledgerInsert(entry, now),
      destinationInsert(entry),
      ledgerFinalize(entry),
    ]),
    "",
  ].join("\n");
}

export function renderChatHistoryVerifySql(plan) {
  if (!plan || !Array.isArray(plan.entries)) fail("plan is invalid");
  const staged = plan.entries.filter((entry) => entry.action === "stage");
  if (!staged.length) {
    return "SELECT 'no_staged_chat_history_rows' AS status;\n";
  }
  const values = staged.map((entry) =>
    `SELECT ${sqlLiteral(entry.uid)} AS uid, ${sqlLiteral(entry.entityKind)} AS entity_kind, ${sqlLiteral(entry.entityId)} AS entity_id, ` +
    `${sqlLiteral(entry.importId)} AS import_id, ${sqlLiteral(entry.sourceRowSha256)} AS source_row_sha256, ${entry.accountGeneration} AS account_generation`,
  );
  return [
    "-- Generated verification query; a zero-row result is required.",
    "WITH expected(uid, entity_kind, entity_id, import_id, source_row_sha256, account_generation) AS (",
    values.join(" UNION ALL\n"),
    "), missing_or_conflicting AS (",
    "SELECT e.uid, e.entity_kind, e.entity_id, 'ledger' AS problem FROM expected e",
    "LEFT JOIN cf_chat_history_import_ledger l ON l.uid = e.uid AND l.import_id = e.import_id",
    "WHERE l.import_id IS NULL OR l.status <> 'applied' OR l.source_row_sha256 <> e.source_row_sha256 OR l.account_generation <> e.account_generation",
    "UNION ALL",
    "SELECT e.uid, e.entity_kind, e.entity_id, 'destination' AS problem FROM expected e",
    "LEFT JOIN cf_chat_sessions s ON e.entity_kind = 'session' AND s.uid = e.uid AND s.id = e.entity_id AND s.history_import_id = e.import_id AND s.history_source_row_sha256 = e.source_row_sha256 AND s.history_account_generation = e.account_generation",
    "LEFT JOIN cf_chat_messages m ON e.entity_kind = 'message' AND m.uid = e.uid AND m.id = e.entity_id AND m.history_import_id = e.import_id AND m.history_source_row_sha256 = e.source_row_sha256 AND m.history_account_generation = e.account_generation",
    "WHERE (e.entity_kind = 'session' AND s.id IS NULL) OR (e.entity_kind = 'message' AND m.id IS NULL)",
    ")",
    "SELECT * FROM missing_or_conflicting ORDER BY uid, entity_kind, entity_id;",
    "",
  ].join("\n");
}

async function readManifest(filename) {
  const raw = await readFile(filename);
  if (raw.byteLength > MAX_INPUT_BYTES) fail(`input exceeds ${MAX_INPUT_BYTES} bytes`);
  let parsed;
  try {
    parsed = JSON.parse(raw.toString("utf8"));
  } catch {
    fail("input is not valid JSON");
  }
  return parsed;
}

async function main() {
  const args = process.argv.slice(2);
  const inputIndex = args.indexOf("--input");
  const filename = inputIndex >= 0 ? args[inputIndex + 1] : null;
  if (!filename || filename.startsWith("--")) {
    process.stderr.write("usage: node chat-history-reconcile.mjs --input manifest.json [--fenced-uid uid]\n");
    process.exitCode = 2;
    return;
  }
  const fencedUids = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === "--fenced-uid" && args[index + 1]) fencedUids.push(args[++index]);
  }
  const plan = planChatHistoryReconciliation(await readManifest(filename), { fencedUids });
  process.stdout.write(`${JSON.stringify({
    ...plan,
    apply_sql: renderChatHistoryApplySql(plan),
    verify_sql: renderChatHistoryVerifySql(plan),
  }, null, 2)}\n`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}
