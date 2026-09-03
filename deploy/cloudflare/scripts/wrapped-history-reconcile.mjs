#!/usr/bin/env node
// LIFECYCLE: permanent

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

// This planner is deliberately separate from the live Wrapped worker. A
// historical Firestore result is not a new provider generation: it may only
// be imported when the export, source snapshot, and destination account
// generation have all been independently attested.
const MANIFEST_SCHEMA_VERSION = 1;
const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const MAX_ROWS = 5_000;
const SUPPORTED_YEAR = 2025;
const MAX_RESULT_BYTES = 256 * 1024;
const MAX_NESTED_OBJECT_BYTES = 32 * 1024;
const SHA256 = /^[0-9a-f]{64}$/;
const UID = /^[^/\u0000]{1,256}$/;
const SENSITIVE_KEYS = new Set([
  "access_token",
  "api_key",
  "authorization",
  "bearer",
  "client_secret",
  "credential",
  "credentials",
  "custom_token",
  "firebase_id_token",
  "id_token",
  "openai_api_key",
  "password",
  "private_key",
  "refresh_token",
  "secret",
  "secret_key",
  "token",
]);
const REQUIRED_RESULT_OBJECTS = [
  "decision_style",
  "memorable_days",
  "funniest_event",
  "most_embarrassing_event",
  "obsessions",
  "struggle",
  "personal_win",
];
const REQUIRED_RESULT_ARRAYS = [
  "top_phrases",
  "top_buddies",
  "movie_recommendations",
];

function fail(message) {
  throw new Error(`wrapped history reconciliation: ${message}`);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function byteLength(value) {
  return Buffer.byteLength(value, "utf8");
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`)
      .join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) fail("manifest contains an unsupported value");
  return encoded;
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value
    : null;
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

function sourceValue(source) {
  const object = objectValue(source);
  if (!object) fail("source must be an object");
  const sensitive = sensitiveField(object);
  if (sensitive) fail(`source contains sensitive field ${sensitive}`);
  if (object.kind !== "firestore") fail("source.kind must be firestore");
  if (object.collection !== "users/{uid}/wrapped/{year}") {
    fail("source.collection must be users/{uid}/wrapped/{year}");
  }
  const exportSha = object.export_sha256;
  if (
    exportSha !== undefined &&
    (typeof exportSha !== "string" || !SHA256.test(exportSha))
  ) {
    fail("source.export_sha256 must be lowercase SHA-256");
  }
  const exportedAt = object.exported_at;
  if (
    exportedAt !== undefined &&
    (typeof exportedAt !== "string" || byteLength(exportedAt) > 128)
  ) {
    fail("source.exported_at is invalid");
  }
  return {
    kind: "firestore",
    collection: "users/{uid}/wrapped/{year}",
    export_sha256: exportSha ?? null,
    ...(exportedAt === undefined ? {} : { exported_at: exportedAt }),
  };
}

function epochValue(value) {
  if (typeof value === "number" && Number.isSafeInteger(value) && value > 0)
    return value;
  if (typeof value === "string" && /^\d+$/.test(value.trim())) {
    const parsed = Number(value.trim());
    if (Number.isSafeInteger(parsed) && parsed > 0) return parsed;
  }
  if (value !== undefined && value !== null) {
    const parsed = Date.parse(String(value));
    if (!Number.isNaN(parsed)) return Math.floor(parsed / 1_000);
  }
  return null;
}

function parseResult(raw) {
  let result = raw;
  if (typeof result === "string") {
    try {
      result = JSON.parse(result);
    } catch {
      return { error: "result_json_invalid" };
    }
  }
  const object = objectValue(result);
  if (!object) return { error: "result_not_object" };
  const sensitive = sensitiveField(object);
  if (sensitive) return { error: `sensitive_field:${sensitive}` };
  let resultJson;
  try {
    resultJson = stableJson(object);
  } catch {
    return { error: "result_not_serializable" };
  }
  if (byteLength(resultJson) > MAX_RESULT_BYTES)
    return { error: "result_too_large" };
  for (const key of REQUIRED_RESULT_OBJECTS) {
    if (!objectValue(object[key])) return { error: `result_missing_${key}` };
    if (byteLength(stableJson(object[key])) > MAX_NESTED_OBJECT_BYTES)
      return { error: `result_${key}_too_large` };
  }
  for (const key of REQUIRED_RESULT_ARRAYS) {
    if (!Array.isArray(object[key])) return { error: `result_missing_${key}` };
    if (object[key].length > 5) return { error: `result_${key}_too_many` };
  }
  return { resultJson, resultSha256: sha256(resultJson) };
}

function manifestValue(input) {
  const object = objectValue(input);
  if (!object || !Array.isArray(object.rows)) {
    fail("input must be a manifest object with rows");
  }
  if (object.schema_version !== MANIFEST_SCHEMA_VERSION)
    fail(`schema_version must be ${MANIFEST_SCHEMA_VERSION}`);
  const source = sourceValue(object.source);
  return { schema_version: MANIFEST_SCHEMA_VERSION, source, rows: object.rows };
}

function addError(errors, error) {
  if (error && !errors.includes(error)) errors.push(error);
}

function planCandidate(row, source, index) {
  if (!row || typeof row !== "object" || Array.isArray(row))
    fail(`row ${index + 1} must be an object`);
  const errors = [];
  const sensitive = sensitiveField(row);
  if (sensitive) addError(errors, `sensitive_field:${sensitive}`);

  const uid = typeof row.uid === "string" ? row.uid : "";
  if (!UID.test(uid)) addError(errors, "uid_invalid");
  const year = Number(row.year);
  if (!Number.isSafeInteger(year) || year !== SUPPORTED_YEAR)
    addError(errors, "year_unsupported");
  if (!["done", "completed"].includes(row.status))
    addError(errors, "status_not_completed");

  const sourceFingerprint =
    typeof row.source_fingerprint === "string" ? row.source_fingerprint : "";
  if (!SHA256.test(sourceFingerprint))
    addError(errors, "source_fingerprint_missing_or_invalid");
  if (!source.export_sha256) addError(errors, "source_export_checksum_missing");

  const generation = Number(row.account_generation ?? row.generation);
  if (!Number.isSafeInteger(generation) || generation < 0)
    addError(errors, "account_generation_invalid");

  const createdAt = epochValue(row.created_at);
  const updatedAt = epochValue(row.updated_at ?? row.completed_at);
  if (createdAt === null) addError(errors, "created_at_invalid");
  if (updatedAt === null) addError(errors, "updated_at_invalid");

  const parsedResult = parseResult(row.result ?? row.result_json);
  // The row-wide scan already reports a nested result secret. Avoid emitting
  // the same finding twice while still parsing the result for all other
  // structural checks.
  const resultSecretPath = parsedResult.error?.startsWith("sensitive_field:")
    ? parsedResult.error.slice("sensitive_field:".length)
    : null;
  const duplicateResultSecret =
    sensitive &&
    resultSecretPath !== null &&
    sensitive === `result.${resultSecretPath}`;
  if (parsedResult.error && !duplicateResultSecret) {
    addError(errors, parsedResult.error);
  }

  const requestFingerprint = sha256(`wrapped\0${uid}\0${year}`);
  const jobId = `wrapped-history-${year}-${requestFingerprint.slice(0, 40)}`;
  const rowHash = sha256(
    stableJson({
      uid,
      year,
      source_fingerprint: sourceFingerprint,
      account_generation: Number.isSafeInteger(generation) ? generation : null,
      created_at: createdAt,
      updated_at: updatedAt,
      result_sha256: parsedResult.resultSha256 ?? null,
    }),
  );
  return {
    uid,
    year,
    jobId,
    requestFingerprint,
    sourceFingerprint,
    accountGeneration: Number.isSafeInteger(generation) ? generation : null,
    resultJson: parsedResult.resultJson ?? null,
    resultSha256: parsedResult.resultSha256 ?? null,
    createdAt,
    updatedAt,
    sourceRowSha256: rowHash,
    action: errors.length ? "blocked" : "stage",
    status: errors.length ? "blocked" : "planned",
    lastError: errors.length ? errors.join(",") : null,
  };
}

function block(entry, reason) {
  entry.action = "blocked";
  entry.status = "blocked";
  addError(entry._errors, reason);
  entry.lastError = entry._errors.join(",");
}

function sourceRowHash(entry) {
  return sha256(
    stableJson({
      uid: entry.uid,
      year: entry.year,
      source_fingerprint: entry.sourceFingerprint,
      account_generation: Number.isSafeInteger(entry.accountGeneration)
        ? entry.accountGeneration
        : null,
      created_at: entry.createdAt,
      updated_at: entry.updatedAt,
      result_sha256: entry.resultSha256 ?? null,
    }),
  );
}

function manifestHash(plan) {
  return sha256(
    stableJson({
      schema_version: MANIFEST_SCHEMA_VERSION,
      source: plan.source,
      rows: plan.entries.map((entry) => entry.sourceRowSha256),
    }),
  );
}

export function planWrappedHistory(
  input,
  { maxRows = MAX_ROWS, fencedUids = [] } = {},
) {
  const manifest = manifestValue(input);
  if (!Number.isSafeInteger(maxRows) || maxRows < 1 || maxRows > MAX_ROWS)
    fail(`maximum rows must be between 1 and ${MAX_ROWS}`);
  if (manifest.rows.length > maxRows || manifest.rows.length > MAX_ROWS)
    fail(`maximum ${Math.min(maxRows, MAX_ROWS)} rows per run`);
  const fenced = new Set(fencedUids);
  const byKey = new Map();
  for (const [index, row] of manifest.rows.entries()) {
    const entry = planCandidate(row, manifest.source, index);
    entry._errors = entry.lastError ? entry.lastError.split(",") : [];
    const key = `${entry.uid}\0${entry.year}`;
    const prior = byKey.get(key);
    if (prior) {
      if (prior.sourceRowSha256 === entry.sourceRowSha256) continue;
      block(prior, "conflicting_duplicate_row");
      block(entry, "conflicting_duplicate_row");
    }
    if (fenced.has(entry.uid)) block(entry, "account_deletion_fence");
    byKey.set(key, entry);
  }
  const entries = [...byKey.values()]
    .sort((left, right) =>
      `${left.uid}\0${left.year}`.localeCompare(`${right.uid}\0${right.year}`),
    )
    .map(({ _errors, ...entry }) => entry);
  const plan = {
    mode: "dry-run",
    schema_version: MANIFEST_SCHEMA_VERSION,
    source: manifest.source,
    manifest_sha256: null,
    max_rows: maxRows,
    total: entries.length,
    stage: entries.filter((entry) => entry.action === "stage").length,
    blocked: entries.filter((entry) => entry.action === "blocked").length,
    entries,
  };
  plan.manifest_sha256 = manifestHash(plan);
  return plan;
}

function sqlLiteral(value) {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "number") return String(value);
  return `'${String(value).replaceAll("'", "''")}'`;
}

function assertPlan(plan) {
  if (
    !plan ||
    plan.schema_version !== MANIFEST_SCHEMA_VERSION ||
    !SHA256.test(plan.manifest_sha256 || "") ||
    !Array.isArray(plan.entries)
  )
    fail("plan is invalid");
  for (const entry of plan.entries) {
    if (entry.sourceRowSha256 !== sourceRowHash(entry))
      fail("plan row checksum does not match entry");
  }
  if (manifestHash(plan) !== plan.manifest_sha256)
    fail("plan manifest checksum does not match entries");
}

export function renderWrappedHistorySql(
  plan,
  now = Math.floor(Date.now() / 1_000),
) {
  assertPlan(plan);
  if (!Number.isSafeInteger(now) || now < 0) fail("now must be a timestamp");
  const statements = plan.entries
    .filter((entry) => entry.action === "stage")
    .map((entry) => {
      if (
        !SHA256.test(entry.sourceFingerprint) ||
        !SHA256.test(entry.requestFingerprint)
      )
        fail("staged entry fingerprint is invalid");
      if (
        !Number.isSafeInteger(entry.accountGeneration) ||
        entry.accountGeneration < 0
      )
        fail("staged entry account generation is invalid");
      if (
        !Number.isSafeInteger(entry.createdAt) ||
        !Number.isSafeInteger(entry.updatedAt)
      )
        fail("staged entry timestamps are invalid");
      return (
        "INSERT INTO cf_wrapped_jobs (uid, year, job_id, request_fingerprint, source_fingerprint, " +
        "account_generation, status, attempts, next_attempt_at, result_json, last_error, created_at, updated_at) " +
        `SELECT ${sqlLiteral(entry.uid)}, ${entry.year}, ${sqlLiteral(entry.jobId)}, ` +
        `${sqlLiteral(entry.requestFingerprint)}, ${sqlLiteral(entry.sourceFingerprint)}, ` +
        `${entry.accountGeneration}, 'completed', 0, ${now}, ${sqlLiteral(entry.resultJson)}, NULL, ` +
        `${entry.createdAt}, ${entry.updatedAt} ` +
        "WHERE EXISTS (SELECT 1 FROM cf_account_cutover WHERE uid = " +
        `${sqlLiteral(entry.uid)} AND state = 'new' AND checkpoint_phase = 'completed' ` +
        `AND destination_backend_bound = 1 AND account_generation = ${entry.accountGeneration}) ` +
        "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = " +
        `${sqlLiteral(entry.uid)}) ` +
        "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = " +
        `${sqlLiteral(entry.uid)} AND expires_at > ${now}) ` +
        "ON CONFLICT(uid, year) DO NOTHING;"
      );
    });
  return [
    "-- Generated by deploy/cloudflare/scripts/wrapped-history-reconcile.mjs; review before applying.",
    `-- source manifest SHA-256: ${plan.manifest_sha256}`,
    "-- This SQL only imports completed result snapshots; it never queues a provider generation.",
    "-- D1 remote file ingestion supplies the transaction; no BEGIN/COMMIT is emitted.",
    ...statements,
    "",
  ].join("\n");
}

export function renderWrappedHistoryVerifySql(plan) {
  assertPlan(plan);
  const entries = plan.entries.filter((entry) => entry.action === "stage");
  if (!entries.length) return "-- No staged Wrapped rows; nothing to verify.\n";
  const predicates = entries.map(
    (entry) => `(uid = ${sqlLiteral(entry.uid)} AND year = ${entry.year})`,
  );
  return [
    "-- Compare each returned row with the dry-run plan and then run --verify with the JSON export.",
    `-- source manifest SHA-256: ${plan.manifest_sha256}`,
    "SELECT uid, year, job_id, request_fingerprint, source_fingerprint, account_generation, status, length(result_json) AS result_bytes, created_at, updated_at",
    `FROM cf_wrapped_jobs WHERE ${predicates.join(" OR ")} ORDER BY uid, year;`,
    "",
  ].join("\n");
}

function actualRowsValue(input) {
  if (Array.isArray(input)) return input;
  if (objectValue(input) && Array.isArray(input.rows)) return input.rows;
  fail("actual input must be an array or an object with rows");
}

export function verifyWrappedHistory(plan, actualInput) {
  assertPlan(plan);
  const actualRows = actualRowsValue(actualInput);
  const actualByKey = new Map();
  const duplicateActual = [];
  for (const row of actualRows) {
    if (!row || typeof row !== "object") continue;
    const key = `${row.uid}\0${Number(row.year)}`;
    if (actualByKey.has(key)) duplicateActual.push(key);
    actualByKey.set(key, row);
  }
  const missing = [];
  const mismatched = [];
  const fencedPresent = [];
  for (const entry of plan.entries) {
    const key = `${entry.uid}\0${entry.year}`;
    const actual = actualByKey.get(key);
    if (entry.action === "blocked") {
      if (entry.lastError?.includes("account_deletion_fence") && actual)
        fencedPresent.push(key);
      continue;
    }
    if (!actual) {
      missing.push(key);
      continue;
    }
    const actualResult = parseResult(actual.result_json ?? actual.result);
    const reasons = [];
    if (actual.status !== "completed") reasons.push("status");
    if (actual.request_fingerprint !== entry.requestFingerprint)
      reasons.push("request_fingerprint");
    if (actual.source_fingerprint !== entry.sourceFingerprint)
      reasons.push("source_fingerprint");
    if (Number(actual.account_generation) !== entry.accountGeneration)
      reasons.push("account_generation");
    if (actualResult.resultSha256 !== entry.resultSha256)
      reasons.push("result");
    if (reasons.length) mismatched.push({ key, reasons });
  }
  const ok =
    !missing.length &&
    !mismatched.length &&
    !fencedPresent.length &&
    !duplicateActual.length;
  return {
    status: ok ? "passed" : "failed",
    manifest_sha256: plan.manifest_sha256,
    checked: plan.entries.filter((entry) => entry.action === "stage").length,
    blocked: plan.entries.filter((entry) => entry.action === "blocked").length,
    missing,
    mismatched,
    fenced_present: fencedPresent,
    duplicate_actual: duplicateActual,
  };
}

async function readJson(filename) {
  if (!filename || filename.startsWith("--")) fail("--input is required");
  const buffer = await readFile(filename);
  if (buffer.byteLength > MAX_INPUT_BYTES)
    fail(`input exceeds ${MAX_INPUT_BYTES} bytes`);
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer));
  } catch {
    fail("input is not valid JSON");
  }
}

async function main() {
  const args = process.argv.slice(2);
  const inputIndex = args.indexOf("--input");
  const actualIndex = args.indexOf("--actual");
  const input = await readJson(inputIndex >= 0 ? args[inputIndex + 1] : null);
  const fencedUids = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === "--fenced-uid" && args[index + 1])
      fencedUids.push(args[++index]);
  }
  const plan =
    objectValue(input) &&
    input.mode === "dry-run" &&
    Array.isArray(input.entries)
      ? input
      : planWrappedHistory(input, { fencedUids });
  if (args.includes("--verify")) {
    const actual = await readJson(
      actualIndex >= 0 ? args[actualIndex + 1] : null,
    );
    const result = verifyWrappedHistory(plan, actual);
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    if (result.status !== "passed") process.exitCode = 2;
    return;
  }
  process.stdout.write(
    `${JSON.stringify(
      {
        ...plan,
        sql: renderWrappedHistorySql(plan),
        verify_sql: renderWrappedHistoryVerifySql(plan),
      },
      null,
      2,
    )}\n`,
  );
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    process.stderr.write(
      `${error instanceof Error ? error.message : String(error)}\n`,
    );
    process.exitCode = 2;
  });
}
