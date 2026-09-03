import type { JobsEnv } from "./env";

// Port of the legacy webhook_health.py graduated-response contract, keyed by
// a continuous-failure window instead of raw counts: warn the app owner after
// one and two days of unbroken failures and auto-disable delivery after three.
// A success — or an owner webhook-config change — resets the window. The
// drain loop is the single writer, so plain read-then-write is race-safe
// enough here; a duplicated warn in a rare overlap is bounded by the outbox's
// per-window UNIQUE (source_kind, source_id) key.
export const WEBHOOK_HEALTH_DAY1_SECONDS = 86_400;
export const WEBHOOK_HEALTH_DAY2_SECONDS = 172_800;
export const WEBHOOK_HEALTH_DISABLE_SECONDS = 259_200;

const ENDPOINT_INTEGRATION = "integration";

export type WebhookHealthAction = "none" | "day1" | "day2" | "disable";

type HealthRow = {
  first_failure_at: number;
  last_success_at: number | null;
  notified_day1: number;
  notified_day2: number;
  disabled: number;
};

export async function readDisabledWebhookApps(
  env: JobsEnv,
  appIds: string[],
): Promise<Set<string>> {
  const disabled = new Set<string>();
  if (!appIds.length) return disabled;
  const unique = [...new Set(appIds)];
  const placeholders = unique.map(() => "?").join(", ");
  const result = await env.APP_DB.prepare(
    `SELECT app_id FROM cf_app_webhook_health
     WHERE endpoint = '${ENDPOINT_INTEGRATION}' AND disabled = 1 AND app_id IN (${placeholders})`,
  )
    .bind(...unique)
    .all<{ app_id: string }>();
  for (const row of result.results || []) disabled.add(row.app_id);
  return disabled;
}

export async function recordAppWebhookSuccess(
  env: JobsEnv,
  appId: string,
  now: number,
): Promise<void> {
  // Only an existing failure window needs the success stamp; a healthy app
  // keeps zero rows.
  await env.APP_DB.prepare(
    `UPDATE cf_app_webhook_health SET last_success_at = ?, updated_at = ?
     WHERE app_id = ? AND endpoint = '${ENDPOINT_INTEGRATION}' AND disabled = 0`,
  )
    .bind(now, now, appId)
    .run();
}

export async function clearAppWebhookHealth(
  env: JobsEnv,
  appId: string,
): Promise<void> {
  await env.APP_DB.prepare(
    "DELETE FROM cf_app_webhook_health WHERE app_id = ?",
  )
    .bind(appId)
    .run();
}

async function notifyOwner(
  env: JobsEnv,
  appId: string,
  windowStart: number,
  action: Exclude<WebhookHealthAction, "none">,
  now: number,
): Promise<void> {
  const owner = await env.APP_DB.prepare(
    "SELECT owner_uid FROM cf_app_catalog WHERE id = ? LIMIT 1",
  )
    .bind(appId)
    .first<{ owner_uid: string | null }>();
  const ownerUid = owner?.owner_uid;
  if (typeof ownerUid !== "string" || !ownerUid) return;
  const titles: Record<typeof action, string> = {
    day1: "Your app's webhook is failing",
    day2: "Your app's webhook is still failing",
    disable: "Your app's webhook was disabled",
  };
  const bodies: Record<typeof action, string> = {
    day1: `Deliveries to the webhook for app ${appId} have failed for a day. Please check the endpoint.`,
    day2: `Deliveries to the webhook for app ${appId} have failed for two days and will be disabled after three.`,
    disable: `Deliveries to the webhook for app ${appId} were automatically disabled after three days of failures. Update the webhook configuration to re-enable them.`,
  };
  await env.APP_DB.prepare(
    "INSERT INTO cf_notification_outbox (notification_id, source_kind, source_id, uid, title, body, data_json, " +
      "status, attempts, not_before, created_at, updated_at) " +
      "VALUES (?, 'integration', ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?) " +
      "ON CONFLICT(source_kind, source_id) DO NOTHING",
  )
    .bind(
      crypto.randomUUID(),
      `webhook-health:${appId}:${windowStart}:${action}`,
      ownerUid,
      titles[action],
      bodies[action],
      JSON.stringify({ type: "text", app_id: appId, notification_type: "webhook_health" }),
      now,
      now,
      now,
    )
    .run();
}

export async function recordAppWebhookFailure(
  env: JobsEnv,
  appId: string,
  statusCode: number,
  error: string,
  now: number,
): Promise<WebhookHealthAction> {
  const boundedError = error.slice(0, 200);
  const row = await env.APP_DB.prepare(
    `SELECT first_failure_at, last_success_at, notified_day1, notified_day2, disabled
     FROM cf_app_webhook_health WHERE app_id = ? AND endpoint = '${ENDPOINT_INTEGRATION}'`,
  )
    .bind(appId)
    .first<HealthRow>();
  const windowBroken =
    !row ||
    (row.last_success_at !== null &&
      Number(row.last_success_at) >= Number(row.first_failure_at));
  if (windowBroken) {
    await env.APP_DB.prepare(
      "INSERT INTO cf_app_webhook_health (app_id, endpoint, first_failure_at, last_failure_at, last_success_at, " +
        "failure_count, last_status, last_error, notified_day1, notified_day2, disabled, updated_at) " +
        `VALUES (?, '${ENDPOINT_INTEGRATION}', ?, ?, NULL, 1, ?, ?, 0, 0, 0, ?) ` +
        "ON CONFLICT(app_id, endpoint) DO UPDATE SET first_failure_at = excluded.first_failure_at, " +
        "last_failure_at = excluded.last_failure_at, last_success_at = NULL, failure_count = 1, " +
        "last_status = excluded.last_status, last_error = excluded.last_error, notified_day1 = 0, " +
        "notified_day2 = 0, disabled = 0, updated_at = excluded.updated_at",
    )
      .bind(appId, now, now, statusCode, boundedError, now)
      .run();
    return "none";
  }
  const elapsed = now - Number(row.first_failure_at);
  let action: WebhookHealthAction = "none";
  let extra = "";
  if (elapsed >= WEBHOOK_HEALTH_DISABLE_SECONDS && Number(row.disabled) !== 1) {
    action = "disable";
    extra = ", disabled = 1";
  } else if (
    elapsed >= WEBHOOK_HEALTH_DAY2_SECONDS &&
    Number(row.notified_day2) !== 1
  ) {
    action = "day2";
    extra = ", notified_day2 = 1";
  } else if (
    elapsed >= WEBHOOK_HEALTH_DAY1_SECONDS &&
    Number(row.notified_day1) !== 1
  ) {
    action = "day1";
    extra = ", notified_day1 = 1";
  }
  await env.APP_DB.prepare(
    "UPDATE cf_app_webhook_health SET last_failure_at = ?, failure_count = failure_count + 1, " +
      `last_status = ?, last_error = ?, updated_at = ?${extra} ` +
      `WHERE app_id = ? AND endpoint = '${ENDPOINT_INTEGRATION}'`,
  )
    .bind(now, statusCode, boundedError, now, appId)
    .run();
  if (action !== "none") {
    await notifyOwner(env, appId, Number(row.first_failure_at), action, now);
  }
  return action;
}
