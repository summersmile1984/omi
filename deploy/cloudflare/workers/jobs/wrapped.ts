import type { Message } from "@cloudflare/workers-types";
import type { Context, Hono } from "hono";
import type { JobMessage, JobsEnv } from "./env";

const SUPPORTED_YEAR = 2025;
const MAX_REQUEST_BYTES = 4_096;
const MAX_CONVERSATIONS = 10_000;
const MAX_ACTION_ITEMS = 10_000;
const MAX_PROVIDER_CONTEXT_BYTES = 96_000;
const MAX_RESULT_BYTES = 256_000;
const MAX_ATTEMPTS = 3;
const LEASE_SECONDS = 15 * 60;
const RETRY_DELAY_SECONDS = 10;
const WRAPPED_MODEL = "@cf/meta/llama-3.2-3b-instruct";
const SIGNATURE_PHRASES = [
  "let's do this",
  "sounds good",
  "makes sense",
  "i think",
  "we should",
  "let me",
  "i need to",
  "we need to",
  "that's interesting",
  "exactly",
  "absolutely",
  "definitely",
  "basically",
  "actually",
  "honestly",
  "you know",
  "i mean",
  "right",
  "okay",
  "got it",
] as const;

type WrappedContext = Context<{ Bindings: JobsEnv }>;
type AuthContext = { uid: string };
type WrappedStatus = "queued" | "running" | "completed" | "failed";

type WrappedJobRow = {
  uid: string;
  year: number;
  job_id: string;
  request_fingerprint: string;
  source_fingerprint: string;
  account_generation: number;
  status: WrappedStatus;
  attempts: number;
  lease_token: string | null;
  lease_until: number | null;
  next_attempt_at: number;
  result_json: string | null;
  last_error: string | null;
  created_at: number;
  updated_at: number;
};

type ConversationRow = {
  id: string;
  created_at: number;
  updated_at: number | null;
  started_at: number | null;
  finished_at: number | null;
  structured_json: string;
  transcript_segments_json: string;
};

type ActionItemRow = {
  id: string;
  description: string;
  status: string;
  completed: number;
  created_at: number;
};

type ConversationSource = {
  id: string;
  createdAt: number;
  updatedAt: number;
  startedAt: number | null;
  finishedAt: number | null;
  title: string;
  overview: string;
  category: string;
  segments: Array<{ text: string; end: number; isUser: boolean }>;
};

type ActionSource = {
  id: string;
  description: string;
  completed: boolean;
  createdAt: number;
};

type SourceSnapshot = {
  conversations: ConversationSource[];
  actionItems: ActionSource[];
  fingerprint: string;
};

class WrappedHttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

class WrappedTerminalError extends Error {}

function validUid(value: string): boolean {
  return value.length > 0 && value.length <= 256 && !value.includes("/") && !value.includes("\u0000");
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function parseObject(value: string | null | undefined): Record<string, unknown> | null {
  if (!value || new TextEncoder().encode(value).byteLength > 64_000) return null;
  try {
    return objectValue(JSON.parse(value));
  } catch {
    return null;
  }
}

function parseArray(value: string | null | undefined): unknown[] | null {
  if (!value || new TextEncoder().encode(value).byteLength > 256_000) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function finiteInteger(value: unknown, minimum = 0): number | null {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isSafeInteger(number) && number >= minimum ? number : null;
}

function boundedString(value: unknown, maximum: number): string {
  return typeof value === "string" ? value.trim().slice(0, maximum) : "";
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
  return JSON.stringify(value) ?? "null";
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function isoDate(epoch: number): string {
  return new Date(epoch * 1_000).toISOString().slice(0, 10);
}

function yearRange(year: number): { start: number; end: number } {
  return {
    start: Math.floor(Date.UTC(year, 0, 1) / 1_000),
    end: Math.floor(Date.UTC(year + 1, 0, 1) / 1_000),
  };
}

function parseSegments(value: string): ConversationSource["segments"] {
  const parsed = parseArray(value);
  if (parsed === null) throw new WrappedTerminalError("invalid transcript projection");
  return parsed.slice(0, 200).map((item) => {
    const object = objectValue(item);
    if (!object) throw new WrappedTerminalError("invalid transcript segment projection");
    const end = finiteInteger(object.end, 0) ?? 0;
    const text = boundedString(object.text, 2_000);
    const isUser = object.is_user === true || object.isUser === true;
    return { text, end, isUser };
  });
}

function parseConversation(row: ConversationRow): ConversationSource {
  const createdAt = finiteInteger(row.created_at, 0);
  const updatedAt = finiteInteger(row.updated_at ?? row.created_at, 0);
  if (createdAt === null || updatedAt === null || typeof row.id !== "string" || !validUid(row.id))
    throw new WrappedTerminalError("invalid conversation projection");
  const structured = parseObject(row.structured_json);
  if (!structured) throw new WrappedTerminalError("invalid conversation structured projection");
  const startedAt = row.started_at === null ? null : finiteInteger(row.started_at, 0);
  const finishedAt = row.finished_at === null ? null : finiteInteger(row.finished_at, 0);
  if ((row.started_at !== null && startedAt === null) || (row.finished_at !== null && finishedAt === null))
    throw new WrappedTerminalError("invalid conversation timestamp projection");
  return {
    id: row.id,
    createdAt,
    updatedAt,
    startedAt,
    finishedAt,
    title: boundedString(structured.title, 300),
    overview: boundedString(structured.overview, 2_000),
    category: boundedString(structured.category, 100) || "other",
    segments: parseSegments(row.transcript_segments_json),
  };
}

function parseActionItem(row: ActionItemRow): ActionSource {
  const createdAt = finiteInteger(row.created_at, 0);
  if (createdAt === null || typeof row.id !== "string" || !validUid(row.id))
    throw new WrappedTerminalError("invalid action-item projection");
  if (typeof row.description !== "string" || row.description.trim().length === 0)
    throw new WrappedTerminalError("invalid action-item description projection");
  return {
    id: row.id,
    description: row.description.trim().slice(0, 1_000),
    completed: row.completed === 1 || row.status === "completed",
    createdAt,
  };
}

async function readSourceSnapshot(
  env: JobsEnv,
  uid: string,
  year: number,
): Promise<SourceSnapshot> {
  const range = yearRange(year);
  const conversationsResult = await env.APP_DB.prepare(
    "SELECT id, created_at, updated_at, started_at, finished_at, structured_json, transcript_segments_json " +
      "FROM cf_conversations WHERE uid = ? AND status = 'completed' AND discarded = 0 AND created_at >= ? AND created_at < ? " +
      "ORDER BY created_at ASC, id ASC LIMIT ?",
  )
    .bind(uid, range.start, range.end, MAX_CONVERSATIONS + 1)
    .all<ConversationRow>();
  const conversationRows = Array.isArray(conversationsResult.results)
    ? conversationsResult.results
    : [];
  if (conversationRows.length > MAX_CONVERSATIONS)
    throw new WrappedTerminalError("wrapped conversation source exceeds bounded limit");

  const actionResult = await env.APP_DB.prepare(
    "SELECT id, description, status, completed, created_at FROM cf_action_items " +
      "WHERE uid = ? AND deleted = 0 AND created_at >= ? AND created_at < ? " +
      "ORDER BY created_at ASC, id ASC LIMIT ?",
  )
    .bind(uid, range.start, range.end, MAX_ACTION_ITEMS + 1)
    .all<ActionItemRow>();
  const actionRows = Array.isArray(actionResult.results) ? actionResult.results : [];
  if (actionRows.length > MAX_ACTION_ITEMS)
    throw new WrappedTerminalError("wrapped action-item source exceeds bounded limit");
  const conversations = conversationRows.map(parseConversation);
  const actionItems = actionRows.map(parseActionItem);
  const fingerprint = await sha256Hex(
    stableJson({
      year,
      conversations: conversations.map((row) => ({
        id: row.id,
        createdAt: row.createdAt,
        updatedAt: row.updatedAt,
      })),
      actionItems: actionItems.map((row) => ({
        id: row.id,
        createdAt: row.createdAt,
        completed: row.completed,
        description: row.description,
      })),
    }),
  );
  return { conversations, actionItems, fingerprint };
}

function conversationDurationSeconds(conversation: ConversationSource): number {
  const segmentEnd = conversation.segments.reduce(
    (maximum, segment) => Math.max(maximum, segment.end),
    0,
  );
  if (segmentEnd > 0) return segmentEnd;
  if (
    conversation.startedAt !== null &&
    conversation.finishedAt !== null &&
    conversation.finishedAt >= conversation.startedAt
  ) {
    return Math.min(conversation.finishedAt - conversation.startedAt, 604_800);
  }
  return 300;
}

function computeStats(snapshot: SourceSnapshot): Record<string, unknown> {
  const categories = new Map<string, number>();
  const activeDays = new Set<string>();
  const phraseCounts = new Map<string, number>();
  let totalSeconds = 0;
  for (const conversation of snapshot.conversations) {
    activeDays.add(isoDate(conversation.createdAt));
    totalSeconds += conversationDurationSeconds(conversation);
    categories.set(conversation.category, (categories.get(conversation.category) || 0) + 1);
    const text = conversation.segments
      .filter((segment) => segment.isUser && segment.text)
      .map((segment) => segment.text.toLowerCase())
      .join(" ");
    for (const phrase of SIGNATURE_PHRASES) {
      const count = text.split(phrase).length - 1;
      if (count > 0) phraseCounts.set(phrase, (phraseCounts.get(phrase) || 0) + count);
    }
  }
  const categoryBreakdown = [...categories.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .slice(0, 5);
  const phrase = [...phraseCounts.entries()].sort(
    (left, right) => right[1] - left[1] || left[0].localeCompare(right[0]),
  )[0];
  const completed = snapshot.actionItems.filter((item) => item.completed).length;
  return {
    total_conversations: snapshot.conversations.length,
    days_active: activeDays.size,
    total_time_hours: Math.round((totalSeconds / 3_600) * 10) / 10,
    top_categories: categoryBreakdown.map(([category]) => category),
    category_breakdown: categoryBreakdown.map(([category, count]) => ({ category, count })),
    total_action_items: snapshot.actionItems.length,
    completed_action_items: completed,
    action_items_completion_rate:
      snapshot.actionItems.length ? completed / snapshot.actionItems.length : 0,
    signature_phrase: phrase ? { phrase: phrase[0], count: phrase[1] } : null,
  };
}

function providerContext(snapshot: SourceSnapshot, stats: Record<string, unknown>): string {
  const lines = [
    `STATS: ${stableJson(stats)}`,
    "CONVERSATION SUMMARIES:",
    ...snapshot.conversations.map(
      (conversation) =>
        `[${new Date(conversation.createdAt * 1_000).toISOString().slice(0, 16).replace("T", " ")}] ` +
        `${conversation.title || "Untitled"}: ${conversation.overview}`,
    ),
    "ACTION ITEMS:",
    ...snapshot.actionItems.map(
      (item) => `- ${item.completed ? "completed" : "open"}: ${item.description}`,
    ),
  ];
  let result = "";
  for (const line of lines) {
    const candidate = result ? `${result}\n${line}` : line;
    if (new TextEncoder().encode(candidate).byteLength > MAX_PROVIDER_CONTEXT_BYTES) break;
    result = candidate;
  }
  return result;
}

function wrappedProviderSchema() {
  const event = {
    type: "object",
    properties: {
      date: { type: "string" },
      title: { type: "string" },
      description: { type: "string" },
      story: { type: "string" },
      emoji: { type: "string" },
    },
    required: ["date", "title", "description", "story", "emoji"],
    additionalProperties: false,
  };
  return {
    type: "json_schema",
    json_schema: {
      name: "omi_wrapped_2025",
      strict: true,
      schema: {
        type: "object",
        properties: {
          decision_style: {
            type: "object",
            properties: { name: { type: "string" }, description: { type: "string" } },
            required: ["name", "description"],
            additionalProperties: false,
          },
          top_phrases: { type: "array", items: { type: "object", properties: { phrase: { type: "string" }, context: { type: "string" } }, required: ["phrase", "context"], additionalProperties: false }, },
          memorable_days: { type: "object", properties: { most_fun_day: event, most_productive_day: event, most_stressful_day: event }, required: ["most_fun_day", "most_productive_day", "most_stressful_day"], additionalProperties: false },
          funniest_event: event,
          most_embarrassing_event: event,
          top_buddies: { type: "array", items: { type: "object", properties: { name: { type: "string" }, relationship: { type: "string" }, context: { type: "string" }, emoji: { type: "string" } }, required: ["name", "relationship", "context", "emoji"], additionalProperties: false }, },
          obsessions: { type: "object", properties: { show: { type: "string" }, movie: { type: "string" }, book: { type: "string" }, celebrity: { type: "string" }, food: { type: "string" } }, required: ["show", "movie", "book", "celebrity", "food"], additionalProperties: false },
          movie_recommendations: { type: "array", items: { type: "string" } },
          struggle: { type: "object", properties: { title: { type: "string" }, description: { type: "string" } }, required: ["title", "description"], additionalProperties: false },
          personal_win: { type: "object", properties: { title: { type: "string" }, description: { type: "string" } }, required: ["title", "description"], additionalProperties: false },
        },
        required: ["decision_style", "top_phrases", "memorable_days", "funniest_event", "most_embarrassing_event", "top_buddies", "obsessions", "movie_recommendations", "struggle", "personal_win"],
        additionalProperties: false,
      },
    },
  };
}

function parseModelObject(value: unknown): Record<string, unknown> | null {
  const direct = objectValue(value);
  if (!direct) return null;
  const response = direct.response;
  const objectResponse = objectValue(response);
  if (objectResponse) return objectResponse;
  if (typeof response === "string") {
    const fenced = response.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1];
    try {
      return objectValue(JSON.parse((fenced || response).trim()));
    } catch {
      return null;
    }
  }
  return direct;
}

function validProviderObject(value: Record<string, unknown>): boolean {
  const requiredObjects = ["decision_style", "memorable_days", "funniest_event", "most_embarrassing_event", "obsessions", "struggle", "personal_win"];
  if (requiredObjects.some((key) => !objectValue(value[key]))) return false;
  if (!Array.isArray(value.top_phrases) || !Array.isArray(value.top_buddies) || !Array.isArray(value.movie_recommendations)) return false;
  if (value.top_phrases.length > 5 || value.top_buddies.length > 5 || value.movie_recommendations.length > 5) return false;
  const objects = [value.decision_style, value.memorable_days, value.funniest_event, value.most_embarrassing_event, value.obsessions, value.struggle, value.personal_win] as unknown[];
  return objects.every((item) => new TextEncoder().encode(JSON.stringify(item)).byteLength <= 32_000);
}

async function generateProviderResult(
  env: JobsEnv,
  snapshot: SourceSnapshot,
  stats: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  if (!env.AI || typeof env.AI.run !== "function")
    throw new Error("workers ai wrapped provider unavailable");
  let response: unknown;
  try {
    response = await env.AI.run(env.WORKERS_AI_WRAPPED_MODEL || WRAPPED_MODEL, {
      messages: [
        {
          role: "system",
          content:
            "Analyze a private yearly personal recap. Return only the requested JSON. " +
            "Do not invent facts; use Not mentioned when evidence is absent. Keep all text concise.",
        },
        {
          role: "user",
          content:
            "Generate the Wrapped insight fields from these bounded summaries. " +
            "Use Month Day dates and at most five array entries.\n\n" +
            providerContext(snapshot, stats),
        },
      ],
      response_format: wrappedProviderSchema(),
      max_tokens: 3_000,
      temperature: 0.1,
    });
  } catch {
    throw new Error("workers ai wrapped provider unavailable");
  }
  const parsed = parseModelObject(response);
  if (!parsed || !validProviderObject(parsed))
    throw new Error("workers ai returned invalid wrapped result");
  return parsed;
}

function publicJob(row: WrappedJobRow): Record<string, unknown> {
  let result: unknown = null;
  if (row.result_json) {
    try {
      result = JSON.parse(row.result_json);
    } catch {
      result = null;
    }
  }
  return {
    status:
      row.status === "queued" || row.status === "running"
        ? "processing"
        : row.status === "failed"
          ? "error"
          : row.status,
    year: row.year,
    result,
    error: row.last_error,
    progress: null,
  };
}

function publicGenerate(row: WrappedJobRow, message: string): Record<string, unknown> {
  return {
    status: row.status === "completed" ? "done" : row.status === "failed" ? "error" : "processing",
    message,
  };
}

async function deletionFenced(env: JobsEnv, uid: string, now: number): Promise<boolean> {
  const row = await env.APP_DB.prepare(
    "SELECT lifecycle FROM (" +
      "SELECT 'deleting' AS lifecycle, 0 AS priority FROM cf_account_deletion_intents WHERE uid = ? " +
      "UNION ALL SELECT 'deleted' AS lifecycle, 1 AS priority FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ?" +
      ") ORDER BY priority LIMIT 1",
  )
    .bind(uid, uid, now)
    .first<{ lifecycle?: string }>();
  return row?.lifecycle === "deleting" || row?.lifecycle === "deleted";
}

async function accountGeneration(env: JobsEnv, uid: string, now: number): Promise<number> {
  if (await deletionFenced(env, uid, now))
    throw new WrappedHttpError(409, "account_deletion_in_progress", "account deletion is in progress");
  const row = await env.APP_DB.prepare(
    "SELECT state, account_generation, checkpoint_phase, destination_backend_bound FROM cf_account_cutover WHERE uid = ?",
  )
    .bind(uid)
    .first<Record<string, unknown>>();
  const generation = finiteInteger(row?.account_generation, 0);
  if (
    !row || row.state !== "new" || row.checkpoint_phase !== "completed" ||
    row.destination_backend_bound !== 1 || generation === null
  ) {
    throw new WrappedHttpError(503, "wrapped_authority_unavailable", "Wrapped Cloudflare authority is not ready");
  }
  return generation;
}

async function readJob(env: JobsEnv, uid: string, year: number): Promise<WrappedJobRow | null> {
  return env.APP_DB.prepare("SELECT * FROM cf_wrapped_jobs WHERE uid = ? AND year = ?")
    .bind(uid, year)
    .first<WrappedJobRow>();
}

function queuePayload(row: WrappedJobRow): JobMessage {
  return {
    jobId: row.job_id,
    uid: row.uid,
    kind: "wrapped_generate",
    payload: {
      year: row.year,
      requestFingerprint: row.request_fingerprint,
      sourceFingerprint: row.source_fingerprint,
      accountGeneration: row.account_generation,
    },
  };
}

async function enqueue(env: JobsEnv, row: WrappedJobRow): Promise<void> {
  try {
    await env.JOBS.send(queuePayload(row));
  } catch {
    await env.APP_DB.prepare(
      "UPDATE cf_wrapped_jobs SET status = 'failed', lease_token = NULL, lease_until = NULL, last_error = ?, updated_at = ? " +
        "WHERE uid = ? AND year = ? AND status = 'queued'",
    )
      .bind("queue unavailable", Math.floor(Date.now() / 1_000), row.uid, row.year)
      .run();
    throw new WrappedHttpError(503, "queue_unavailable", "Wrapped queue is unavailable");
  }
}

async function parseGenerateBody(c: WrappedContext): Promise<void> {
  const declared = Number(c.req.header("content-length"));
  if (Number.isFinite(declared) && declared > MAX_REQUEST_BYTES)
    throw new WrappedHttpError(413, "payload_too_large", "Wrapped request is too large");
  const bytes = await c.req.raw.arrayBuffer();
  if (bytes.byteLength > MAX_REQUEST_BYTES)
    throw new WrappedHttpError(413, "payload_too_large", "Wrapped request is too large");
  if (!bytes.byteLength) return;
  let value: unknown;
  try {
    value = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new WrappedHttpError(400, "invalid_request", "Wrapped request must be JSON");
  }
  const object = objectValue(value);
  if (!object || Object.keys(object).length)
    throw new WrappedHttpError(400, "invalid_request", "Wrapped generation does not accept input fields");
}

export function registerWrappedRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: (c: WrappedContext) => Promise<AuthContext | null>,
): void {
  app.get("/v1/wrapped/:year", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    const year = Number(c.req.param("year"));
    if (year !== SUPPORTED_YEAR)
      return c.json({ detail: `Only year ${SUPPORTED_YEAR} is currently supported` }, 400);
    try {
      await accountGeneration(c.env, context.uid, Math.floor(Date.now() / 1_000));
      const row = await readJob(c.env, context.uid, year);
      return c.json(row ? publicJob(row) : { status: "not_generated", year, result: null, error: null, progress: null });
    } catch (error) {
      if (error instanceof WrappedHttpError) return c.json({ error: error.code, message: error.message }, error.status as 400);
      return c.json({ error: "wrapped_unavailable" }, 503);
    }
  });

  app.post("/v1/wrapped/:year/generate", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    const year = Number(c.req.param("year"));
    if (year !== SUPPORTED_YEAR)
      return c.json({ detail: `Only year ${SUPPORTED_YEAR} is currently supported` }, 400);
    try {
      await parseGenerateBody(c);
      const now = Math.floor(Date.now() / 1_000);
      const generation = await accountGeneration(c.env, context.uid, now);
      const snapshot = await readSourceSnapshot(c.env, context.uid, year);
      const requestFingerprint = await sha256Hex(`wrapped\0${context.uid}\0${year}`);
      const jobId = `wrapped-${year}-${requestFingerprint.slice(0, 48)}`;
      await c.env.APP_DB.prepare(
        "INSERT INTO cf_wrapped_jobs (uid, year, job_id, request_fingerprint, source_fingerprint, account_generation, status, attempts, next_attempt_at, created_at, updated_at) " +
          "VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?) ON CONFLICT(uid, year) DO NOTHING",
      )
        .bind(context.uid, year, jobId, requestFingerprint, snapshot.fingerprint, generation, now, now, now)
        .run();
      let row = await readJob(c.env, context.uid, year);
      if (!row) return c.json({ error: "wrapped_job_unavailable" }, 503);
      if (row.status === "completed")
        return c.json(publicGenerate(row, `Your Wrapped ${year} is already generated`));
      const stale = row.status === "running" && (row.lease_until === null || row.lease_until <= now);
      const reset = row.source_fingerprint !== snapshot.fingerprint || row.status === "failed" || stale;
      if (reset) {
        await c.env.APP_DB.prepare(
          "UPDATE cf_wrapped_jobs SET source_fingerprint = ?, account_generation = ?, status = 'queued', attempts = 0, lease_token = NULL, lease_until = NULL, next_attempt_at = ?, result_json = NULL, last_error = NULL, updated_at = ? WHERE uid = ? AND year = ? AND status <> 'completed'",
        )
          .bind(snapshot.fingerprint, generation, now, now, context.uid, year)
          .run();
        row = await readJob(c.env, context.uid, year);
        if (!row) return c.json({ error: "wrapped_job_unavailable" }, 503);
      }
      if (row.status === "running")
        return c.json(publicGenerate(row, "Generation is already in progress"));
      await enqueue(c.env, row);
      return c.json(publicGenerate(row, "Starting Wrapped 2025 generation..."));
    } catch (error) {
      if (error instanceof WrappedHttpError)
        return c.json({ error: error.code, message: error.message }, error.status as 400);
      if (error instanceof WrappedTerminalError)
        return c.json({ error: "wrapped_source_unavailable", message: error.message }, 503);
      return c.json({ error: "wrapped_unavailable" }, 503);
    }
  });
}

function parseQueuePayload(payload: Record<string, unknown>): {
  year: number;
  requestFingerprint: string;
  sourceFingerprint: string;
  accountGeneration: number;
} | null {
  const year = finiteInteger(payload.year, 2000);
  const accountGeneration = finiteInteger(payload.accountGeneration, 0);
  const requestFingerprint = typeof payload.requestFingerprint === "string" ? payload.requestFingerprint : "";
  const sourceFingerprint = typeof payload.sourceFingerprint === "string" ? payload.sourceFingerprint : "";
  if (year !== SUPPORTED_YEAR || accountGeneration === null || !/^[a-f0-9]{64}$/.test(requestFingerprint) || !/^[a-f0-9]{64}$/.test(sourceFingerprint)) return null;
  return { year, requestFingerprint, sourceFingerprint, accountGeneration };
}

async function markFailed(env: JobsEnv, row: WrappedJobRow, reason: string, now: number): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_wrapped_jobs SET status = 'failed', lease_token = NULL, lease_until = NULL, last_error = ?, updated_at = ? " +
      "WHERE uid = ? AND year = ? AND status = 'running' AND lease_token = ?",
  )
    .bind(reason.slice(0, 2_048), now, row.uid, row.year, row.lease_token)
    .run();
}

async function markRetry(env: JobsEnv, row: WrappedJobRow, reason: string, now: number): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_wrapped_jobs SET status = 'queued', lease_token = NULL, lease_until = NULL, next_attempt_at = ?, last_error = ?, updated_at = ? " +
      "WHERE uid = ? AND year = ? AND status = 'running' AND lease_token = ?",
  )
    .bind(now + RETRY_DELAY_SECONDS, reason.slice(0, 2_048), now, row.uid, row.year, row.lease_token)
    .run();
}

async function processWrapped(
  env: JobsEnv,
  message: Message<JobMessage>,
): Promise<void> {
  const payload = parseQueuePayload(message.body.payload);
  if (!payload) {
    message.ack();
    return;
  }
  let current = await readJob(env, message.body.uid, payload.year);
  if (!current || current.job_id !== message.body.jobId || current.request_fingerprint !== payload.requestFingerprint || current.source_fingerprint !== payload.sourceFingerprint || current.account_generation !== payload.accountGeneration) {
    message.ack();
    return;
  }
  if (current.status === "completed") {
    message.ack();
    return;
  }
  const now = Math.floor(Date.now() / 1_000);
  const leaseToken = crypto.randomUUID();
  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_wrapped_jobs SET status = 'running', attempts = attempts + 1, lease_token = ?, lease_until = ?, updated_at = ? " +
      "WHERE uid = ? AND year = ? AND ((status = 'queued' AND next_attempt_at <= ?) OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?))",
  )
    .bind(leaseToken, now + LEASE_SECONDS, now, current.uid, current.year, now, now)
    .run();
  if (claimed.meta?.changes !== 1) {
    message.ack();
    return;
  }
  current = await readJob(env, message.body.uid, payload.year);
  if (!current || current.lease_token !== leaseToken) {
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
    return;
  }
  try {
    const generation = await accountGeneration(env, current.uid, now);
    if (generation !== current.account_generation) throw new WrappedTerminalError("wrapped account generation changed");
    const snapshot = await readSourceSnapshot(env, current.uid, current.year);
    if (snapshot.fingerprint !== current.source_fingerprint) throw new WrappedTerminalError("wrapped source changed during generation");
    const stats = computeStats(snapshot);
    const provider = await generateProviderResult(env, snapshot, stats);
    const result = { ...stats, ...provider };
    const resultJson = JSON.stringify(result);
    if (new TextEncoder().encode(resultJson).byteLength > MAX_RESULT_BYTES) throw new WrappedTerminalError("wrapped result is too large");
    const completed = await env.APP_DB.batch([
      env.APP_DB.prepare(
        "UPDATE cf_wrapped_jobs SET status = 'completed', lease_token = NULL, lease_until = NULL, result_json = ?, last_error = NULL, updated_at = ? WHERE uid = ? AND year = ? AND status = 'running' AND lease_token = ?",
      ).bind(resultJson, now, current.uid, current.year, leaseToken),
      env.APP_DB.prepare(
        "INSERT INTO cf_notification_outbox (notification_id, source_kind, source_id, uid, title, body, data_json, status, attempts, not_before, created_at, updated_at) VALUES (?, 'integration', ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?) ON CONFLICT(source_kind, source_id) DO NOTHING",
      ).bind(
        `wn-${current.request_fingerprint.slice(0, 48)}`,
        `wrapped:${current.job_id}`,
        current.uid,
        "omi",
        `Your Wrapped ${current.year} is ready! 🎁`,
        JSON.stringify({ type: "wrapped_ready", year: String(current.year), navigate_to: `/wrapped/${current.year}` }),
        now,
        now,
        now,
      ),
    ]);
    if (Number(completed[0]?.meta?.changes || 0) !== 1) {
      message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
      return;
    }
    message.ack();
  } catch (error) {
    if (error instanceof WrappedTerminalError) {
      await markFailed(env, current, error.message, now);
      message.ack();
      return;
    }
    if (current.attempts >= MAX_ATTEMPTS) {
      await markFailed(env, current, "wrapped generation failed after retry budget", now);
      message.ack();
      return;
    }
    await markRetry(env, current, "wrapped generation unavailable", now);
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
  }
}

export async function processWrappedJobMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  await processWrapped(env, message);
}
