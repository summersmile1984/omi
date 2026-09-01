import { open } from "node:fs/promises";

// Backfill input is an operator-supplied export, not a trusted stream. Keep
// the generator bounded before parsing JSON so a malformed or accidentally
// huge export cannot consume the operator process's memory.
export const MAX_BACKFILL_INPUT_BYTES = 64 * 1024 * 1024;

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
    json: [
      "device_models_json",
      "targeting_json",
      "display_json",
      "content_json",
    ],
  },
  cf_trend_categories: {
    key_columns: ["id"],
    required: ["id", "category", "type", "created_at"],
    columns: ["id", "category", "type", "created_at"],
    defaults: {},
    integers: ["created_at"],
    json: [],
  },
  cf_trend_topics: {
    key_columns: ["category_id", "id"],
    required: ["category_id", "id", "topic"],
    columns: ["category_id", "id", "topic", "memories_count"],
    defaults: { memories_count: 0 },
    integers: ["memories_count"],
    json: [],
  },
  cf_app_catalog: {
    key_columns: ["id"],
    required: ["id", "data_json", "updated_at"],
    columns: [
      "id",
      "approved",
      "status",
      "disabled",
      "is_popular",
      "installs",
      "rating_avg",
      "rating_count",
      "owner_uid",
      "data_json",
      "updated_at",
    ],
    defaults: {
      approved: 0,
      status: "approved",
      disabled: 0,
      is_popular: 0,
      installs: 0,
      rating_count: 0,
    },
    integers: ["installs", "rating_count", "updated_at"],
    json: ["data_json"],
  },
  cf_app_payment_links: {
    key_columns: ["app_id"],
    required: [
      "app_id",
      "owner_uid",
      "stripe_account_id",
      "stripe_product_id",
      "stripe_price_id",
      "stripe_payment_link_id",
      "payment_link_url",
      "unit_amount",
      "created_at",
      "updated_at",
    ],
    columns: [
      "app_id",
      "owner_uid",
      "stripe_account_id",
      "stripe_product_id",
      "stripe_price_id",
      "stripe_payment_link_id",
      "payment_link_url",
      "unit_amount",
      "currency",
      "interval",
      "active",
      "created_at",
      "updated_at",
    ],
    defaults: { currency: "usd", interval: "month", active: 1 },
    integers: ["unit_amount", "created_at", "updated_at"],
    json: [],
  },
  cf_mcp_api_keys: {
    key_columns: ["key_id"],
    required: ["uid", "key_id", "key_hash", "created_at"],
    columns: [
      "uid",
      "key_id",
      "name",
      "key_hash",
      "key_prefix",
      "app_id",
      "scopes_json",
      "created_at",
      "last_used_at",
    ],
    defaults: {
      name: "Legacy MCP API key",
      key_prefix: "omi_mcp_legacy",
      app_id: "mcp-api",
      scopes_json: JSON.stringify([
        "action_items.read",
        "action_items.write",
        "chat.read",
        "conversations.read",
        "goals.read",
        "memories.read",
        "memories.write",
        "people.read",
        "screen_activity.read",
      ]),
    },
    integers: ["created_at", "last_used_at"],
    json: ["scopes_json"],
  },
  cf_user_enabled_apps: {
    key_columns: ["uid", "app_id"],
    required: ["uid", "app_id", "created_at"],
    columns: ["uid", "app_id", "created_at"],
    defaults: {},
    integers: ["created_at"],
    json: [],
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
    required: [
      "uid",
      "object_key",
      "content_type",
      "size",
      "etag",
      "created_at",
      "updated_at",
    ],
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
  // A ready chat-file row is only safe to import after the source object and
  // provider object have both been verified.  The importer therefore accepts
  // only the committed state; staging/failed rows belong to the reconciliation
  // ledger and must not be promoted by a bulk SQL backfill.
  cf_chat_files: {
    key_columns: ["uid", "file_id"],
    required: [
      "uid",
      "file_id",
      "request_fingerprint",
      "provider",
      "provider_file_id",
      "name",
      "mime_type",
      "size",
      "checksum_sha256",
      "storage_key",
      "status",
      "thumbnail_status",
      "created_at",
      "updated_at",
    ],
    columns: [
      "uid",
      "file_id",
      "request_fingerprint",
      "provider",
      "provider_file_id",
      "name",
      "mime_type",
      "size",
      "checksum_sha256",
      "storage_key",
      "thumbnail_key",
      "status",
      "thumbnail_status",
      "created_at",
      "updated_at",
      "last_error",
    ],
    defaults: { thumbnail_key: null, last_error: null },
    integers: ["size", "created_at", "updated_at"],
    json: [],
  },
  cf_chat_file_import_ledger: {
    key_columns: ["uid", "import_id"],
    required: [
      "uid",
      "import_id",
      "source_file_id",
      "source_object_uri",
      "name",
      "mime_type",
      "desired_storage_key",
      "plan_hash",
      "action",
      "status",
      "created_at",
      "updated_at",
    ],
    columns: [
      "uid",
      "import_id",
      "source_file_id",
      "source_object_uri",
      "source_generation",
      "checksum_sha256",
      "provider_file_id",
      "name",
      "mime_type",
      "size",
      "desired_storage_key",
      "plan_hash",
      "action",
      "status",
      "last_error",
      "created_at",
      "updated_at",
    ],
    defaults: {
      source_generation: null,
      checksum_sha256: null,
      provider_file_id: null,
      size: null,
      last_error: null,
    },
    integers: ["size", "created_at", "updated_at"],
    json: [],
  },
  // Chat history is imported only after the Firestore export has been
  // verified.  The payload stays in one bounded JSON column so the Worker
  // can preserve the legacy wire shape without accepting provider secrets.
  cf_chat_sessions: {
    key_columns: ["uid", "id"],
    required: ["uid", "id", "title", "created_at", "updated_at"],
    columns: [
      "uid",
      "id",
      "title",
      "preview",
      "created_at",
      "updated_at",
      "app_id",
      "message_count",
      "starred",
    ],
    defaults: { preview: null, app_id: null, message_count: 0, starred: 0 },
    integers: ["created_at", "updated_at", "message_count"],
    json: [],
  },
  cf_chat_messages: {
    key_columns: ["uid", "id"],
    required: ["uid", "id", "created_at", "message_json"],
    columns: ["uid", "id", "app_id", "created_at", "message_json"],
    defaults: { app_id: null },
    integers: ["created_at"],
    json: ["message_json"],
  },
  cf_conversations: {
    key_columns: ["uid", "id"],
    required: ["uid", "id", "created_at"],
    columns: [
      "uid",
      "id",
      "created_at",
      "updated_at",
      "started_at",
      "finished_at",
      "source",
      "language",
      "status",
      "visibility",
      "starred",
      "discarded",
      "is_locked",
      "deferred",
      "private_cloud_sync_enabled",
      "folder_id",
      "client_device_id",
      "client_platform",
      "structured_json",
      "transcript_segments_json",
      "photos_json",
      "audio_files_json",
      "conversation_audio_json",
      "apps_results_json",
      "suggested_apps_json",
      "geolocation_json",
      "external_data_json",
      "calendar_event_json",
    ],
    defaults: {
      source: "omi",
      status: "completed",
      visibility: "private",
      starred: 0,
      discarded: 0,
      is_locked: 0,
      deferred: 0,
      private_cloud_sync_enabled: 0,
      structured_json: "{}",
      transcript_segments_json: "[]",
      photos_json: "[]",
      audio_files_json: "[]",
      apps_results_json: "[]",
      suggested_apps_json: "[]",
    },
    integers: ["created_at", "updated_at", "started_at", "finished_at"],
    json: [
      "structured_json",
      "transcript_segments_json",
      "photos_json",
      "audio_files_json",
      "conversation_audio_json",
      "apps_results_json",
      "suggested_apps_json",
      "geolocation_json",
      "external_data_json",
      "calendar_event_json",
    ],
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
  cf_x_posts: {
    required: ["uid", "id", "text", "kind", "created_at", "updated_at"],
    columns: [
      "uid",
      "id",
      "text",
      "kind",
      "lang",
      "metrics_json",
      "created_at",
      "ingested_at",
      "updated_at",
      "memory_extraction_status",
      "memory_extracted_at",
    ],
    defaults: {
      metrics_json: "{}",
      memory_extraction_status: "pending",
    },
    integers: [
      "created_at",
      "ingested_at",
      "updated_at",
      "memory_extracted_at",
    ],
    json: ["metrics_json"],
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
    required: [
      "uid",
      "id",
      "title",
      "desired_outcome",
      "status",
      "created_at",
      "updated_at",
    ],
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
    required: [
      "uid",
      "id",
      "status",
      "app_or_site",
      "description",
      "created_at",
    ],
    columns: [
      "uid",
      "id",
      "status",
      "app_or_site",
      "description",
      "message",
      "created_at",
      "duration_seconds",
    ],
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
    defaults: {
      connected: 0,
      onboarding_skipped: 0,
      reauth_required: 0,
      has_access_token: 0,
    },
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
    required: [
      "uid",
      "event_id",
      "goal_id",
      "sequence",
      "kind",
      "summary",
      "created_at",
    ],
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
    required: [
      "uid",
      "id",
      "title",
      "objective",
      "status",
      "created_at",
      "updated_at",
    ],
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
    defaults: {
      current_state_summary: "",
      latest_event_sequence: 0,
      account_generation: 0,
    },
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
    required: [
      "uid",
      "event_id",
      "workstream_id",
      "sequence",
      "kind",
      "summary",
      "created_at",
    ],
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
    defaults: {
      evidence_event_ids_json: "[]",
      evidence_refs_json: "[]",
      account_generation: 0,
    },
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
  "starred",
  "discarded",
  "deferred",
  "private_cloud_sync_enabled",
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
  "active",
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

/**
 * Decode and parse newline-delimited JSON without accepting replacement
 * characters for malformed UTF-8. The byte limit is checked before JSON
 * parsing, and is intentionally independent of the --max-rows limit.
 */
export function parseBackfillInput(input, { maxBytes = MAX_BACKFILL_INPUT_BYTES } = {}) {
  const bytes =
    input instanceof Uint8Array
      ? input
      : typeof input === "string"
        ? new TextEncoder().encode(input)
        : null;
  if (!bytes) fail("input must be UTF-8 bytes or text");
  if (bytes.byteLength > maxBytes) fail(`input exceeds ${maxBytes} bytes`);
  let raw;
  try {
    raw = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    fail("input is not valid UTF-8");
  }
  return raw
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
}

async function readBoundedInput(inputPath) {
  if (inputPath) {
    const handle = await open(inputPath, "r");
    const chunks = [];
    let total = 0;
    try {
      for (;;) {
        const chunk = Buffer.allocUnsafe(64 * 1024);
        const { bytesRead } = await handle.read(chunk, 0, chunk.byteLength, null);
        if (!bytesRead) break;
        total += bytesRead;
        if (total > MAX_BACKFILL_INPUT_BYTES) {
          fail(`input exceeds ${MAX_BACKFILL_INPUT_BYTES} bytes`);
        }
        chunks.push(chunk.subarray(0, bytesRead));
      }
    } finally {
      await handle.close();
    }
    return Buffer.concat(chunks, total);
  }

  const chunks = [];
  let total = 0;
  for await (const chunk of process.stdin) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += bytes.byteLength;
    if (total > MAX_BACKFILL_INPUT_BYTES) {
      fail(`input exceeds ${MAX_BACKFILL_INPUT_BYTES} bytes`);
    }
    chunks.push(bytes);
  }
  return Buffer.concat(chunks, total);
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
  if (value === true || value === 1 || value === "1" || value === "true")
    return 1;
  if (value === false || value === 0 || value === "0" || value === "false")
    return 0;
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

const CHAT_MESSAGE_FORBIDDEN_KEYS = new Set([
  "access_token",
  "api_key",
  "authorization",
  "custom_token",
  "firebase_id_token",
  "gemini_api_key",
  "id_token",
  "openai_api_key",
  "password",
  "private_key",
  "refresh_token",
  "secret",
  "secret_key",
]);
const MAX_CHAT_MESSAGE_JSON_BYTES = 256 * 1024;
const MAX_CHAT_MESSAGE_NODES = 1_024;

function validateChatMessageJson(value, messageId) {
  if (typeof value !== "string") fail("cf_chat_messages.message_json must be JSON");
  if (new TextEncoder().encode(value).byteLength > MAX_CHAT_MESSAGE_JSON_BYTES) {
    fail("cf_chat_messages.message_json exceeds 256KiB");
  }
  let parsed;
  try {
    parsed = JSON.parse(value);
  } catch {
    fail("cf_chat_messages.message_json must contain valid JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    fail("cf_chat_messages.message_json must be an object");
  }

  let nodes = 0;
  const visit = (current, depth) => {
    if (++nodes > MAX_CHAT_MESSAGE_NODES || depth > 16) {
      fail("cf_chat_messages.message_json is too deeply nested");
    }
    if (!current || typeof current !== "object") return;
    for (const [key, child] of Object.entries(current)) {
      if (CHAT_MESSAGE_FORBIDDEN_KEYS.has(key.toLowerCase())) {
        fail(`cf_chat_messages.message_json contains forbidden field ${key}`);
      }
      visit(child, depth + 1);
    }
  };
  visit(parsed, 0);

  const message = parsed;
  if (message.id !== undefined && message.id !== messageId) {
    fail("cf_chat_messages.message_json.id must match id");
  }
  message.id = messageId;
  if (typeof message.text !== "string" || message.text.length > 200_000) {
    fail("cf_chat_messages.message_json.text is invalid");
  }
  if (!new Set(["human", "ai"]).has(message.sender)) {
    fail("cf_chat_messages.message_json.sender is invalid");
  }
  if (!new Set(["text", "day_summary"]).has(message.type)) {
    fail("cf_chat_messages.message_json.type is invalid");
  }
  const sessionId = message.chat_session_id ?? message.session_id;
  if (sessionId !== undefined && sessionId !== null && sessionId !== "") {
    if (typeof sessionId !== "string" || sessionId.length > 256) {
      fail("cf_chat_messages.message_json session id is invalid");
    }
  }
  const normalized = JSON.stringify(message);
  if (new TextEncoder().encode(normalized).byteLength > MAX_CHAT_MESSAGE_JSON_BYTES) {
    fail("cf_chat_messages.message_json exceeds 256KiB");
  }
  return normalized;
}

function validateChatText(value, column, { min = 0, max = 500 } = {}) {
  if (typeof value !== "string" || value.length < min || value.length > max) {
    fail(`${column} is invalid`);
  }
  // Firestore chat titles/previews historically allowed line breaks.  Keep
  // that wire behavior while refusing NUL, which cannot safely cross D1/JSON
  // tooling boundaries.
  if (value.includes("\0")) fail(`${column} contains control characters`);
}

function normalizeTimestamp(value) {
  const parsed = new Date(
    String(value).replace(" ", "T").replace(/Z$/, "+00:00"),
  );
  if (Number.isNaN(parsed.getTime()))
    fail("timestamp must be ISO-8601 compatible");
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
  if (!input || typeof input !== "object" || Array.isArray(input))
    fail(`${table} row must be an object`);
  const row = { ...input };
  if (table === "cf_mcp_api_keys") {
    for (const forbidden of [
      "key",
      "raw_key",
      "raw_token",
      "api_key",
      "token",
      "secret",
      "secret_key",
    ]) {
      if (row[forbidden] !== undefined)
        fail(`cf_mcp_api_keys must not contain raw secret field ${forbidden}`);
    }
    if (row.user_id !== undefined && row.uid === undefined)
      row.uid = row.user_id;
    if (row.id !== undefined && row.key_id === undefined) row.key_id = row.id;
    if (row.hashed_key !== undefined && row.key_hash === undefined)
      row.key_hash = row.hashed_key;
    if (row.scopes !== undefined && row.scopes_json === undefined)
      row.scopes_json = row.scopes;
  }
  if (row.provenance !== undefined && row.provenance_json === undefined)
    row.provenance_json = row.provenance;
  if (row.metrics !== undefined && row.metrics_json === undefined)
    row.metrics_json = row.metrics;
  if (
    row.success_criteria !== undefined &&
    row.success_criteria_json === undefined
  ) {
    row.success_criteria_json = row.success_criteria;
  }
  if (row.content !== undefined && row.content_json === undefined)
    row.content_json = row.content;
  if (row.structured !== undefined && row.structured_json === undefined)
    row.structured_json = row.structured;
  if (
    row.transcript_segments !== undefined &&
    row.transcript_segments_json === undefined
  ) {
    row.transcript_segments_json = row.transcript_segments;
  }
  if (row.photos !== undefined && row.photos_json === undefined)
    row.photos_json = row.photos;
  if (row.audio_files !== undefined && row.audio_files_json === undefined)
    row.audio_files_json = row.audio_files;
  if (
    row.conversation_audio !== undefined &&
    row.conversation_audio_json === undefined
  ) {
    row.conversation_audio_json = row.conversation_audio;
  }
  if (row.apps_results !== undefined && row.apps_results_json === undefined)
    row.apps_results_json = row.apps_results;
  if (
    row.suggested_summarization_apps !== undefined &&
    row.suggested_apps_json === undefined
  ) {
    row.suggested_apps_json = row.suggested_summarization_apps;
  }
  if (row.geolocation !== undefined && row.geolocation_json === undefined)
    row.geolocation_json = row.geolocation;
  if (row.external_data !== undefined && row.external_data_json === undefined)
    row.external_data_json = row.external_data;
  if (
    row.calendar_event !== undefined &&
    row.calendar_event_json === undefined
  ) {
    row.calendar_event_json = row.calendar_event;
  }
  if (row.data !== undefined && row.data_json === undefined)
    row.data_json = row.data;
  if (row.targeting !== undefined && row.targeting_json === undefined)
    row.targeting_json = row.targeting;
  if (row.display !== undefined && row.display_json === undefined)
    row.display_json = row.display;
  if (row.device_models !== undefined && row.device_models_json === undefined)
    row.device_models_json = row.device_models;
  for (const required of spec.required) {
    if (
      row[required] === undefined ||
      row[required] === null ||
      row[required] === ""
    ) {
      fail(`${table} row is missing ${required}`);
    }
  }
  const keyColumns = spec.key_columns || ["uid", "id"];
  for (const keyColumn of keyColumns) {
    if (
      typeof row[keyColumn] !== "string" ||
      row[keyColumn].length === 0 ||
      row[keyColumn].length > 256
    ) {
      fail(`${table}.${keyColumn} is invalid`);
    }
  }
  const normalized = {};
  for (const column of spec.columns) {
    let value = row[column];
    if (value === undefined && Object.hasOwn(spec.defaults, column))
      value = spec.defaults[column];
    if (value === undefined) continue;
    if (spec.json.includes(column)) value = jsonValue(value, column);
    else if (BOOL_COLUMNS.has(column)) value = boolValue(value, column);
    else if (spec.integers.includes(column))
      value = epochSeconds(value, column);
    else if (column === "timestamp") value = normalizeTimestamp(value);
    else if (typeof value !== "string" && value !== null) value = String(value);
    normalized[column] = value;
  }
  if (table === "cf_chat_sessions") {
    validateChatText(normalized.title, "cf_chat_sessions.title", { min: 1, max: 500 });
    if (normalized.preview !== null && normalized.preview !== undefined) {
      validateChatText(normalized.preview, "cf_chat_sessions.preview", { max: 1_000 });
    }
    for (const column of ["app_id"]) {
      if (normalized[column] !== null && normalized[column] !== undefined) {
        validateChatText(normalized[column], `cf_chat_sessions.${column}`, { min: 1, max: 256 });
      }
    }
    if (
      !Number.isSafeInteger(normalized.message_count) ||
      normalized.message_count < 0 ||
      normalized.message_count > 1_000_000
    ) {
      fail("cf_chat_sessions.message_count is invalid");
    }
    if (![0, 1].includes(normalized.starred)) fail("cf_chat_sessions.starred is invalid");
  }
  if (table === "cf_chat_messages") {
    normalized.message_json = validateChatMessageJson(normalized.message_json, normalized.id);
    if (normalized.app_id !== null && normalized.app_id !== undefined) {
      validateChatText(normalized.app_id, "cf_chat_messages.app_id", { min: 1, max: 256 });
    }
  }
  if (table === "cf_action_items") {
    const completed = normalized.completed === 1;
    if (row.status === undefined)
      normalized.status = completed ? "completed" : "active";
    if (normalized.status === "completed" && normalized.completed === undefined)
      normalized.completed = 1;
    if (normalized.status !== "completed" && normalized.completed === undefined)
      normalized.completed = 0;
    if (normalized.status === "completed" && normalized.completed !== 1)
      fail("completed action item must have completed=1");
  }
  if (table === "cf_x_posts") {
    if (!new Set(["tweet", "bookmark", "like"]).has(normalized.kind))
      fail("cf_x_posts.kind is invalid");
    if (
      typeof normalized.text !== "string" ||
      normalized.text.trim().length === 0 ||
      new TextEncoder().encode(normalized.text).length > 100_000
    ) {
      fail("cf_x_posts.text is invalid");
    }
    if (
      !new Set(["pending", "completed"]).has(
        normalized.memory_extraction_status,
      )
    ) {
      fail("cf_x_posts.memory_extraction_status is invalid");
    }
  }
  if (table === "cf_trend_categories") {
    if (
      !new Set([
        "ceo",
        "company",
        "software_product",
        "hardware_product",
        "ai_product",
      ]).has(normalized.category)
    ) {
      fail("cf_trend_categories.category is invalid");
    }
    if (!new Set(["best", "worst"]).has(normalized.type)) {
      fail("cf_trend_categories.type is invalid");
    }
  }
  if (table === "cf_trend_topics") {
    if (
      typeof normalized.topic !== "string" ||
      normalized.topic.length < 1 ||
      normalized.topic.length > 512
    ) {
      fail("cf_trend_topics.topic is invalid");
    }
    if (
      !Number.isSafeInteger(normalized.memories_count) ||
      normalized.memories_count < 0
    ) {
      fail("cf_trend_topics.memories_count is invalid");
    }
  }
  if (table === "cf_app_catalog") {
    const raw = normalized.data_json;
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      fail("cf_app_catalog.data_json must contain valid JSON");
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      fail("cf_app_catalog.data_json must contain an object");
    }
    const forbidden = new Set([
      "email",
      "reviews",
      "user_review",
      "persona_prompt",
      "chat_prompt",
      "memory_prompt",
      "payment_product_id",
      "payment_price_id",
      "payment_link_id",
      "money_made",
      "usage_count",
      "twitter",
      "mcp_oauth_tokens",
    ]);
    const scan = (value) => {
      if (!value || typeof value !== "object") return false;
      for (const [key, nested] of Object.entries(value)) {
        if (forbidden.has(key) || scan(nested)) return true;
      }
      return false;
    };
    if (scan(payload)) fail("cf_app_catalog.data_json contains private fields");
    if (normalized.owner_uid === undefined && typeof payload.uid === "string") {
      normalized.owner_uid = payload.uid;
    }
    if (
      normalized.owner_uid !== undefined &&
      (typeof normalized.owner_uid !== "string" ||
        normalized.owner_uid.length < 1 ||
        normalized.owner_uid.length > 256)
    ) {
      fail("cf_app_catalog.owner_uid is invalid");
    }
    if (payload.id !== undefined && payload.id !== normalized.id) {
      fail("cf_app_catalog.data_json id must match id");
    }
    if (
      payload.capabilities !== undefined &&
      (!Array.isArray(payload.capabilities) ||
        payload.capabilities.some((value) => typeof value !== "string"))
    ) {
      fail("cf_app_catalog.data_json capabilities must be a string array");
    }
    if (String(raw).length > 500_000)
      fail("cf_app_catalog.data_json is too large");
    if (normalized.installs < 0 || normalized.rating_count < 0) {
      fail("cf_app_catalog counters must be non-negative");
    }
  }
  if (table === "cf_app_payment_links") {
    const providerIds = [
      ["stripe_account_id", /^acct_[A-Za-z0-9]{7,155}$/],
      ["stripe_product_id", /^prod_[A-Za-z0-9]{7,155}$/],
      ["stripe_price_id", /^price_[A-Za-z0-9]{7,155}$/],
      ["stripe_payment_link_id", /^plink_[A-Za-z0-9]{7,155}$/],
    ];
    for (const [column, pattern] of providerIds) {
      if (
        typeof normalized[column] !== "string" ||
        !pattern.test(normalized[column])
      ) {
        fail(`cf_app_payment_links.${column} is invalid`);
      }
    }
    if (
      typeof normalized.owner_uid !== "string" ||
      normalized.owner_uid.length < 1 ||
      normalized.owner_uid.length > 256 ||
      normalized.owner_uid.includes("/")
    ) {
      fail("cf_app_payment_links.owner_uid is invalid");
    }
    if (
      !Number.isSafeInteger(normalized.unit_amount) ||
      normalized.unit_amount <= 0
    ) {
      fail("cf_app_payment_links.unit_amount is invalid");
    }
    if (normalized.currency !== "usd" || normalized.interval !== "month") {
      fail("cf_app_payment_links billing terms are unsupported");
    }
    try {
      const paymentLink = new URL(normalized.payment_link_url);
      if (paymentLink.protocol !== "https:")
        throw new Error("invalid protocol");
    } catch {
      fail("cf_app_payment_links.payment_link_url is invalid");
    }
  }
  if (table === "cf_mcp_api_keys") {
    if (
      typeof normalized.key_hash !== "string" ||
      !/^[0-9a-f]{64}$/.test(normalized.key_hash)
    ) {
      fail("cf_mcp_api_keys.key_hash is invalid");
    }
    if (
      normalized.key_prefix !== "omi_mcp_legacy" &&
      !/^omi_mcp_[0-9a-f]{4}\.\.\.[0-9a-f]{4}$/.test(normalized.key_prefix)
    ) {
      fail("cf_mcp_api_keys.key_prefix is invalid");
    }
    if (normalized.app_id !== "mcp-api")
      fail("cf_mcp_api_keys.app_id is invalid");
    const fullScopes = [
      "action_items.read",
      "action_items.write",
      "chat.read",
      "conversations.read",
      "goals.read",
      "memories.read",
      "memories.write",
      "people.read",
      "screen_activity.read",
    ];
    const parsedScopes = JSON.parse(normalized.scopes_json);
    if (
      !Array.isArray(parsedScopes) ||
      parsedScopes.length !== fullScopes.length ||
      new Set(parsedScopes).size !== fullScopes.length ||
      [...parsedScopes]
        .sort()
        .some((scope, index) => scope !== fullScopes[index])
    ) {
      fail("cf_mcp_api_keys.scopes_json must contain the full MCP scope set");
    }
    normalized.scopes_json = JSON.stringify(fullScopes);
  }
  if (table === "cf_chat_file_import_ledger") {
    if (
      typeof normalized.uid !== "string" ||
      normalized.uid.length < 1 ||
      normalized.uid.length > 256 ||
      /[\\/\0]/.test(normalized.uid)
    ) {
      fail("cf_chat_file_import_ledger.uid is invalid");
    }
    if (
      typeof normalized.import_id !== "string" ||
      !/^[0-9a-f]{64}$/.test(normalized.import_id)
    ) {
      fail("cf_chat_file_import_ledger.import_id is invalid");
    }
    if (
      typeof normalized.source_file_id !== "string" ||
      normalized.source_file_id.length < 1 ||
      normalized.source_file_id.length > 128 ||
      /[\\/\0]/.test(normalized.source_file_id)
    ) {
      fail("cf_chat_file_import_ledger.source_file_id is invalid");
    }
    if (
      typeof normalized.source_object_uri !== "string" ||
      normalized.source_object_uri.length < 1 ||
      normalized.source_object_uri.length > 1024 ||
      !/^gs:\/\/[^/]+\/[^\s]+$/.test(normalized.source_object_uri) ||
      /[\0\u0000-\u001f\u007f]/.test(normalized.source_object_uri)
    ) {
      fail("cf_chat_file_import_ledger.source_object_uri is invalid");
    }
    if (
      normalized.source_generation !== null &&
      normalized.source_generation !== undefined &&
      (typeof normalized.source_generation !== "string" ||
        normalized.source_generation.length < 1 ||
        normalized.source_generation.length > 256 ||
        /[\0\u0000-\u001f\u007f]/.test(normalized.source_generation))
    ) {
      fail("cf_chat_file_import_ledger.source_generation is invalid");
    }
    if (
      normalized.checksum_sha256 !== null &&
      normalized.checksum_sha256 !== undefined &&
      (typeof normalized.checksum_sha256 !== "string" ||
        !/^[0-9a-f]{64}$/.test(normalized.checksum_sha256))
    ) {
      fail("cf_chat_file_import_ledger.checksum_sha256 is invalid");
    }
    if (
      normalized.provider_file_id !== null &&
      normalized.provider_file_id !== undefined &&
      (typeof normalized.provider_file_id !== "string" ||
        !/^file-[A-Za-z0-9_-]{1,256}$/.test(normalized.provider_file_id))
    ) {
      fail("cf_chat_file_import_ledger.provider_file_id is invalid");
    }
    if (
      typeof normalized.name !== "string" ||
      normalized.name.length < 1 ||
      normalized.name.length > 512 ||
      normalized.name.includes("\0")
    ) {
      fail("cf_chat_file_import_ledger.name is invalid");
    }
    if (
      typeof normalized.mime_type !== "string" ||
      normalized.mime_type.length < 1 ||
      normalized.mime_type.length > 200 ||
      !/^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/.test(
        normalized.mime_type,
      )
    ) {
      fail("cf_chat_file_import_ledger.mime_type is invalid");
    }
    if (
      normalized.size !== null &&
      normalized.size !== undefined &&
      (!Number.isSafeInteger(normalized.size) ||
        normalized.size <= 0 ||
        normalized.size > 50 * 1024 * 1024)
    ) {
      fail("cf_chat_file_import_ledger.size is invalid");
    }
    if (
      typeof normalized.desired_storage_key !== "string" ||
      normalized.desired_storage_key.length < 1 ||
      normalized.desired_storage_key.length > 512 ||
      !normalized.desired_storage_key.startsWith(`${normalized.uid}/`) ||
      /[\0\u0000-\u001f\u007f]/.test(normalized.desired_storage_key)
    ) {
      fail("cf_chat_file_import_ledger.desired_storage_key is invalid");
    }
    if (
      typeof normalized.plan_hash !== "string" ||
      !/^[0-9a-f]{64}$/.test(normalized.plan_hash)
    ) {
      fail("cf_chat_file_import_ledger.plan_hash is invalid");
    }
    if (!["stage", "blocked"].includes(normalized.action))
      fail("cf_chat_file_import_ledger.action is invalid");
    if (!["planned", "blocked", "applied", "failed"].includes(normalized.status))
      fail("cf_chat_file_import_ledger.status is invalid");
    if (
      normalized.last_error !== null &&
      normalized.last_error !== undefined &&
      (typeof normalized.last_error !== "string" || normalized.last_error.length > 2048)
    ) {
      fail("cf_chat_file_import_ledger.last_error is invalid");
    }
  }
  if (table === "cf_chat_files") {
    if (
      typeof normalized.uid !== "string" ||
      normalized.uid.length < 1 ||
      normalized.uid.length > 256 ||
      /[\\/\0]/.test(normalized.uid)
    ) {
      fail("cf_chat_files.uid is invalid");
    }
    if (
      typeof normalized.file_id !== "string" ||
      normalized.file_id.length < 1 ||
      normalized.file_id.length > 128 ||
      /[\\/\0\u0000-\u001f\u007f]/.test(normalized.file_id)
    ) {
      fail("cf_chat_files.file_id is invalid");
    }
    if (
      typeof normalized.request_fingerprint !== "string" ||
      !/^[0-9a-f]{64}$/.test(normalized.request_fingerprint)
    ) {
      fail("cf_chat_files.request_fingerprint is invalid");
    }
    if (normalized.provider !== "openai")
      fail("cf_chat_files.provider is invalid");
    if (
      typeof normalized.provider_file_id !== "string" ||
      !/^file-[A-Za-z0-9_-]{1,256}$/.test(normalized.provider_file_id)
    ) {
      fail("cf_chat_files.provider_file_id is invalid");
    }
    if (
      typeof normalized.name !== "string" ||
      normalized.name.length < 1 ||
      normalized.name.length > 512 ||
      /[\0\u0000-\u001f\u007f]/.test(normalized.name)
    ) {
      fail("cf_chat_files.name is invalid");
    }
    if (
      typeof normalized.mime_type !== "string" ||
      normalized.mime_type.length < 1 ||
      normalized.mime_type.length > 200 ||
      !/^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/.test(
        normalized.mime_type,
      )
    ) {
      fail("cf_chat_files.mime_type is invalid");
    }
    if (
      !Number.isSafeInteger(normalized.size) ||
      normalized.size <= 0 ||
      normalized.size > 50 * 1024 * 1024
    ) {
      fail("cf_chat_files.size is invalid");
    }
    if (
      typeof normalized.checksum_sha256 !== "string" ||
      !/^[0-9a-f]{64}$/.test(normalized.checksum_sha256)
    ) {
      fail("cf_chat_files.checksum_sha256 is invalid");
    }
    if (
      typeof normalized.storage_key !== "string" ||
      normalized.storage_key.length < 1 ||
      normalized.storage_key.length > 512 ||
      !normalized.storage_key.startsWith(`${normalized.uid}/`) ||
      /[\0\u0000-\u001f\u007f]/.test(normalized.storage_key)
    ) {
      fail("cf_chat_files.storage_key is invalid");
    }
    if (normalized.thumbnail_key !== null && normalized.thumbnail_key !== undefined) {
      if (
        typeof normalized.thumbnail_key !== "string" ||
        normalized.thumbnail_key.length < 1 ||
        normalized.thumbnail_key.length > 512 ||
        !normalized.thumbnail_key.startsWith(`${normalized.uid}/`) ||
        /[\0\u0000-\u001f\u007f]/.test(normalized.thumbnail_key)
      ) {
        fail("cf_chat_files.thumbnail_key is invalid");
      }
    }
    if (normalized.status !== "ready")
      fail("cf_chat_files.status must be ready for backfill");
    if (!new Set(["not_applicable", "unsupported", "ready"]).has(normalized.thumbnail_status))
      fail("cf_chat_files.thumbnail_status is invalid");
    if (normalized.last_error !== null && normalized.last_error !== undefined)
      fail("cf_chat_files.last_error must be null for ready backfill");
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

function xPostOutboxSql(row) {
  const values = [
    sqlLiteral(row.uid),
    "'x_post'",
    sqlLiteral(row.id),
    sqlLiteral(row.updated_at),
    "'upsert'",
    "0",
    sqlLiteral(row.updated_at),
    "NULL",
    sqlLiteral(row.updated_at),
    sqlLiteral(row.updated_at),
  ];
  return (
    "INSERT INTO cf_vector_projection_outbox " +
    "(uid, source_kind, source_id, desired_version, operation, attempts, next_attempt_at, last_error, created_at, updated_at) " +
    `VALUES (${values.join(", ")}) ` +
    "ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET " +
    "desired_version = excluded.desired_version, operation = 'upsert', attempts = 0, " +
    "next_attempt_at = excluded.next_attempt_at, last_error = NULL, updated_at = excluded.updated_at;"
  );
}

export function normalizeRows(records, { table = null, maxRows = 5000 } = {}) {
  if (!Array.isArray(records)) fail("input must be an array of records");
  if (records.length > maxRows) fail(`maximum ${maxRows} rows per run`);
  return records.map((record, index) => {
    if (!record || typeof record !== "object" || Array.isArray(record))
      fail(`record ${index + 1} must be an object`);
    const recordTable = table || record.table;
    if (typeof recordTable !== "string")
      fail(`record ${index + 1} is missing table`);
    const sourceRow =
      record.row && typeof record.row === "object" && !Array.isArray(record.row)
        ? record.row
        : record;
    return { table: recordTable, row: normalizeRow(recordTable, sourceRow) };
  });
}

export function renderBackfillSql(records, options = {}) {
  const rows = normalizeRows(records, options);
  const statements = rows.flatMap(({ table, row }) => [
    insertSql(table, row),
    ...(table === "cf_x_posts" ? [xPostOutboxSql(row)] : []),
  ]);
  return [
    "-- Generated by deploy/cloudflare/scripts/backfill-d1.mjs; review and apply with wrangler d1 execute --remote --file.",
    "-- D1 remote file ingestion supplies the transaction; manual BEGIN/COMMIT statements are rejected.",
    "PRAGMA foreign_keys = ON;",
    ...statements,
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
  if (
    maxRowsIndex >= 0 &&
    (!Number.isSafeInteger(maxRows) || maxRows < 1 || maxRows > 5000)
  ) {
    fail("--max-rows must be an integer between 1 and 5000");
  }
  const records = parseBackfillInput(await readBoundedInput(inputPath));
  process.stdout.write(renderBackfillSql(records, { table, maxRows }));
}

if (process.argv[1]?.endsWith("backfill-d1.mjs")) {
  main().catch((error) => {
    console.error(
      `D1 backfill generation failed: ${error instanceof Error ? error.message : "unknown error"}`,
    );
    process.exitCode = 1;
  });
}
