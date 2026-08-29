import { publicHttpsUrl } from "./app-mutations";
import type { JobsEnv } from "./env";

const BATCH_SIZE = 25;
const LEASE_SECONDS = 5 * 60;
const MAX_ATTEMPTS = 10;
const MAX_RESPONSE_BYTES = 16_000;
const RETENTION_SECONDS = 7 * 24 * 60 * 60;
const RETRYABLE_STATUSES = new Set([408, 425, 429]);

type WebhookRow = {
  delivery_id: string;
  app_id: string;
  uid: string;
  conversation_id: string;
  webhook_url: string;
  payload_json: string;
  attempts: number;
};

function retryDelay(attempts: number) {
  return Math.min(6 * 60 * 60, 30 * 2 ** Math.min(attempts, 10));
}

async function boundedResponseJson(response: Response) {
  const declared = Number(response.headers.get("content-length"));
  if (
    Number.isFinite(declared) &&
    (declared < 0 || declared > MAX_RESPONSE_BYTES)
  ) {
    await response.body?.cancel();
    return null;
  }
  if (!response.body) return null;
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      size += result.value.byteLength;
      if (size > MAX_RESPONSE_BYTES) {
        await reader.cancel();
        return null;
      }
      chunks.push(result.value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    const value = JSON.parse(new TextDecoder().decode(bytes)) as unknown;
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

async function finishWithoutMessage(
  env: JobsEnv,
  deliveryId: string,
  now: number,
) {
  await env.APP_DB.prepare(
    "UPDATE cf_integration_webhook_outbox SET status = 'sent', lease_until = NULL, " +
      "last_error = NULL, updated_at = ? WHERE delivery_id = ? AND status = 'sending'",
  )
    .bind(now, deliveryId)
    .run();
}

async function finishWithMessage(
  env: JobsEnv,
  row: WebhookRow,
  message: string,
  now: number,
) {
  const session = await env.APP_DB.prepare(
    "SELECT id FROM cf_chat_sessions WHERE uid = ? AND app_id = ? " +
      "ORDER BY updated_at DESC, id DESC LIMIT 1",
  )
    .bind(row.uid, row.app_id)
    .first<{ id?: unknown }>();
  const sessionId =
    typeof session?.id === "string" ? session.id : crypto.randomUUID();
  const messageId = crypto.randomUUID();
  const createdAt = new Date(now * 1_000).toISOString();
  const messageJson = JSON.stringify({
    id: messageId,
    text: message,
    created_at: createdAt,
    sender: "ai",
    type: "text",
    app_id: row.app_id,
    plugin_id: row.app_id,
    session_id: sessionId,
    chat_session_id: sessionId,
    from_external_integration: false,
    rating: null,
    reported: false,
    memories_id: [row.conversation_id],
    memories: [],
    files_id: [],
    files: [],
    metadata: {},
    content_blocks: [],
  });
  const statements = [];
  if (typeof session?.id !== "string") {
    statements.push(
      env.APP_DB.prepare(
        "INSERT INTO cf_chat_sessions " +
          "(uid, id, title, preview, created_at, updated_at, app_id, message_count, starred) " +
          "VALUES (?, ?, 'New Chat', NULL, ?, ?, ?, 0, 0)",
      ).bind(row.uid, sessionId, now, now, row.app_id),
    );
  }
  statements.push(
    env.APP_DB.prepare(
      "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) VALUES (?, ?, ?, ?, ?)",
    ).bind(
      row.uid,
      messageId,
      row.app_id,
      Date.now() * 1_000 * 2,
      messageJson,
    ),
    env.APP_DB.prepare(
      "UPDATE cf_chat_sessions SET updated_at = ?, message_count = message_count + 1, preview = ? " +
        "WHERE uid = ? AND id = ?",
    ).bind(now, message.slice(0, 100), row.uid, sessionId),
    env.APP_DB.prepare(
      "UPDATE cf_integration_webhook_outbox SET status = 'sent', lease_until = NULL, " +
        "last_error = NULL, updated_at = ? WHERE delivery_id = ? AND status = 'sending'",
    ).bind(now, row.delivery_id),
  );
  await env.APP_DB.batch(statements);
}

async function markFailure(
  env: JobsEnv,
  row: WebhookRow,
  now: number,
  retryable: boolean,
  error: string,
) {
  const attempts = Number(row.attempts || 0) + 1;
  const retry = retryable && attempts < MAX_ATTEMPTS;
  await env.APP_DB.prepare(
    "UPDATE cf_integration_webhook_outbox SET status = ?, not_before = ?, lease_until = NULL, " +
      "last_error = ?, updated_at = ? WHERE delivery_id = ? AND status = 'sending'",
  )
    .bind(
      retry ? "pending" : "failed",
      retry ? now + retryDelay(attempts) : now,
      error.slice(0, 200),
      now,
      row.delivery_id,
    )
    .run();
}

async function deliver(
  env: JobsEnv,
  row: WebhookRow,
  now: number,
  fetcher: typeof fetch,
) {
  let url: URL;
  try {
    if (!publicHttpsUrl(row.webhook_url)) {
      await markFailure(env, row, now, false, "unsafe webhook URL");
      return;
    }
    url = new URL(row.webhook_url);
    url.searchParams.set("uid", row.uid);
  } catch {
    await markFailure(env, row, now, false, "invalid webhook URL");
    return;
  }
  let response: Response;
  try {
    response = await fetcher(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-omi-idempotency-key": row.delivery_id,
      },
      body: row.payload_json,
      redirect: "manual",
      signal: AbortSignal.timeout(30_000),
    });
  } catch {
    await markFailure(env, row, now, true, "webhook request failed");
    return;
  }
  if (!response.ok) {
    await response.body?.cancel();
    const retryable =
      response.status >= 500 || RETRYABLE_STATUSES.has(response.status);
    await markFailure(env, row, now, retryable, `HTTP ${response.status}`);
    return;
  }
  const body = await boundedResponseJson(response);
  const message = body?.message;
  if (typeof message === "string" && message.trim().length > 5) {
    await finishWithMessage(env, row, message.trim().slice(0, 2_000), now);
  } else {
    await finishWithoutMessage(env, row.delivery_id, now);
  }
}

export async function drainIntegrationWebhooks(
  env: JobsEnv,
  now = Math.floor(Date.now() / 1_000),
  fetcher: typeof fetch = fetch,
) {
  await env.APP_DB.prepare(
    "UPDATE cf_integration_webhook_outbox SET status = 'failed', lease_until = NULL, " +
      "last_error = COALESCE(last_error, 'retry limit exceeded'), updated_at = ? WHERE attempts >= ? AND " +
      "((status = 'pending' AND not_before <= ?) OR " +
      "(status = 'sending' AND COALESCE(lease_until, 0) <= ?))",
  )
    .bind(now, MAX_ATTEMPTS, now, now)
    .run();
  const result = await env.APP_DB.prepare(
    "SELECT delivery_id, app_id, uid, conversation_id, webhook_url, payload_json, attempts " +
      "FROM cf_integration_webhook_outbox WHERE " +
      "attempts < ? AND ((status = 'pending' AND not_before <= ?) OR " +
      "(status = 'sending' AND COALESCE(lease_until, 0) <= ?)) " +
      "ORDER BY created_at ASC, delivery_id ASC LIMIT ?",
  )
    .bind(MAX_ATTEMPTS, now, now, BATCH_SIZE)
    .all<WebhookRow>();
  for (const row of result.results || []) {
    const leased = await env.APP_DB.prepare(
      "UPDATE cf_integration_webhook_outbox SET status = 'sending', attempts = attempts + 1, " +
        "lease_until = ?, updated_at = ? WHERE delivery_id = ? AND " +
        "attempts < ? AND ((status = 'pending' AND not_before <= ?) OR " +
        "(status = 'sending' AND COALESCE(lease_until, 0) <= ?))",
    )
      .bind(
        now + LEASE_SECONDS,
        now,
        row.delivery_id,
        MAX_ATTEMPTS,
        now,
        now,
      )
      .run();
    if (leased.meta?.changes !== 1) continue;
    await deliver(env, row, now, fetcher);
  }
  await env.APP_DB.prepare(
    "DELETE FROM cf_integration_webhook_outbox WHERE status IN ('sent', 'failed') AND updated_at < ?",
  )
    .bind(now - RETENTION_SECONDS)
    .run();
}
