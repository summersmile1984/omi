import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

// This command deliberately plans historical GCS chunk migration only.  It
// does not contact GCS/R2, copy bytes, or mutate D1.  A separate executor can
// consume a reviewed plan after source generation and checksum verification.
const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const MAX_ROWS = 5_000;
const MAX_OBJECT_BYTES = 64 * 1024 * 1024;
const MAX_OPUS_OBJECT_BYTES = 16 * 1024 * 1024;
const MAX_URI_BYTES = 1_024;
const MAX_GENERATION_BYTES = 256;
const MAX_FILENAME_BYTES = 512;
const SHA256 = /^[0-9a-f]{64}$/;
const DECIMAL_TIMESTAMP = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;
const SUPPORTED_SUFFIXES = [
  [".batch.enc", "pcm", true, true],
  [".batch.bin", "pcm", true, false],
  [".opus.enc", "opus", false, true],
  [".opus", "opus", false, false],
  [".enc", "pcm", false, true],
  [".bin", "pcm", false, false],
];

function fail(message) {
  throw new Error(`audio reconciliation: ${message}`);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function boundedPathPart(value, field, maxLength = 256) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maxLength ||
    value === "." ||
    value === ".." ||
    /[\\/\0\u0000-\u001f\u007f]/.test(value)
  ) {
    fail(`${field} is invalid`);
  }
  return value;
}

function uidValue(row) {
  return boundedPathPart(row.uid ?? row.user_id, "uid");
}

function conversationValue(row) {
  return boundedPathPart(
    row.conversation_id ?? row.conversationId,
    "conversation_id",
    128,
  );
}

function decodePathSegment(value, field) {
  try {
    const decoded = decodeURIComponent(value);
    return boundedPathPart(decoded, field, 512);
  } catch {
    fail(`${field} is invalid`);
  }
}

function sourceUriValue(row, uid, conversationId) {
  const value = row.source_object_uri ?? row.gcs_uri ?? row.gcs_path;
  if (typeof value !== "string" || value.length === 0 || Buffer.byteLength(value, "utf8") > MAX_URI_BYTES)
    fail("source_object_uri is invalid");
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    fail("source_object_uri must be a credential-free gs:// URI");
  }
  if (
    parsed.protocol !== "gs:" ||
    !parsed.hostname ||
    parsed.username ||
    parsed.password ||
    parsed.port ||
    parsed.search ||
    parsed.hash
  ) {
    fail("source_object_uri must be a credential-free gs:// URI");
  }
  const rawSegments = parsed.pathname.split("/").filter(Boolean);
  if (rawSegments.length !== 4 || rawSegments[0] !== "chunks")
    fail("source_object_uri must point to chunks/{uid}/{conversation}/...");
  const segments = rawSegments.map((segment, index) =>
    decodePathSegment(segment, `source_object_uri segment ${index}`),
  );
  if (segments[1] !== uid || segments[2] !== conversationId)
    fail("source_object_uri identity does not match uid/conversation_id");
  const objectName = segments.join("/");
  return {
    objectName,
    fileName: segments[3],
    // Normalize escaped path segments so equivalent GCS URLs deduplicate.
    uri: `gs://${parsed.hostname}/${objectName}`,
  };
}

function checksumValue(row) {
  const value = row.checksum_sha256 ?? row.sha256;
  if (value === undefined || value === null || value === "") return null;
  return typeof value === "string" && SHA256.test(value.toLowerCase())
    ? value.toLowerCase()
    : "invalid";
}

function generationValue(row) {
  const value = row.source_generation ?? row.generation;
  if (value === undefined || value === null || value === "") return null;
  if (typeof value === "number") {
    return Number.isSafeInteger(value) && value > 0 ? String(value) : "invalid";
  }
  return typeof value === "string" && /^\d+$/.test(value) && value.length <= MAX_GENERATION_BYTES
    ? value
    : "invalid";
}

function sizeValue(row) {
  const value = Number(row.size);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function timestampValue(value) {
  if (typeof value !== "string" || !DECIMAL_TIMESTAMP.test(value)) return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0 || parsed > 4_102_444_800) return null;
  return Math.round(parsed * 1_000) / 1_000;
}

function parseChunkName(fileName) {
  if (Buffer.byteLength(fileName, "utf8") > MAX_FILENAME_BYTES) return null;
  const descriptor = SUPPORTED_SUFFIXES.find(([suffix]) => fileName.endsWith(suffix));
  if (!descriptor) return null;
  const [suffix, sourceKind, batch, encrypted] = descriptor;
  const stem = fileName.slice(0, -suffix.length);
  const pieces = batch ? stem.split("-") : [stem];
  if (pieces.length !== (batch && pieces.length === 2 ? 2 : 1)) return null;
  const startTimestamp = timestampValue(pieces[0]);
  const endTimestamp = timestampValue(pieces[1] ?? pieces[0]);
  if (startTimestamp === null || endTimestamp === null || endTimestamp < startTimestamp)
    return null;
  return {
    sourceKind,
    encrypted,
    batch,
    startTimestamp,
    endTimestamp,
    suffix,
  };
}

function planCandidate(row, now) {
  if (!row || typeof row !== "object" || Array.isArray(row)) fail("each row must be an object");
  const uid = uidValue(row);
  const conversationId = conversationValue(row);
  const source = sourceUriValue(row, uid, conversationId);
  const checksum = checksumValue(row);
  const sourceGeneration = generationValue(row);
  const size = sizeValue(row);
  const chunk = parseChunkName(source.fileName);
  const errors = [];
  if (!checksum) errors.push("checksum_missing");
  else if (checksum === "invalid") errors.push("checksum_invalid");
  if (!sourceGeneration) errors.push("source_generation_missing");
  else if (sourceGeneration === "invalid") errors.push("source_generation_invalid");
  if (size === null || size > MAX_OBJECT_BYTES) errors.push("size_invalid");
  if (chunk?.sourceKind === "opus" && size !== null && size > MAX_OPUS_OBJECT_BYTES)
    errors.push("opus_size_invalid");
  if (!chunk) errors.push("unsupported_chunk_name");
  const importId = sha256(
    `${uid}\0${conversationId}\0${source.uri}\0${sourceGeneration ?? ""}\0${checksum ?? ""}`,
  );
  const planHash = sha256(
    JSON.stringify({
      uid,
      conversationId,
      sourceObjectUri: source.uri,
      sourceGeneration: sourceGeneration === "invalid" ? null : sourceGeneration,
      checksum: checksum === "invalid" ? null : checksum,
      size,
      chunk,
      destinationKey: source.objectName,
      errors,
    }),
  );
  return {
    uid,
    conversationId,
    importId,
    sourceObjectUri: source.uri,
    sourceGeneration: sourceGeneration === "invalid" ? null : sourceGeneration,
    sourceObjectName: source.objectName,
    checksum: checksum === "invalid" ? null : checksum,
    size,
    sourceKind: chunk?.sourceKind ?? null,
    encrypted: chunk?.encrypted ?? null,
    batch: chunk?.batch ?? null,
    startTimestamp: chunk?.startTimestamp ?? null,
    endTimestamp: chunk?.endTimestamp ?? null,
    destinationKey: source.objectName,
    planHash,
    action: errors.length ? "blocked" : "stage",
    status: errors.length ? "blocked" : "planned",
    lastError: errors.length ? errors.join(",") : null,
    createdAt: now,
    updatedAt: now,
  };
}

function blockConflict(entry, reason) {
  entry.action = "blocked";
  entry.status = "blocked";
  entry.lastError = reason;
  entry.planHash = sha256(JSON.stringify(entry));
}

export function planAudioReconciliation(
  records,
  { maxRows = MAX_ROWS, now = Math.floor(Date.now() / 1_000), fencedUids = [] } = {},
) {
  if (!Array.isArray(records)) fail("input must be an array");
  if (records.length > maxRows || records.length > MAX_ROWS)
    fail(`maximum ${Math.min(maxRows, MAX_ROWS)} rows`);
  const fenced = new Set(fencedUids);
  const byImportId = new Map();
  for (const record of records) {
    const candidate = planCandidate(record, now);
    if (fenced.has(candidate.uid)) {
      candidate.action = "blocked";
      candidate.status = "blocked";
      candidate.lastError = "account_deletion_fence";
      candidate.planHash = sha256(JSON.stringify(candidate));
    }
    const prior = byImportId.get(candidate.importId);
    if (!prior) byImportId.set(candidate.importId, candidate);
    else if (prior.planHash !== candidate.planHash) {
      blockConflict(prior, "conflicting_duplicate_plan");
      blockConflict(candidate, "conflicting_duplicate_plan");
      byImportId.set(`${candidate.importId}:conflict`, candidate);
    }
  }
  const entries = [...byImportId.values()];
  const destinations = new Map();
  for (const entry of entries) {
    if (entry.action !== "stage") continue;
    const prior = destinations.get(entry.destinationKey);
    if (prior && prior !== entry) {
      blockConflict(prior, "conflicting_destination_claim");
      blockConflict(entry, "conflicting_destination_claim");
    } else {
      destinations.set(entry.destinationKey, entry);
    }
  }
  entries.sort((left, right) =>
    `${left.uid}\0${left.conversationId}\0${left.destinationKey}\0${left.importId}`.localeCompare(
      `${right.uid}\0${right.conversationId}\0${right.destinationKey}\0${right.importId}`,
    ),
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

export function renderAudioLedgerSql(plan, now = Math.floor(Date.now() / 1_000)) {
  if (!plan || !Array.isArray(plan.entries)) fail("plan is invalid");
  return plan.entries
    .map((entry) => {
      const columns = [
        "uid",
        "import_id",
        "conversation_id",
        "source_object_uri",
        "source_generation",
        "source_object_name",
        "checksum_sha256",
        "size",
        "source_kind",
        "encrypted",
        "is_batch",
        "start_timestamp",
        "end_timestamp",
        "desired_storage_key",
        "plan_hash",
        "action",
        "status",
        "last_error",
        "created_at",
        "updated_at",
      ];
      const values = [
        entry.uid,
        entry.importId,
        entry.conversationId,
        entry.sourceObjectUri,
        entry.sourceGeneration,
        entry.sourceObjectName,
        entry.checksum,
        entry.size,
        entry.sourceKind,
        entry.encrypted === null ? null : entry.encrypted ? 1 : 0,
        entry.batch === null ? null : entry.batch ? 1 : 0,
        entry.startTimestamp,
        entry.endTimestamp,
        entry.destinationKey,
        entry.planHash,
        entry.action,
        entry.status,
        entry.lastError,
        entry.createdAt,
        now,
      ];
      return (
        `INSERT INTO cf_audio_chunk_import_ledger (${columns.join(", ")}) VALUES (${values.map(sqlLiteral).join(", ")}) ` +
        "ON CONFLICT(uid, import_id) DO UPDATE SET " +
        "status = CASE WHEN cf_audio_chunk_import_ledger.plan_hash = excluded.plan_hash " +
        "THEN cf_audio_chunk_import_ledger.status ELSE 'blocked' END, " +
        "action = CASE WHEN cf_audio_chunk_import_ledger.plan_hash = excluded.plan_hash " +
        "THEN cf_audio_chunk_import_ledger.action ELSE 'blocked' END, " +
        "last_error = CASE WHEN cf_audio_chunk_import_ledger.plan_hash = excluded.plan_hash " +
        "THEN excluded.last_error ELSE 'conflicting_duplicate_plan' END, " +
        "updated_at = excluded.updated_at;"
      );
    })
    .join("\n");
}

export function renderAudioR2Plan(plan) {
  if (!plan || !Array.isArray(plan.entries)) fail("plan is invalid");
  return plan.entries
    .filter((entry) => entry.action === "stage")
    .map((entry) => ({
      source_object_uri: entry.sourceObjectUri,
      source_generation: entry.sourceGeneration,
      destination_key: entry.destinationKey,
      checksum_sha256: entry.checksum,
      size: entry.size,
      source_kind: entry.sourceKind,
      encrypted: entry.encrypted,
      batch: entry.batch,
      start_timestamp: entry.startTimestamp,
      end_timestamp: entry.endTimestamp,
      if_generation_match: entry.sourceGeneration,
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
  if (parsed && typeof parsed === "object" && Array.isArray(parsed.objects)) return parsed.objects;
  if (parsed && typeof parsed === "object" && Array.isArray(parsed.chunks)) return parsed.chunks;
  if (parsed && typeof parsed === "object" && Array.isArray(parsed.rows)) return parsed.rows;
  fail("input must be an array or an object with objects/chunks/rows");
}

async function main() {
  const args = process.argv.slice(2);
  const inputIndex = args.indexOf("--input");
  const filename = inputIndex >= 0 ? args[inputIndex + 1] : null;
  if (!filename || filename.startsWith("--")) {
    process.stderr.write("usage: node scripts/audio-reconcile.mjs --input export.json [--fenced-uid uid]\n");
    process.exitCode = 2;
    return;
  }
  const fencedUids = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === "--fenced-uid" && args[index + 1]) fencedUids.push(args[++index]);
  }
  const plan = planAudioReconciliation(await readInput(filename), { fencedUids });
  process.stdout.write(
    `${JSON.stringify(
      {
        ...plan,
        ledger_sql: renderAudioLedgerSql(plan),
        r2_copy_plan: renderAudioR2Plan(plan),
      },
      null,
      2,
    )}\n`,
  );
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}
