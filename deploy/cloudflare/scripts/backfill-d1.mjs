import { readFile } from "node:fs/promises";

const TABLES = {
  cf_announcements: {
    key_columns: ["id"],
    required: ["id", "type", "created_at"],
    columns: [
      "id",
      "type",
      "created_at",
      "active",
      "app_version",
      "firmware_version",
      "device_models_json",
      "expires_at",
      "targeting_json",
      "display_json",
      "content_json",
    ],
    defaults: { active: 1, device_models_json: "[]", content_json: "{}" },
    integers: ["created_at", "expires_at"],
    json: ["device_models_json", "targeting_json", "display_json", "content_json"],
  },
  cf_announcement_dismissals: {
    key_columns: ["uid", "announcement_id"],
    required: ["uid", "announcement_id", "dismissed_at"],
    columns: ["uid", "announcement_id", "dismissed_at", "cta_clicked"],
    defaults: { cta_clicked: 0 },
    integers: ["dismissed_at"],
    json: [],
  },
  cf_asset_objects: {
    key_columns: ["uid", "object_key"],
    required: ["uid", "object_key", "content_type", "size", "etag", "created_at", "updated_at"],
    columns: [
      "uid",
      "object_key",
      "content_type",
      "size",
      "etag",
      "checksum_sha256",
      "created_at",
      "updated_at",
    ],
    defaults: { checksum_sha256: "" },
    integers: ["size", "created_at", "updated_at"],
    json: [],
  },
  cf_action_items: {
    required: ["uid", "id", "description", "created_at", "updated_at"],
    columns: [
      "uid",
      "id",
      "description",
      "status",
      "completed",
      "goal_id",
      "workstream_id",
      "owner",
      "due_at",
      "due_confidence",
      "source",
      "provenance_json",
      "priority",
      "sort_order",
      "indent_level",
      "recurrence_rule",
      "recurrence_parent_id",
      "superseded_by",
      "conversation_id",
      "is_locked",
      "exported",
      "export_date",
      "export_platform",
      "apple_reminder_id",
      "completed_at",
      "created_at",
      "updated_at",
      "idempotency_key",
      "sync_requested",
      "deleted",
    ],
    defaults: {
      status: "active",
      completed: 0,
      owner: "unknown",
      source: "legacy",
      provenance_json: "[]",
      sort_order: 0,
      indent_level: 0,
      is_locked: 0,
      exported: 0,
      sync_requested: 0,
      deleted: 0,
    },
    integers: [
      "completed",
      "sort_order",
      "indent_level",
      "is_locked",
      "exported",
      "export_date",
      "completed_at",
      "created_at",
      "updated_at",
      "sync_requested",
      "deleted",
    ],
    json: ["provenance_json"],
  },
  cf_people: {
    required: ["uid", "id", "name", "created_at", "updated_at"],
    columns: [
      "uid",
      "id",
      "name",
      "speech_samples_json",
      "speech_sample_transcripts_json",
      "speech_samples_version",
      "created_at",
      "updated_at",
    ],
    defaults: { speech_samples_json: "[]", speech_samples_version: 3 },
    integers: ["speech_samples_version", "created_at", "updated_at"],
    json: ["speech_samples_json", "speech_sample_transcripts_json"],
  },
  cf_goals: {
    required: ["uid", "id", "title", "desired_outcome", "status", "created_at", "updated_at"],
    columns: [
      "uid",
      "id",
      "title",
      "desired_outcome",
      "why_it_matters",
      "success_criteria_json",
      "horizon_at",
      "status",
      "focus_rank",
      "metric_json",
      "source",
      "relationship_disposition",
      "is_active",
      "latest_progress_sequence",
      "ended_at",
      "created_at",
      "updated_at",
    ],
    defaults: {
      success_criteria_json: "[]",
      source: "imported",
      relationship_disposition: "retain",
      is_active: 1,
      latest_progress_sequence: 0,
    },
    integers: [
      "horizon_at",
      "focus_rank",
      "is_active",
      "latest_progress_sequence",
      "ended_at",
      "created_at",
      "updated_at",
    ],
    json: ["success_criteria_json", "metric_json"],
  },
  cf_folders: {
    required: ["uid", "id", "name", "created_at", "updated_at"],
    columns: [
      "uid",
      "id",
      "name",
      "description",
      "color",
      "icon",
      "created_at",
      "updated_at",
      "display_order",
      "is_default",
      "is_system",
      "category_mapping",
      "conversation_count",
    ],
    defaults: {
      color: "#6B7280",
      icon: "folder",
      display_order: 0,
      is_default: 0,
      is_system: 0,
      conversation_count: 0,
    },
    integers: [
      "created_at",
      "updated_at",
      "display_order",
      "is_default",
      "is_system",
      "conversation_count",
    ],
    json: [],
  },
  cf_focus_sessions: {
    required: ["uid", "id", "status", "app_or_site", "description", "created_at"],
    columns: ["uid", "id", "status", "app_or_site", "description", "message", "created_at", "duration_seconds"],
    defaults: {},
    integers: ["created_at", "duration_seconds"],
    json: [],
  },
  cf_screen_activity: {
    required: ["uid", "id", "timestamp"],
    columns: [
      "uid",
      "id",
      "timestamp",
      "app_name",
      "window_title",
      "ocr_text",
      "device_name",
      "client_device_id",
    ],
    defaults: { app_name: "", window_title: "", ocr_text: "" },
    integers: [],
    json: [],
  },
  cf_user_calendar_onboarding: {
    key_columns: ["uid"],
    required: ["uid", "created_at", "updated_at"],
    columns: [
      "uid",
      "connected",
      "onboarding_skipped",
      "reauth_required",
      "has_access_token",
      "reauth_reason",
      "created_at",
      "updated_at",
    ],
    defaults: { connected: 0, onboarding_skipped: 0, reauth_required: 0, has_access_token: 0 },
    integers: ["created_at", "updated_at"],
    json: [],
  },
  cf_goal_progress_history: {
    key_columns: ["uid", "goal_id", "date"],
    required: ["uid", "goal_id", "date", "value", "recorded_at"],
    columns: ["uid", "goal_id", "date", "value", "recorded_at"],
    defaults: {},
    integers: ["recorded_at"],
    json: [],
  },
  cf_goal_progress_events: {
    key_columns: ["uid", "event_id"],
    required: ["uid", "event_id", "goal_id", "sequence", "kind", "summary", "created_at"],
    columns: [
      "uid",
      "event_id",
      "goal_id",
      "sequence",
      "kind",
      "summary",
      "evidence_refs_json",
      "metric_json",
      "created_at",
    ],
    defaults: { evidence_refs_json: "[]" },
    integers: ["sequence", "created_at"],
    json: ["evidence_refs_json", "metric_json"],
  },
  cf_workstreams: {
    key_columns: ["uid", "id"],
    required: ["uid", "id", "title", "objective", "status", "created_at", "updated_at"],
    columns: [
      "uid",
      "id",
      "goal_id",
      "title",
      "objective",
      "status",
      "current_state_summary",
      "next_review_at",
      "last_meaningful_progress_at",
      "latest_event_sequence",
      "account_generation",
      "created_at",
      "updated_at",
    ],
    defaults: { current_state_summary: "", latest_event_sequence: 0, account_generation: 0 },
    integers: [
      "next_review_at",
      "last_meaningful_progress_at",
      "latest_event_sequence",
      "account_generation",
      "created_at",
      "updated_at",
    ],
    json: [],
  },
  cf_workstream_events: {
    key_columns: ["uid", "event_id"],
    required: ["uid", "event_id", "workstream_id", "sequence", "kind", "summary", "created_at"],
    columns: [
      "uid",
      "event_id",
      "workstream_id",
      "sequence",
      "kind",
      "summary",
      "evidence_refs_json",
      "sensitivity",
      "created_at",
    ],
    defaults: { evidence_refs_json: "[]", sensitivity: "normal" },
    integers: ["sequence", "created_at"],
    json: ["evidence_refs_json"],
  },
  cf_workstream_artifacts: {
    key_columns: ["uid", "artifact_id"],
    required: [
      "uid",
      "artifact_id",
      "workstream_id",
      "logical_key",
      "version",
      "kind",
      "uri",
      "content_hash",
      "status",
      "created_at",
    ],
    columns: [
      "uid",
      "artifact_id",
      "workstream_id",
      "logical_key",
      "version",
      "supersedes_artifact_id",
      "kind",
      "uri",
      "content_hash",
      "source_run_id",
      "evidence_event_ids_json",
      "evidence_refs_json",
      "status",
      "created_at",
      "account_generation",
    ],
    defaults: { evidence_event_ids_json: "[]", evidence_refs_json: "[]", account_generation: 0 },
    integers: ["version", "created_at", "account_generation"],
    json: ["evidence_event_ids_json", "evidence_refs_json"],
  },
  cf_workstream_checkpoints: {
    key_columns: ["uid", "checkpoint_id"],
    required: [
      "uid",
      "checkpoint_id",
      "workstream_id",
      "runtime_id",
      "last_event_sequence",
      "context_summary",
      "updated_at",
    ],
    columns: [
      "uid",
      "checkpoint_id",
      "workstream_id",
      "runtime_id",
      "last_event_sequence",
      "context_summary",
      "evidence_refs_json",
      "updated_at",
      "account_generation",
    ],
    defaults: { evidence_refs_json: "[]", account_generation: 0 },
    integers: ["last_event_sequence", "updated_at", "account_generation"],
    json: ["evidence_refs_json"],
  },
};

const BOOL_COLUMNS = new Set([
  "completed",
  "is_locked",
  "exported",
  "sync_requested",
  "deleted",
  "is_active",
  "is_default",
  "is_system",
  "cta_clicked",
  "connected",
  "onboarding_skipped",
  "reauth_required",
  "has_access_token",
]);
const DATE_COLUMNS = new Set([
  "due_at",
  "export_date",
  "completed_at",
  "horizon_at",
  "ended_at",
  "created_at",
  "updated_at",
]);

function fail(message) {
  throw new Error(`backfill input: ${message}`);
}

function epochSeconds(value, column) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number" && Number.isSafeInteger(value)) return value;
  if (typeof value === "string" && /^-?\d+$/.test(value.trim())) {
    const parsed = Number(value.trim());
    if (Number.isSafeInteger(parsed)) return parsed;
  }
  const parsed = Date.parse(String(value));
  if (!Number.isNaN(parsed)) return Math.floor(parsed / 1000);
  fail(`${column} must be epoch seconds or ISO timestamp`);
}

function boolValue(value, column) {
  if (value === null || value === undefined) return null;
  if (value === true || value === 1 || value === "1" || value === "true") return 1;
  if (value === false || value === 0 || value === "0" || value === "false") return 0;
  fail(`${column} must be boolean`);
}

function jsonValue(value, column) {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") {
    try {
      JSON.parse(value);
      return value;
    } catch {
      fail(`${column} must contain valid JSON`);
    }
  }
  try {
    return JSON.stringify(value);
  } catch {
    fail(`${column} must be JSON serializable`);
  }
}

function normalizeTimestamp(value) {
  const parsed = new Date(String(value).replace(" ", "T").replace(/Z$/, "+00:00"));
  if (Number.isNaN(parsed.getTime())) fail("timestamp must be ISO-8601 compatible");
  const iso = parsed.toISOString();
  return `${iso.slice(0, 19).replace("T", " ")}.${iso.slice(20, 23)}`;
}

function sqlLiteral(value) {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail("numeric value must be finite");
    return String(value);
  }
  return `'${String(value).replaceAll("'", "''")}'`;
}

export function normalizeRow(table, input) {
  const spec = TABLES[table];
  if (!spec) fail(`unsupported table ${table}`);
  if (!input || typeof input !== "object" || Array.isArray(input)) fail(`${table} row must be an object`);
  const row = { ...input };
  if (row.provenance !== undefined && row.provenance_json === undefined) row.provenance_json = row.provenance;
  if (row.success_criteria !== undefined && row.success_criteria_json === undefined) {
    row.success_criteria_json = row.success_criteria;
  }
  if (row.content !== undefined && row.content_json === undefined) row.content_json = row.content;
  if (row.targeting !== undefined && row.targeting_json === undefined) row.targeting_json = row.targeting;
  if (row.display !== undefined && row.display_json === undefined) row.display_json = row.display;
  if (row.device_models !== undefined && row.device_models_json === undefined) row.device_models_json = row.device_models;
  for (const required of spec.required) {
    if (row[required] === undefined || row[required] === null || row[required] === "") {
      fail(`${table} row is missing ${required}`);
    }
  }
  const keyColumns = spec.key_columns || ["uid", "id"];
  for (const keyColumn of keyColumns) {
    if (typeof row[keyColumn] !== "string" || row[keyColumn].length === 0 || row[keyColumn].length > 256) {
      fail(`${table}.${keyColumn} is invalid`);
    }
  }
  const normalized = {};
  for (const column of spec.columns) {
    let value = row[column];
    if (value === undefined && Object.hasOwn(spec.defaults, column)) value = spec.defaults[column];
    if (value === undefined) continue;
    if (spec.json.includes(column)) value = jsonValue(value, column);
    else if (BOOL_COLUMNS.has(column)) value = boolValue(value, column);
    else if (spec.integers.includes(column)) value = epochSeconds(value, column);
    else if (column === "timestamp") value = normalizeTimestamp(value);
    else if (typeof value !== "string" && value !== null) value = String(value);
    normalized[column] = value;
  }
  if (table === "cf_action_items") {
    const completed = normalized.completed === 1;
    if (row.status === undefined) normalized.status = completed ? "completed" : "active";
    if (normalized.status === "completed" && normalized.completed === undefined) normalized.completed = 1;
    if (normalized.status !== "completed" && normalized.completed === undefined) normalized.completed = 0;
    if (normalized.status === "completed" && normalized.completed !== 1) fail("completed action item must have completed=1");
  }
  return normalized;
}

function insertSql(table, row) {
  const keyColumns = TABLES[table].key_columns || ["uid", "id"];
  const columns = Object.keys(row);
  const values = columns.map((column) => sqlLiteral(row[column]));
  const updates = columns.filter((column) => !keyColumns.includes(column));
  const updateClause = updates.length
    ? ` ON CONFLICT(${keyColumns.join(", ")}) DO UPDATE SET ${updates.map((column) => `${column} = excluded.${column}`).join(", ")}`
    : ` ON CONFLICT(${keyColumns.join(", ")}) DO NOTHING`;
  return `INSERT INTO ${table} (${columns.join(", ")}) VALUES (${values.join(", ")})${updateClause};`;
}

export function normalizeRows(records, { table = null, maxRows = 5000 } = {}) {
  if (!Array.isArray(records)) fail("input must be an array of records");
  if (records.length > maxRows) fail(`maximum ${maxRows} rows per run`);
  return records.map((record, index) => {
    if (!record || typeof record !== "object" || Array.isArray(record)) fail(`record ${index + 1} must be an object`);
    const recordTable = table || record.table;
    if (typeof recordTable !== "string") fail(`record ${index + 1} is missing table`);
    const sourceRow = record.row && typeof record.row === "object" && !Array.isArray(record.row) ? record.row : record;
    return { table: recordTable, row: normalizeRow(recordTable, sourceRow) };
  });
}

export function renderBackfillSql(records, options = {}) {
  const rows = normalizeRows(records, options);
  const statements = rows.map(({ table, row }) => insertSql(table, row));
  return [
    "-- Generated by deploy/cloudflare/scripts/backfill-d1.mjs; review before applying.",
    "PRAGMA foreign_keys = ON;",
    "BEGIN TRANSACTION;",
    ...statements,
    "COMMIT;",
    "",
  ].join("\n");
}

async function main() {
  const args = process.argv.slice(2);
  const inputIndex = args.indexOf("--input");
  const tableIndex = args.indexOf("--table");
  const maxRowsIndex = args.indexOf("--max-rows");
  const inputPath = inputIndex >= 0 ? args[inputIndex + 1] : null;
  const table = tableIndex >= 0 ? args[tableIndex + 1] : null;
  const maxRows = maxRowsIndex >= 0 ? Number(args[maxRowsIndex + 1]) : 5000;
  if (maxRowsIndex >= 0 && (!Number.isSafeInteger(maxRows) || maxRows < 1 || maxRows > 5000)) {
    fail("--max-rows must be an integer between 1 and 5000");
  }
  const raw = inputPath
    ? await readFile(inputPath, "utf8")
    : await new Promise((resolve, reject) => {
        const chunks = [];
        process.stdin.setEncoding("utf8");
        process.stdin.on("data", (chunk) => chunks.push(chunk));
        process.stdin.on("end", () => resolve(chunks.join("")));
        process.stdin.on("error", reject);
      });
  const records = raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      try {
        return JSON.parse(line);
      } catch {
        fail(`line ${index + 1} is not valid JSON`);
      }
    });
  process.stdout.write(renderBackfillSql(records, { table, maxRows }));
}

if (process.argv[1]?.endsWith("backfill-d1.mjs")) {
  main().catch((error) => {
    console.error(`D1 backfill generation failed: ${error instanceof Error ? error.message : "unknown error"}`);
    process.exitCode = 1;
  });
}
