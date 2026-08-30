import { publicHttpsUrl } from "./app-mutations";
import type { JobsEnv } from "./env";

const BATCH_SIZE = 25;
const LEASE_SECONDS = 5 * 60;
const MAX_ATTEMPTS = 10;
const RETENTION_SECONDS = 7 * 24 * 60 * 60;
const RETRYABLE_STATUSES = new Set([408, 425, 429]);

type DeveloperWebhookRow = {
  delivery_id: string;
  uid: string;
  webhook_type: "memory_created";
  conversation_id: string;
  webhook_url: string;
  payload_json: string;
  attempts: number;
};

function retryDelay(attempts: number) {
  return Math.min(6 * 60 * 60, 30 * 2 ** Math.min(attempts, 10));
}

async function markFailure(
  env: JobsEnv,
  row: DeveloperWebhookRow,
  now: number,
  retryable: boolean,
  error: string,
) {
  const attempts = Number(row.attempts || 0) + 1;
  const retry = retryable && attempts < MAX_ATTEMPTS;
  await env.APP_DB.prepare(
    "UPDATE cf_developer_webhook_outbox SET status = ?, not_before = ?, lease_until = NULL, " +
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
  row: DeveloperWebhookRow,
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
        "x-omi-webhook-type": row.webhook_type,
      },
      body: row.payload_json,
      redirect: "manual",
      signal: AbortSignal.timeout(30_000),
    });
  } catch {
    await markFailure(env, row, now, true, "webhook request failed");
    return;
  }
  await response.body?.cancel();
  if (!response.ok) {
    await markFailure(
      env,
      row,
      now,
      response.status >= 500 || RETRYABLE_STATUSES.has(response.status),
      `HTTP ${response.status}`,
    );
    return;
  }
  await env.APP_DB.prepare(
    "UPDATE cf_developer_webhook_outbox SET status = 'sent', lease_until = NULL, last_error = NULL, " +
      "updated_at = ? WHERE delivery_id = ? AND status = 'sending'",
  )
    .bind(now, row.delivery_id)
    .run();
}

export async function drainDeveloperWebhooks(
  env: JobsEnv,
  now = Math.floor(Date.now() / 1_000),
  fetcher: typeof fetch = fetch,
) {
  await env.APP_DB.prepare(
    "UPDATE cf_developer_webhook_outbox SET status = 'failed', lease_until = NULL, " +
      "last_error = COALESCE(last_error, 'retry limit exceeded'), updated_at = ? WHERE attempts >= ? AND " +
      "((status = 'pending' AND not_before <= ?) OR " +
      "(status = 'sending' AND COALESCE(lease_until, 0) <= ?))",
  )
    .bind(now, MAX_ATTEMPTS, now, now)
    .run();
  const result = await env.APP_DB.prepare(
    "SELECT delivery_id, uid, webhook_type, conversation_id, webhook_url, payload_json, attempts " +
      "FROM cf_developer_webhook_outbox WHERE attempts < ? AND " +
      "((status = 'pending' AND not_before <= ?) OR " +
      "(status = 'sending' AND COALESCE(lease_until, 0) <= ?)) " +
      "ORDER BY created_at ASC, delivery_id ASC LIMIT ?",
  )
    .bind(MAX_ATTEMPTS, now, now, BATCH_SIZE)
    .all<DeveloperWebhookRow>();
  for (const row of result.results || []) {
    const leased = await env.APP_DB.prepare(
      "UPDATE cf_developer_webhook_outbox SET status = 'sending', attempts = attempts + 1, lease_until = ?, " +
        "updated_at = ? WHERE delivery_id = ? AND attempts < ? AND " +
        "((status = 'pending' AND not_before <= ?) OR " +
        "(status = 'sending' AND COALESCE(lease_until, 0) <= ?))",
    )
      .bind(now + LEASE_SECONDS, now, row.delivery_id, MAX_ATTEMPTS, now, now)
      .run();
    if (leased.meta?.changes !== 1) continue;
    await deliver(env, row, now, fetcher);
  }
  await env.APP_DB.prepare(
    "DELETE FROM cf_developer_webhook_outbox WHERE status IN ('sent', 'failed') AND updated_at < ?",
  )
    .bind(now - RETENTION_SECONDS)
    .run();
}
