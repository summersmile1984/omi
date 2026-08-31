import type { Message } from "@cloudflare/workers-types";
import type { Context, Hono } from "hono";
import type { JobMessage, JobsEnv } from "./env";

const OPENAI_API_BASE = "https://api.openai.com/v1";
const PROVIDER_RETRIES = 2;
const RUN_LEASE_SECONDS = 120;
const POLL_RETRY_SECONDS = 5;
const MAX_POLL_ATTEMPTS = 12;
const MAX_TEXT_BYTES = 64_000;
const MAX_IDEMPOTENCY_BYTES = 300;
const MAX_ATTACHMENTS = 20;

type JobsContext = Context<{ Bindings: JobsEnv }>;
type AuthContext = { uid: string; authority?: string };

type AssistantSessionRow = {
  uid: string;
  session_id: string;
  provider: "openai-assistants";
  thread_id: string;
  assistant_id: string;
  status: "active" | "failed" | "deleted";
  generation: number;
  created_at: number;
  updated_at: number;
  last_error: string | null;
};

type AssistantRunRow = {
  uid: string;
  run_id: string;
  session_id: string;
  provider: "openai-assistants";
  idempotency_key: string;
  request_fingerprint: string;
  provider_message_id: string | null;
  provider_run_id: string | null;
  status: "staging" | "queued" | "in_progress" | "completed" | "failed" | "cancelled";
  attempts: number;
  lease_token: string | null;
  lease_until: number | null;
  next_attempt_at: number;
  result_json: string | null;
  last_error: string | null;
  created_at: number;
  updated_at: number;
};

export class ChatAssistantProviderError extends Error {
  constructor(
    readonly code: "provider_unavailable" | "provider_not_configured" | "provider_rejected" | "invalid_provider_response",
    message: string,
    readonly retryable = false,
  ) {
    super(message);
  }
}

function validSegment(value: string, maximum: number): boolean {
  return value.length > 0 && value.length <= maximum && !/[\\/\0]/.test(value);
}

function providerId(value: unknown, prefix: string): string {
  const id = typeof value === "string" ? value : "";
  if (!new RegExp(`^${prefix}[A-Za-z0-9_-]{1,256}$`).test(id))
    throw new ChatAssistantProviderError(
      "invalid_provider_response",
      `provider returned no ${prefix} id`,
    );
  return id;
}

function transientStatus(status: number): boolean {
  return status === 408 || status === 409 || status === 425 || status === 429 || status >= 500;
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function providerJson(
  env: JobsEnv,
  pathname: string,
  init: RequestInit,
  idempotencyKey?: string,
): Promise<Record<string, unknown>> {
  const apiKey = String(env.OPENAI_API_KEY || "").trim();
  if (!apiKey)
    throw new ChatAssistantProviderError(
      "provider_not_configured",
      "OpenAI Assistants provider is not configured",
    );
  const headers = new Headers(init.headers);
  headers.set("authorization", `Bearer ${apiKey}`);
  headers.set("accept", "application/json");
  headers.set("openai-beta", "assistants=v2");
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  if (idempotencyKey) headers.set("idempotency-key", idempotencyKey);

  let lastError: ChatAssistantProviderError | null = null;
  for (let attempt = 0; attempt <= PROVIDER_RETRIES; attempt += 1) {
    let response: Response;
    try {
      response = await fetch(`${OPENAI_API_BASE}${pathname}`, {
        ...init,
        headers,
      });
    } catch {
      lastError = new ChatAssistantProviderError(
        "provider_unavailable",
        "OpenAI Assistants provider is unavailable",
        true,
      );
      continue;
    }
    if (response.ok) {
      try {
        const payload = await response.json();
        if (payload && typeof payload === "object" && !Array.isArray(payload))
          return payload as Record<string, unknown>;
      } catch {
        // Fall through to the stable provider error below.
      }
      throw new ChatAssistantProviderError(
        "invalid_provider_response",
        "OpenAI Assistants provider returned invalid JSON",
      );
    }
    const retryable = transientStatus(response.status);
    lastError = new ChatAssistantProviderError(
      retryable ? "provider_unavailable" : "provider_rejected",
      retryable
        ? "OpenAI Assistants provider is temporarily unavailable"
        : "OpenAI Assistants provider rejected the request",
      retryable,
    );
    if (!retryable) throw lastError;
  }
  throw lastError || new ChatAssistantProviderError("provider_unavailable", "OpenAI Assistants provider is unavailable", true);
}

async function providerDelete(env: JobsEnv, pathname: string): Promise<void> {
  const apiKey = String(env.OPENAI_API_KEY || "").trim();
  if (!apiKey)
    throw new ChatAssistantProviderError(
      "provider_not_configured",
      "OpenAI Assistants provider is not configured",
    );
  let response: Response;
  try {
    response = await fetch(`${OPENAI_API_BASE}${pathname}`, {
      method: "DELETE",
      headers: {
        authorization: `Bearer ${apiKey}`,
        accept: "application/json",
        "openai-beta": "assistants=v2",
      },
    });
  } catch {
    throw new ChatAssistantProviderError("provider_unavailable", "OpenAI Assistants provider is unavailable", true);
  }
  if (!response.ok && response.status !== 404)
    throw new ChatAssistantProviderError("provider_rejected", "OpenAI Assistants provider rejected deletion");
}

function assistantId(env: JobsEnv): string {
  const id = String(env.OPENAI_ASSISTANT_ID || "").trim();
  if (!/^asst-[A-Za-z0-9_-]{1,256}$/.test(id))
    throw new ChatAssistantProviderError(
      "provider_not_configured",
      "OPENAI_ASSISTANT_ID is not configured",
    );
  return id;
}

async function existingSession(env: JobsEnv, uid: string, sessionId: string): Promise<AssistantSessionRow | null> {
  return env.APP_DB.prepare(
    "SELECT * FROM cf_chat_assistant_sessions WHERE uid = ? AND session_id = ?",
  )
    .bind(uid, sessionId)
    .first<AssistantSessionRow>();
}

export async function ensureAssistantSession(
  env: JobsEnv,
  uid: string,
  sessionId: string,
  now = Math.floor(Date.now() / 1000),
): Promise<AssistantSessionRow> {
  if (!validSegment(uid, 256) || !validSegment(sessionId, 256))
    throw new ChatAssistantProviderError("provider_rejected", "invalid chat session");
  const existing = await existingSession(env, uid, sessionId);
  if (existing?.status === "active") return existing;
  const assistant = assistantId(env);
  const thread = await providerJson(env, "/threads", { method: "POST", body: "{}" }, `chat-thread:${uid}:${sessionId}`);
  const threadId = providerId(thread.id, "thread-");
  const inserted = await env.APP_DB.prepare(
    "INSERT OR IGNORE INTO cf_chat_assistant_sessions (uid, session_id, provider, thread_id, assistant_id, status, generation, created_at, updated_at, last_error) VALUES (?, ?, 'openai-assistants', ?, ?, 'active', 1, ?, ?, NULL)",
  )
    .bind(uid, sessionId, threadId, assistant, now, now)
    .run();
  if (inserted.meta?.changes !== 1) {
    const winner = await existingSession(env, uid, sessionId);
    try {
      await providerDelete(env, `/threads/${encodeURIComponent(threadId)}`);
    } catch {
      // A residual provider thread cannot become authoritative without a D1 row.
    }
    if (winner?.status === "active") return winner;
    throw new ChatAssistantProviderError("provider_unavailable", "chat provider session could not be persisted");
  }
  return {
    uid,
    session_id: sessionId,
    provider: "openai-assistants",
    thread_id: threadId,
    assistant_id: assistant,
    status: "active",
    generation: 1,
    created_at: now,
    updated_at: now,
    last_error: null,
  };
}

type ReadyAttachment = {
  file_id: string;
  provider_file_id: string;
  mime_type: string;
};

async function readyAttachments(env: JobsEnv, uid: string, sessionId: string, fileIds: string[]): Promise<ReadyAttachment[]> {
  if (!fileIds.length || fileIds.length > MAX_ATTACHMENTS || new Set(fileIds).size !== fileIds.length)
    throw new ChatAssistantProviderError("provider_rejected", "invalid chat attachments");
  const placeholders = fileIds.map(() => "?").join(", ");
  const result = await env.APP_DB.prepare(
    "SELECT sf.file_id, f.provider_file_id, f.mime_type FROM cf_chat_session_files sf JOIN cf_chat_files f ON f.uid = sf.uid AND f.file_id = sf.file_id WHERE sf.uid = ? AND sf.session_id = ? AND f.status = 'ready' AND f.provider_file_id IS NOT NULL AND sf.file_id IN (" +
      placeholders +
      ")",
  )
    .bind(uid, sessionId, ...fileIds)
    .all<ReadyAttachment>();
  const rows = result.results || [];
  if (rows.length !== fileIds.length)
    throw new ChatAssistantProviderError("provider_rejected", "chat attachment is not ready");
  const byId = new Map(rows.map((row) => [row.file_id, row]));
  return fileIds.map((fileId) => {
    const row = byId.get(fileId);
    if (!row || !/^file-[A-Za-z0-9_-]{1,256}$/.test(row.provider_file_id))
      throw new ChatAssistantProviderError("provider_rejected", "chat attachment provider id is invalid");
    return row;
  });
}

function assistantMessage(text: string, attachments: ReadyAttachment[]): Record<string, unknown> {
  const content: Record<string, unknown>[] = [{ type: "text", text }];
  const fileAttachments: Record<string, unknown>[] = [];
  for (const file of attachments) {
    if (file.mime_type.startsWith("image/")) {
      content.push({ type: "image_file", image_file: { file_id: file.provider_file_id, detail: "auto" } });
    } else {
      fileAttachments.push({ file_id: file.provider_file_id, tools: [{ type: "file_search" }] });
    }
  }
  return {
    role: "user",
    content,
    ...(fileAttachments.length ? { attachments: fileAttachments } : {}),
  };
}

async function existingRun(env: JobsEnv, uid: string, idempotencyKey: string): Promise<AssistantRunRow | null> {
  return env.APP_DB.prepare(
    "SELECT * FROM cf_chat_assistant_runs WHERE uid = ? AND idempotency_key = ?",
  )
    .bind(uid, idempotencyKey)
    .first<AssistantRunRow>();
}

async function runRow(env: JobsEnv, uid: string, runId: string): Promise<AssistantRunRow | null> {
  return env.APP_DB.prepare(
    "SELECT * FROM cf_chat_assistant_runs WHERE uid = ? AND run_id = ?",
  )
    .bind(uid, runId)
    .first<AssistantRunRow>();
}

async function runRowForSession(
  env: JobsEnv,
  uid: string,
  sessionId: string,
  runId: string,
): Promise<AssistantRunRow | null> {
  return env.APP_DB.prepare(
    "SELECT * FROM cf_chat_assistant_runs WHERE uid = ? AND session_id = ? AND run_id = ?",
  )
    .bind(uid, sessionId, runId)
    .first<AssistantRunRow>();
}

function publicRun(row: AssistantRunRow): Record<string, unknown> {
  let result: unknown;
  if (row.result_json) {
    try {
      result = JSON.parse(row.result_json);
    } catch {
      result = undefined;
    }
  }
  return {
    run_id: row.run_id,
    session_id: row.session_id,
    status: row.status,
    attempts: row.attempts,
    ...(row.provider_run_id ? { provider_run_id: row.provider_run_id } : {}),
    ...(result === undefined ? {} : { result }),
    last_error: row.last_error,
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
}

function providerRunStatus(value: unknown): AssistantRunRow["status"] {
  if (value === "queued" || value === "in_progress" || value === "completed" || value === "failed" || value === "cancelled") return value;
  if (value === "expired" || value === "requires_action") return "failed";
  return "in_progress";
}

export async function createAssistantRun(
  env: JobsEnv,
  uid: string,
  sessionId: string,
  idempotencyKey: string,
  text: string,
  fileIds: string[],
  now = Math.floor(Date.now() / 1000),
): Promise<Record<string, unknown>> {
  if (!validSegment(uid, 256) || !validSegment(sessionId, 256) || !validSegment(idempotencyKey, MAX_IDEMPOTENCY_BYTES))
    throw new ChatAssistantProviderError("provider_rejected", "invalid chat assistant run request");
  if (!text.trim() || new TextEncoder().encode(text).byteLength > MAX_TEXT_BYTES)
    throw new ChatAssistantProviderError("provider_rejected", "chat question is invalid");
  const fingerprint = await sha256Hex(`${sessionId}\0${text}\0${fileIds.join("\0")}`);
  const existing = await existingRun(env, uid, idempotencyKey);
  if (existing) {
    if (existing.request_fingerprint !== fingerprint)
      throw new ChatAssistantProviderError("provider_rejected", "idempotency key reused with different payload");
    return { ...publicRun(existing), created: false };
  }
  const attachments = await readyAttachments(env, uid, sessionId, fileIds);
  const runId = crypto.randomUUID();
  const inserted = await env.APP_DB.prepare(
    "INSERT OR IGNORE INTO cf_chat_assistant_runs (uid, run_id, session_id, provider, idempotency_key, request_fingerprint, provider_message_id, provider_run_id, status, attempts, lease_token, lease_until, next_attempt_at, result_json, last_error, created_at, updated_at) VALUES (?, ?, ?, 'openai-assistants', ?, ?, NULL, NULL, 'staging', 0, NULL, NULL, ?, NULL, NULL, ?, ?)",
  )
    .bind(uid, runId, sessionId, idempotencyKey, fingerprint, now, now, now)
    .run();
  if (inserted.meta?.changes !== 1) {
    const winner = await existingRun(env, uid, idempotencyKey);
    if (!winner) throw new ChatAssistantProviderError("provider_unavailable", "chat assistant run could not be persisted");
    if (winner.request_fingerprint !== fingerprint)
      throw new ChatAssistantProviderError("provider_rejected", "idempotency key reused with different payload");
    return { ...publicRun(winner), created: false };
  }

  const leaseToken = crypto.randomUUID();
  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_chat_assistant_runs SET attempts = attempts + 1, lease_token = ?, lease_until = ?, updated_at = ? WHERE uid = ? AND run_id = ? AND status = 'staging'",
  )
    .bind(leaseToken, now + RUN_LEASE_SECONDS, now, uid, runId)
    .run();
  if (claimed.meta?.changes !== 1)
    throw new ChatAssistantProviderError("provider_unavailable", "chat assistant run lease unavailable", true);

  try {
    const session = await ensureAssistantSession(env, uid, sessionId, now);
    const message = await providerJson(
      env,
      `/threads/${encodeURIComponent(session.thread_id)}/messages`,
      { method: "POST", body: JSON.stringify(assistantMessage(text, attachments)) },
      `${idempotencyKey}:message`,
    );
    const providerMessageId = providerId(message.id, "msg-");
    await env.APP_DB.prepare(
      "UPDATE cf_chat_assistant_runs SET provider_message_id = ?, updated_at = ? WHERE uid = ? AND run_id = ? AND lease_token = ?",
    )
      .bind(providerMessageId, now, uid, runId, leaseToken)
      .run();
    const run = await providerJson(
      env,
      `/threads/${encodeURIComponent(session.thread_id)}/runs`,
      { method: "POST", body: JSON.stringify({ assistant_id: session.assistant_id }) },
      `${idempotencyKey}:run`,
    );
    const providerRunId = providerId(run.id, "run-");
    const status = providerRunStatus(run.status);
    await env.APP_DB.prepare(
      "UPDATE cf_chat_assistant_runs SET provider_run_id = ?, status = ?, lease_token = NULL, lease_until = NULL, last_error = NULL, updated_at = ? WHERE uid = ? AND run_id = ? AND lease_token = ?",
    )
      .bind(providerRunId, status, now, uid, runId, leaseToken)
      .run();
    const saved = await runRow(env, uid, runId);
    if (!saved) throw new ChatAssistantProviderError("provider_unavailable", "chat assistant run disappeared");
    return { ...publicRun(saved), created: true };
  } catch (error) {
    const providerError = error instanceof ChatAssistantProviderError ? error : new ChatAssistantProviderError("provider_unavailable", "chat assistant provider unavailable", true);
    await env.APP_DB.prepare(
      "UPDATE cf_chat_assistant_runs SET status = 'failed', lease_token = NULL, lease_until = NULL, last_error = ?, updated_at = ? WHERE uid = ? AND run_id = ? AND lease_token = ?",
    )
      .bind(providerError.message.slice(0, 2048), now, uid, runId, leaseToken)
      .run();
    throw providerError;
  }
}

function assistantText(payload: Record<string, unknown>): string | null {
  const data = Array.isArray(payload.data) ? payload.data : [];
  for (const item of data) {
    if (!item || typeof item !== "object") continue;
    const message = item as Record<string, unknown>;
    if (message.role !== "assistant" || !Array.isArray(message.content)) continue;
    for (const block of message.content) {
      if (!block || typeof block !== "object") continue;
      const text = (block as Record<string, unknown>).text;
      if (text && typeof text === "object" && typeof (text as Record<string, unknown>).value === "string")
        return String((text as Record<string, unknown>).value).slice(0, MAX_TEXT_BYTES);
    }
  }
  return null;
}

export async function pollAssistantRun(
  env: JobsEnv,
  uid: string,
  sessionId: string,
  runId: string,
  now = Math.floor(Date.now() / 1000),
): Promise<Record<string, unknown>> {
  if (!validSegment(sessionId, 256))
    throw new ChatAssistantProviderError("provider_rejected", "invalid chat session");
  const row = await runRowForSession(env, uid, sessionId, runId);
  if (!row) throw new ChatAssistantProviderError("provider_rejected", "chat assistant run not found");
  if (!row.provider_run_id || row.status === "completed" || row.status === "failed" || row.status === "cancelled") return publicRun(row);
  const session = await existingSession(env, uid, row.session_id);
  if (!session || session.status !== "active") throw new ChatAssistantProviderError("provider_rejected", "chat assistant session not found");
  try {
    const providerRun = await providerJson(
      env,
      `/threads/${encodeURIComponent(session.thread_id)}/runs/${encodeURIComponent(row.provider_run_id)}`,
      { method: "GET" },
    );
    const status = providerRunStatus(providerRun.status);
    let resultJson: string | null = null;
    if (status === "completed") {
      const messages = await providerJson(
        env,
        `/threads/${encodeURIComponent(session.thread_id)}/messages?limit=20`,
        { method: "GET" },
      );
      const text = assistantText(messages);
      resultJson = JSON.stringify(text ? { text } : {});
    }
    await env.APP_DB.prepare(
      "UPDATE cf_chat_assistant_runs SET status = ?, result_json = ?, last_error = NULL, lease_token = NULL, lease_until = NULL, updated_at = ? WHERE uid = ? AND run_id = ?",
    )
      .bind(status, resultJson, now, uid, runId)
      .run();
  } catch (error) {
    const providerError = error instanceof ChatAssistantProviderError ? error : new ChatAssistantProviderError("provider_unavailable", "chat assistant provider unavailable", true);
    await env.APP_DB.prepare(
      "UPDATE cf_chat_assistant_runs SET last_error = ?, updated_at = ? WHERE uid = ? AND run_id = ?",
    )
      .bind(providerError.message.slice(0, 2048), now, uid, runId)
      .run();
    throw providerError;
  }
  const updated = await runRow(env, uid, runId);
  if (!updated) throw new ChatAssistantProviderError("provider_unavailable", "chat assistant run disappeared");
  return publicRun(updated);
}

type AssistantPollPayload = {
  sessionId: string;
  runId: string;
};

function pollPayload(value: unknown): AssistantPollPayload | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const payload = value as Partial<AssistantPollPayload>;
  if (
    typeof payload.sessionId !== "string" ||
    typeof payload.runId !== "string" ||
    !validSegment(payload.sessionId, 256) ||
    !validSegment(payload.runId, 128)
  )
    return null;
  return { sessionId: payload.sessionId, runId: payload.runId };
}

/**
 * Queue poller for an admitted provider run.  The initial thread/message/run
 * admission is deliberately synchronous so the caller gets provider ids;
 * this bounded poller keeps completion off the request path.  A terminal
 * provider state is acknowledged, while transient provider failures and
 * in-progress runs are retried by Cloudflare Queues.
 */
export async function processChatAssistantRunMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  const payload = pollPayload(message.body.payload);
  if (!payload) {
    message.ack();
    return;
  }
  const row = await runRowForSession(
    env,
    message.body.uid,
    payload.sessionId,
    payload.runId,
  );
  if (!row || ["completed", "failed", "cancelled"].includes(row.status)) {
    message.ack();
    return;
  }
  const attempts = Math.max(1, Number(message.attempts || 1));
  try {
    const result = await pollAssistantRun(
      env,
      message.body.uid,
      payload.sessionId,
      payload.runId,
    );
    if (["completed", "failed", "cancelled"].includes(String(result.status))) {
      message.ack();
      return;
    }
    if (attempts >= MAX_POLL_ATTEMPTS) {
      await env.APP_DB.prepare(
        "UPDATE cf_chat_assistant_runs SET status = 'failed', last_error = ?, updated_at = ? WHERE uid = ? AND session_id = ? AND run_id = ? AND status NOT IN ('completed', 'failed', 'cancelled')",
      )
        .bind(
          "chat assistant run exceeded poll retry budget",
          Math.floor(Date.now() / 1000),
          message.body.uid,
          payload.sessionId,
          payload.runId,
        )
        .run();
      message.ack();
      return;
    }
    message.retry({ delaySeconds: POLL_RETRY_SECONDS });
  } catch (error) {
    const providerError =
      error instanceof ChatAssistantProviderError
        ? error
        : new ChatAssistantProviderError(
            "provider_unavailable",
            "chat assistant provider unavailable",
            true,
          );
    if (!providerError.retryable || attempts >= MAX_POLL_ATTEMPTS) {
      await env.APP_DB.prepare(
        "UPDATE cf_chat_assistant_runs SET status = 'failed', last_error = ?, updated_at = ? WHERE uid = ? AND session_id = ? AND run_id = ? AND status NOT IN ('completed', 'failed', 'cancelled')",
      )
        .bind(
          providerError.message.slice(0, 2048),
          Math.floor(Date.now() / 1000),
          message.body.uid,
          payload.sessionId,
          payload.runId,
        )
        .run();
      message.ack();
      return;
    }
    message.retry({ delaySeconds: POLL_RETRY_SECONDS });
  }
}

export async function deleteAssistantSession(env: JobsEnv, uid: string, sessionId: string): Promise<void> {
  const session = await existingSession(env, uid, sessionId);
  if (!session) return;
  await providerDelete(env, `/threads/${encodeURIComponent(session.thread_id)}`);
  await env.APP_DB.batch([
    env.APP_DB.prepare("DELETE FROM cf_chat_assistant_runs WHERE uid = ? AND session_id = ?").bind(uid, sessionId),
    env.APP_DB.prepare("DELETE FROM cf_chat_assistant_sessions WHERE uid = ? AND session_id = ?").bind(uid, sessionId),
  ]);
}

function stagingEnabled(c: JobsContext): boolean {
  return c.env.CHAT_ASSISTANT_PROVIDER_STAGING_ENABLED === "true";
}

function errorResponse(c: JobsContext, error: unknown): Response {
  if (error instanceof ChatAssistantProviderError) {
    const status = error.code === "provider_rejected" ? 400 : error.code === "provider_not_configured" ? 503 : 502;
    return c.json({ error: error.code, message: error.message }, status);
  }
  return c.json({ error: "chat_assistant_provider_unavailable" }, 503);
}

export function registerChatAssistantRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: (c: JobsContext) => Promise<AuthContext | null>,
): void {
  app.post("/v2/cf/chat-sessions/:sessionId/assistant-runs", async (c) => {
    if (!stagingEnabled(c)) return c.json({ error: "legacy_route_disabled" }, 404);
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    const sessionId = c.req.param("sessionId");
    const idempotencyKey = c.req.header("idempotency-key")?.trim() || "";
    let payload: { text?: unknown; file_ids?: unknown };
    try {
      const body = await c.req.text();
      if (new TextEncoder().encode(body).byteLength > 128 * 1024)
        return c.json({ error: "request_too_large" }, 413);
      const decoded: unknown = JSON.parse(body);
      if (!decoded || typeof decoded !== "object" || Array.isArray(decoded))
        return c.json({ error: "invalid_request" }, 400);
      payload = decoded as { text?: unknown; file_ids?: unknown };
    } catch {
      return c.json({ error: "invalid_request" }, 400);
    }
    const text = typeof payload.text === "string" ? payload.text : "";
    const fileIds = Array.isArray(payload.file_ids) && payload.file_ids.every((value) => typeof value === "string") ? payload.file_ids : [];
    try {
      const result = await createAssistantRun(c.env, context.uid, sessionId, idempotencyKey, text, fileIds as string[]);
      const runId = typeof result.run_id === "string" ? result.run_id : "";
      if (runId) {
        try {
          await c.env.JOBS.send({
            jobId: runId,
            uid: context.uid,
            kind: "chat_assistant_poll",
            payload: { sessionId, runId },
          });
        } catch {
          // The run remains durable and can still be polled through the GET
          // route; a retry with the same idempotency key re-admits the poll.
          return c.json(
            { ...result, queue_status: "unavailable" },
            503,
          );
        }
      }
      return c.json(result, result.created ? 202 : 200);
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.get("/v2/cf/chat-sessions/:sessionId/assistant-runs/:runId", async (c) => {
    if (!stagingEnabled(c)) return c.json({ error: "legacy_route_disabled" }, 404);
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const result = await pollAssistantRun(
        c.env,
        context.uid,
        c.req.param("sessionId"),
        c.req.param("runId"),
      );
      return c.json(result);
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.delete("/v2/cf/chat-sessions/:sessionId/assistant", async (c) => {
    if (!stagingEnabled(c)) return c.json({ error: "legacy_route_disabled" }, 404);
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      await deleteAssistantSession(c.env, context.uid, c.req.param("sessionId"));
      return c.json({ status: "ok" });
    } catch (error) {
      return errorResponse(c, error);
    }
  });
}
