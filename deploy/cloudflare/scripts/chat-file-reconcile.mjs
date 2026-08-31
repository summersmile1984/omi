import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { normalizeRow } from "./backfill-d1.mjs";

const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const MAX_ROWS = 5_000;
const MAX_FILE_BYTES = 50 * 1024 * 1024;
const MAX_SOURCE_URI_BYTES = 1_024;
const MAX_GENERATION_BYTES = 256;
const SHA256 = /^[0-9a-f]{64}$/;
const PROVIDER_FILE_ID = /^file-[A-Za-z0-9_-]{1,256}$/;
const MIME_TYPE = /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/;

function fail(message) {
  throw new Error(`chat-file reconciliation: ${message}`);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function text(value, field, maxBytes) {
  if (typeof value !== "string" || value.length === 0) fail(`${field} is required`);
  if (Buffer.byteLength(value, "utf8") > maxBytes) fail(`${field} is too large`);
  return value;
}

function uidValue(row) {
  const uid = row.uid ?? row.user_id;
  if (typeof uid !== "string" || uid.length === 0 || uid.length > 256 || /[\\/\0]/.test(uid))
    fail("uid is invalid");
  return uid;
}

function fileIdValue(row) {
  const value = row.source_file_id ?? row.legacy_file_id ?? row.file_id ?? row.id;
  const fileId = text(value, "source_file_id", 128);
  if (/[\\/\0]/.test(fileId)) fail("source_file_id is invalid");
  return fileId;
}

function sourceUriValue(row) {
  const value = row.source_object_uri ?? row.gcs_uri ?? row.gcs_path;
  const uri = text(value, "source_object_uri", MAX_SOURCE_URI_BYTES);
  let parsed;
  try {
    parsed = new URL(uri);
  } catch {
    fail("source_object_uri must be a valid gs:// URI");
  }
  if (parsed.protocol !== "gs:" || !parsed.hostname || parsed.username || parsed.password || parsed.search || parsed.hash)
    fail("source_object_uri must be a credential-free gs:// URI");
  const segments = parsed.pathname.split("/").filter(Boolean);
  if (!segments.length || segments.some((segment) => segment === "." || segment === ".." || /[\0\u0000-\u001f\u007f]/.test(segment)))
    fail("source_object_uri path is invalid");
  return uri;
}

function checksumValue(row) {
  const checksum = row.checksum_sha256 ?? row.sha256;
  if (typeof checksum !== "string" || !SHA256.test(checksum.toLowerCase()))
    return null;
  return checksum.toLowerCase();
}

function providerIdValue(row) {
  const value = row.provider_file_id ?? row.openai_file_id;
  if (value === undefined || value === null || value === "") return null;
  return typeof value === "string" && PROVIDER_FILE_ID.test(value) ? value : "invalid";
}

function epochValue(row, field, fallback) {
  const value = row[field] ?? fallback;
  if (typeof value === "number" && Number.isSafeInteger(value) && value > 0) return value;
  if (typeof value === "string" && /^\d+$/.test(value.trim())) {
    const parsed = Number(value.trim());
    if (Number.isSafeInteger(parsed) && parsed > 0) return parsed;
  }
  const parsed = Date.parse(String(value));
  if (!Number.isNaN(parsed)) return Math.floor(parsed / 1_000);
  fail(`${field} must be epoch seconds or ISO timestamp`);
}

function planCandidate(row, now) {
  if (!row || typeof row !== "object" || Array.isArray(row)) fail("each row must be an object");
  const uid = uidValue(row);
  const sourceFileId = fileIdValue(row);
  const sourceObjectUri = sourceUriValue(row);
  const checksum = checksumValue(row);
  const providerFileId = providerIdValue(row);
  const name = text(row.name ?? row.filename ?? "upload", "name", 512).split(/[\\/]/).pop()?.trim() || "upload";
  const mimeType = String(row.mime_type ?? row.content_type ?? "application/octet-stream").trim().toLowerCase();
  const size = Number(row.size);
  const createdAt = epochValue(row, "created_at", now);
  const updatedAt = epochValue(row, "updated_at", createdAt);
  const storageKey = `${uid}/${sourceFileId}`;
  const requestFingerprint = checksum
    ? sha256(`${uid}\0${name}\0${mimeType}\0${checksum}`)
    : null;
  const importId = checksum
    ? sha256(`${uid}\0${sourceFileId}\0${checksum}`)
    : sha256(`${uid}\0${sourceFileId}\0missing-checksum`);
  const errors = [];
  if (!checksum) errors.push("checksum_missing");
  if (providerFileId === "invalid") errors.push("provider_id_invalid");
  if (!providerFileId) errors.push("provider_id_missing");
  if (!Number.isSafeInteger(size) || size <= 0 || size > MAX_FILE_BYTES) errors.push("size_invalid");
  if (name.length === 0 || name.length > 512 || name.includes("\0")) errors.push("name_invalid");
  if (mimeType.length === 0 || mimeType.length > 200 || !MIME_TYPE.test(mimeType)) errors.push("mime_type_invalid");
  if (/[\0]/.test(storageKey) || storageKey.length > 512) errors.push("storage_key_invalid");
  const action = errors.length ? "blocked" : "stage";
  const status = errors.length ? "blocked" : "planned";
  const planHash = sha256(JSON.stringify({
    uid,
    sourceFileId,
    sourceObjectUri,
    checksum,
    providerFileId: providerFileId === "invalid" ? null : providerFileId,
    name,
    mimeType,
    size: Number.isSafeInteger(size) ? size : null,
    storageKey,
    action,
    errors,
  }));
  if (!errors.length) {
    try {
      normalizeRow("cf_chat_file_import_ledger", {
        uid,
        import_id: importId,
        source_file_id: sourceFileId,
        source_object_uri: sourceObjectUri,
        source_generation: row.source_generation == null ? null : text(row.source_generation, "source_generation", MAX_GENERATION_BYTES),
        checksum_sha256: checksum,
        provider_file_id: providerFileId,
        name,
        mime_type: mimeType,
        size,
        desired_storage_key: storageKey,
        plan_hash: planHash,
        action,
        status,
        last_error: null,
        created_at: createdAt,
        updated_at: updatedAt,
      });
    } catch {
      errors.push("ledger_metadata_invalid");
    }
  }
  return {
    uid,
    importId,
    sourceFileId,
    sourceObjectUri,
    sourceGeneration: row.source_generation == null ? null : text(row.source_generation, "source_generation", MAX_GENERATION_BYTES),
    checksum,
    providerFileId: providerFileId === "invalid" ? null : providerFileId,
    name,
    mimeType,
    size: Number.isSafeInteger(size) && size > 0 && size <= MAX_FILE_BYTES ? size : null,
    storageKey,
    requestFingerprint,
    createdAt,
    updatedAt,
    action,
    status,
    lastError: errors.length ? errors.join(",") : null,
    planHash,
  };
}

function samePlan(left, right) {
  return left.planHash === right.planHash;
}

function blockConflict(entry, reason) {
  entry.action = "blocked";
  entry.status = "blocked";
  entry.providerFileId = null;
  entry.lastError = reason;
  entry.planHash = sha256(JSON.stringify(entry));
}

export function planChatFileReconciliation(records, { maxRows = MAX_ROWS, now = Math.floor(Date.now() / 1_000), fencedUids = [] } = {}) {
  if (!Array.isArray(records)) fail("input must be an array");
  if (records.length > maxRows || records.length > MAX_ROWS) fail(`maximum ${Math.min(maxRows, MAX_ROWS)} rows per run`);
  const fenced = new Set(fencedUids);
  const byImportId = new Map();
  for (const record of records) {
    const candidate = planCandidate(record, now);
    if (fenced.has(candidate.uid)) {
      candidate.action = "blocked";
      candidate.status = "blocked";
      candidate.lastError = "account_deletion_fence";
      candidate.providerFileId = null;
      candidate.planHash = sha256(JSON.stringify(candidate));
    }
    const prior = byImportId.get(`${candidate.uid}\0${candidate.importId}`);
    if (!prior) {
      byImportId.set(`${candidate.uid}\0${candidate.importId}`, candidate);
    } else if (!samePlan(prior, candidate)) {
      prior.action = "blocked";
      prior.status = "blocked";
      prior.providerFileId = null;
      prior.lastError = "conflicting_duplicate_plan";
      prior.planHash = sha256(JSON.stringify(prior));
    }
  }
  const entries = [...byImportId.values()];
  const uniqueClaims = new Map();
  for (const entry of entries) {
    if (entry.action !== "stage") continue;
    for (const [claimType, claimValue] of [
      ["provider", entry.providerFileId],
      ["storage", entry.storageKey],
    ]) {
      const claimKey =
        claimType === "provider"
          ? `${claimType}\0${claimValue}`
          : `${claimType}\0${entry.uid}\0${claimValue}`;
      const prior = uniqueClaims.get(claimKey);
      if (prior && prior !== entry) {
        blockConflict(prior, `conflicting_${claimType}_claim`);
        blockConflict(entry, `conflicting_${claimType}_claim`);
      } else {
        uniqueClaims.set(claimKey, entry);
      }
    }
  }
  entries.sort((left, right) =>
    `${left.uid}\0${left.importId}`.localeCompare(`${right.uid}\0${right.importId}`),
  );
  return {
    mode: "dry-run",
    maxRows: Math.min(maxRows, MAX_ROWS),
    total: entries.length,
    stage: entries.filter((entry) => entry.action === "stage").length,
    blocked: entries.filter((entry) => entry.action === "blocked").length,
    entries,
  };
}

function sqlLiteral(value) {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "number") return String(value);
  return `'${String(value).replaceAll("'", "''")}'`;
}

export function renderChatFileLedgerSql(plan, now = Math.floor(Date.now() / 1_000)) {
  if (!plan || !Array.isArray(plan.entries)) fail("plan is invalid");
  return plan.entries.map((entry) => {
    const columns = [
      "uid", "import_id", "source_file_id", "source_object_uri", "source_generation",
      "checksum_sha256", "provider_file_id", "name", "mime_type", "size",
      "desired_storage_key", "plan_hash", "action", "status", "last_error", "created_at", "updated_at",
    ];
    const values = [
      entry.uid, entry.importId, entry.sourceFileId, entry.sourceObjectUri, entry.sourceGeneration,
      entry.checksum, entry.providerFileId, entry.name, entry.mimeType, entry.size,
      entry.storageKey, entry.planHash, entry.action, entry.status, entry.lastError, entry.createdAt, now,
    ];
    return `INSERT INTO cf_chat_file_import_ledger (${columns.join(", ")}) VALUES (${values.map(sqlLiteral).join(", ")}) ` +
      "ON CONFLICT(uid, import_id) DO UPDATE SET " +
      "status = CASE WHEN cf_chat_file_import_ledger.plan_hash = excluded.plan_hash " +
      "THEN cf_chat_file_import_ledger.status ELSE 'blocked' END, " +
      "last_error = CASE WHEN cf_chat_file_import_ledger.plan_hash = excluded.plan_hash " +
      "THEN excluded.last_error ELSE 'conflicting_duplicate_plan' END, " +
      "updated_at = excluded.updated_at;";
  }).join("\n");
}

export function renderChatFileR2Plan(plan) {
  if (!plan || !Array.isArray(plan.entries)) fail("plan is invalid");
  return plan.entries
    .filter((entry) => entry.action === "stage")
    .map((entry) => ({
      source_object_uri: entry.sourceObjectUri,
      destination_key: entry.storageKey,
      checksum_sha256: entry.checksum,
      size: entry.size,
      provider_file_id: entry.providerFileId,
      status: "not_started",
    }));
}

async function readInput(filename) {
  const buffer = await readFile(filename);
  if (buffer.byteLength > MAX_INPUT_BYTES) fail(`input exceeds ${MAX_INPUT_BYTES} bytes`);
  let parsed;
  try {
    parsed = JSON.parse(buffer.toString("utf8"));
  } catch {
    fail("input is not valid JSON");
  }
  if (Array.isArray(parsed)) return parsed;
  if (parsed && typeof parsed === "object" && Array.isArray(parsed.files)) return parsed.files;
  if (parsed && typeof parsed === "object" && Array.isArray(parsed.rows)) return parsed.rows;
  fail("input must be an array or an object with files/rows");
}

async function main() {
  const args = process.argv.slice(2);
  const inputIndex = args.indexOf("--input");
  const filename = inputIndex >= 0 ? args[inputIndex + 1] : null;
  if (!filename || filename.startsWith("--")) {
    process.stderr.write("usage: node chat-file-reconcile.mjs --input export.json [--fenced-uid uid]\n");
    process.exitCode = 2;
    return;
  }
  const fencedUids = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === "--fenced-uid" && args[index + 1]) fencedUids.push(args[++index]);
  }
  const plan = planChatFileReconciliation(await readInput(filename), { fencedUids });
  process.stdout.write(`${JSON.stringify({
    ...plan,
    ledger_sql: renderChatFileLedgerSql(plan),
    r2_copy_plan: renderChatFileR2Plan(plan),
  }, null, 2)}\n`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}
