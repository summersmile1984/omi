import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import { validAccountDeletionUid } from "./account-deletion-residual";
import {
  deactivateAppPaymentLink,
  stripeAppPaymentLink,
  type AppPaymentLinkRow,
} from "./app-payment-links";
import type { JobMessage, JobsEnv } from "./env";
import { stripeRequest, stripeSecretKey } from "./stripe-client";

const APP_ID_MAX_LENGTH = 256;
const SUBSCRIPTION_BATCH_SIZE = 10;
const RECONCILE_BATCH_SIZE = 50;
const JOB_LEASE_SECONDS = 15 * 60;
const FAILED_RETRY_SECONDS = 5 * 60;
const MAX_AUTOMATIC_ATTEMPTS = 10;
const STRIPE_PAGE_SIZE = 100;
const STRIPE_MAX_SCHEDULE_PAGES = 10;
const STRIPE_SUBSCRIPTION_ID = /^sub_[A-Za-z0-9]{8,156}$/;
const STRIPE_SCHEDULE_ID = /^sub_sched_[A-Za-z0-9]{8,151}$/;
const STRIPE_CUSTOMER_ID = /^cus_[A-Za-z0-9]{8,156}$/;
const TERMINAL_SUBSCRIPTION_STATUSES = new Set([
  "canceled",
  "incomplete_expired",
]);

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;

type CatalogRow = {
  id: string;
  owner_uid: string | null;
  disabled: number;
  data_json: string;
};

type AppDeletionJob = {
  job_id: string;
  uid: string;
  status: "queued" | "running" | "completed" | "failed";
  attempts: number;
  payload_json: string;
  app_id: string;
  owner_uid: string;
  data_json: string;
};

type AppSubscriptionRow = {
  uid: string;
  app_id: string;
  stripe_customer_id: string;
  stripe_subscription_id: string;
  status: string;
  cancel_at_period_end: number;
};

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function validAppId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= APP_ID_MAX_LENGTH &&
    !value.includes("/")
  );
}

function paidCatalogPayload(raw: string, appId: string) {
  if (raw.length > 500_000) throw new Error("app catalog payload is invalid");
  const payload = objectValue(JSON.parse(raw));
  if (!payload || (payload.id !== undefined && payload.id !== appId)) {
    throw new Error("app catalog payload is invalid");
  }
  return payload.is_paid === true || payload.is_paid === 1;
}

function deletionMessage(
  jobId: string,
  uid: string,
  appId: string,
): JobMessage {
  return {
    jobId,
    uid,
    kind: "app_delete",
    payload: { appId },
  };
}

async function queueDeletion(
  env: JobsEnv,
  jobId: string,
  uid: string,
  appId: string,
  delaySeconds = 0,
) {
  await env.JOBS.send(deletionMessage(jobId, uid, appId), { delaySeconds });
}

async function existingDeletion(env: JobsEnv, appId: string) {
  return env.APP_DB.prepare(
    `SELECT j.job_id, j.uid, j.status, j.attempts
     FROM cf_app_deletion_fences f
     JOIN cf_jobs j ON j.job_id = f.job_id
     WHERE f.app_id = ? AND j.kind = 'app_delete' LIMIT 1`,
  )
    .bind(appId)
    .first<{
      job_id: string;
      uid: string;
      status: "queued" | "running" | "completed" | "failed";
      attempts: number;
    }>();
}

async function republishExistingDeletion(
  env: JobsEnv,
  existing: {
    job_id: string;
    uid: string;
    status: string;
  },
  appId: string,
) {
  if (existing.status === "running") return;
  const now = Math.floor(Date.now() / 1_000);
  await env.APP_DB.prepare(
    `UPDATE cf_jobs
     SET status = 'queued', attempts = 0, last_error = NULL, updated_at = ?
     WHERE job_id = ? AND uid = ? AND kind = 'app_delete'`,
  )
    .bind(now, existing.job_id, existing.uid)
    .run();
  try {
    await queueDeletion(env, existing.job_id, existing.uid, appId);
  } catch {
    await env.APP_DB.prepare(
      `UPDATE cf_jobs SET status = 'failed', last_error = 'queue unavailable',
                          updated_at = ?
       WHERE job_id = ? AND uid = ? AND kind = 'app_delete'`,
    )
      .bind(now, existing.job_id, existing.uid)
      .run();
    throw new Error("app deletion queue unavailable");
  }
}

async function admitAppDeletion(
  c: JobsContext,
  context: SignedAuthContext,
  appId: string,
) {
  if (!validAppId(appId) || !validAccountDeletionUid(context.uid)) {
    return c.json({ detail: "App not found" }, 404);
  }
  try {
    const app = await c.env.APP_DB.prepare(
      `SELECT id, owner_uid, disabled, data_json
       FROM cf_app_catalog WHERE id = ? LIMIT 1`,
    )
      .bind(appId)
      .first<CatalogRow>();
    if (!app || app.id !== appId) {
      return c.json({ detail: "App not found" }, 404);
    }
    if (app.owner_uid !== context.uid) {
      return c.json(
        { detail: "You are not authorized to perform this action" },
        403,
      );
    }

    const existing = await existingDeletion(c.env, appId);
    if (existing) {
      if (existing.uid !== context.uid) {
        throw new Error("app deletion ownership mismatch");
      }
      await republishExistingDeletion(c.env, existing, appId);
      return c.json({ status: "ok" });
    }

    const [paymentLink, subscriptionCount] = await Promise.all([
      stripeAppPaymentLink(c.env, appId),
      c.env.APP_DB.prepare(
        "SELECT COUNT(*) AS count FROM cf_app_subscriptions WHERE app_id = ?",
      )
        .bind(appId)
        .first<{ count?: unknown }>(),
    ]);
    const isPaid = paidCatalogPayload(app.data_json, appId);
    if (isPaid && !paymentLink) {
      throw new Error("paid app payment mapping is unavailable");
    }
    if (paymentLink && paymentLink.owner_uid !== context.uid) {
      throw new Error("paid app payment owner does not match");
    }
    if (paymentLink || Number(subscriptionCount?.count) > 0) {
      stripeSecretKey(c.env);
    }

    const jobId = crypto.randomUUID();
    const now = Math.floor(Date.now() / 1_000);
    const payloadJson = JSON.stringify({ app_id: appId });
    const results = await c.env.APP_DB.batch([
      c.env.APP_DB.prepare(
        "UPDATE cf_app_catalog SET disabled = 1, updated_at = ? WHERE id = ? AND owner_uid = ?",
      ).bind(now, appId, context.uid),
      c.env.APP_DB.prepare(
        "DELETE FROM cf_user_enabled_apps WHERE app_id = ?",
      ).bind(appId),
      c.env.APP_DB.prepare(
        `INSERT INTO cf_jobs
           (job_id, uid, kind, payload_json, status, attempts, created_at, updated_at)
         VALUES (?, ?, 'app_delete', ?, 'queued', 0, ?, ?)`,
      ).bind(jobId, context.uid, payloadJson, now, now),
      c.env.APP_DB.prepare(
        `INSERT INTO cf_app_deletion_fences (app_id, job_id, created_at)
         VALUES (?, ?, ?)`,
      ).bind(appId, jobId, now),
    ]);
    if (
      results.length !== 4 ||
      results[0]?.meta?.changes !== 1 ||
      results[2]?.meta?.changes !== 1 ||
      results[3]?.meta?.changes !== 1
    ) {
      throw new Error("app deletion intent was not persisted");
    }
    try {
      await queueDeletion(c.env, jobId, context.uid, appId);
    } catch {
      await c.env.APP_DB.prepare(
        `UPDATE cf_jobs SET status = 'failed', last_error = 'queue unavailable',
                            updated_at = ?
         WHERE job_id = ? AND uid = ? AND kind = 'app_delete'`,
      )
        .bind(now, jobId, context.uid)
        .run();
      return c.json({ error: "app_deletion_unavailable" }, 503);
    }
    return c.json({ status: "ok" });
  } catch {
    return c.json({ error: "app_deletion_unavailable" }, 503);
  }
}

export function registerAppDeletionRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
) {
  app.delete("/v1/apps/:appId", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    return admitAppDeletion(c, context, c.req.param("appId"));
  });
}

function stripeObject(raw: unknown, expectedId: string) {
  const value = objectValue(raw);
  if (!value || value.id !== expectedId) {
    throw new Error("Stripe app deletion object does not match");
  }
  return value;
}

async function releaseSubscriptionSchedules(
  env: JobsEnv,
  jobId: string,
  row: AppSubscriptionRow,
) {
  let startingAfter: string | null = null;
  for (let page = 0; page < STRIPE_MAX_SCHEDULE_PAGES; page += 1) {
    const query = new URLSearchParams({
      customer: row.stripe_customer_id,
      limit: String(STRIPE_PAGE_SIZE),
    });
    if (startingAfter) query.set("starting_after", startingAfter);
    const list = objectValue(
      await stripeRequest(
        env,
        `/v1/subscription_schedules?${query.toString()}`,
      ),
    );
    if (!list || list.object !== "list" || !Array.isArray(list.data)) {
      throw new Error("Stripe subscription schedule list is invalid");
    }
    for (const raw of list.data) {
      const schedule = objectValue(raw);
      if (
        !schedule ||
        typeof schedule.id !== "string" ||
        !STRIPE_SCHEDULE_ID.test(schedule.id) ||
        schedule.customer !== row.stripe_customer_id
      ) {
        throw new Error("Stripe subscription schedule is invalid");
      }
      if (
        (schedule.status === "active" || schedule.status === "not_started") &&
        schedule.subscription === row.stripe_subscription_id
      ) {
        const released = stripeObject(
          await stripeRequest(
            env,
            `/v1/subscription_schedules/${encodeURIComponent(schedule.id)}/release`,
            {
              method: "POST",
              idempotencyKey: `app-delete-${jobId}-release-${schedule.id.slice(-48)}`,
            },
          ),
          schedule.id,
        );
        if (
          !TERMINAL_SUBSCRIPTION_STATUSES.has(String(released.status)) &&
          released.status !== "released"
        ) {
          throw new Error("Stripe subscription schedule remains active");
        }
      }
    }
    if (list.has_more === false) return;
    if (list.has_more !== true || list.data.length === 0) {
      throw new Error("Stripe subscription schedule pagination is invalid");
    }
    const last = objectValue(list.data.at(-1));
    startingAfter = typeof last?.id === "string" ? last.id : null;
    if (!startingAfter) {
      throw new Error("Stripe subscription schedule pagination is invalid");
    }
  }
  throw new Error("too many Stripe subscription schedules");
}

function validateSubscriptionRow(row: AppSubscriptionRow, appId: string) {
  if (
    row.app_id !== appId ||
    !validAccountDeletionUid(row.uid) ||
    !STRIPE_CUSTOMER_ID.test(row.stripe_customer_id) ||
    !STRIPE_SUBSCRIPTION_ID.test(row.stripe_subscription_id) ||
    typeof row.status !== "string" ||
    row.status.length < 1 ||
    row.status.length > 80 ||
    (Number(row.cancel_at_period_end) !== 0 &&
      Number(row.cancel_at_period_end) !== 1)
  ) {
    throw new Error("invalid app subscription mapping");
  }
}

function assertStripeSubscriptionOwnership(
  subscription: Record<string, unknown>,
  row: AppSubscriptionRow,
  appId: string,
): asserts subscription is Record<string, unknown> & {
  status: string;
  cancel_at_period_end?: boolean;
} {
  const metadata = objectValue(subscription.metadata);
  if (
    subscription.customer !== row.stripe_customer_id ||
    metadata?.app_id !== appId ||
    metadata.uid !== row.uid ||
    typeof subscription.status !== "string" ||
    (subscription.cancel_at_period_end !== undefined &&
      typeof subscription.cancel_at_period_end !== "boolean")
  ) {
    throw new Error("Stripe app subscription ownership does not match");
  }
}

async function stopSubscriptionRenewal(
  env: JobsEnv,
  jobId: string,
  appId: string,
  row: AppSubscriptionRow,
  now: number,
) {
  validateSubscriptionRow(row, appId);
  const path = `/v1/subscriptions/${encodeURIComponent(row.stripe_subscription_id)}`;
  let subscription = stripeObject(
    await stripeRequest(env, path),
    row.stripe_subscription_id,
  );
  assertStripeSubscriptionOwnership(subscription, row, appId);
  if (
    !TERMINAL_SUBSCRIPTION_STATUSES.has(subscription.status) &&
    subscription.cancel_at_period_end !== true
  ) {
    await releaseSubscriptionSchedules(env, jobId, row);
    subscription = stripeObject(
      await stripeRequest(env, path, {
        method: "POST",
        form: new URLSearchParams({ cancel_at_period_end: "true" }),
        idempotencyKey: `app-delete-${jobId}-cancel-${row.stripe_subscription_id.slice(-48)}`,
      }),
      row.stripe_subscription_id,
    );
    assertStripeSubscriptionOwnership(subscription, row, appId);
    if (
      subscription.cancel_at_period_end !== true &&
      !TERMINAL_SUBSCRIPTION_STATUSES.has(String(subscription.status))
    ) {
      throw new Error("Stripe app subscription remains renewable");
    }
  }
  const result = await env.APP_DB.prepare(
    `UPDATE cf_app_subscriptions
     SET cancel_at_period_end = 1, status = ?, updated_at = ?,
         app_delete_verified_at = ?
     WHERE uid = ? AND app_id = ? AND stripe_subscription_id = ?`,
  )
    .bind(
      String(subscription.status),
      now,
      now,
      row.uid,
      appId,
      row.stripe_subscription_id,
    )
    .run();
  if (result.meta?.changes !== 1) {
    throw new Error("app subscription verification was not persisted");
  }
}

async function appDeletionJob(
  env: JobsEnv,
  jobId: string,
): Promise<AppDeletionJob | null> {
  return env.APP_DB.prepare(
    `SELECT j.job_id, j.uid, j.status, j.attempts, j.payload_json,
            f.app_id, c.owner_uid, c.data_json
     FROM cf_jobs j
     JOIN cf_app_deletion_fences f ON f.job_id = j.job_id
     JOIN cf_app_catalog c ON c.id = f.app_id
     WHERE j.job_id = ? AND j.kind = 'app_delete' LIMIT 1`,
  )
    .bind(jobId)
    .first<AppDeletionJob>();
}

async function requeueRemaining(
  message: Message<JobMessage>,
  env: JobsEnv,
  job: AppDeletionJob,
  now: number,
) {
  await env.APP_DB.prepare(
    `UPDATE cf_jobs
     SET status = 'queued', attempts = 0, last_error = NULL, updated_at = ?
     WHERE job_id = ? AND uid = ? AND kind = 'app_delete'`,
  )
    .bind(now, job.job_id, job.uid)
    .run();
  try {
    await queueDeletion(env, job.job_id, job.uid, job.app_id, 1);
    message.ack();
  } catch {
    await env.APP_DB.prepare(
      `UPDATE cf_jobs SET status = 'failed', last_error = 'queue unavailable',
                          updated_at = ?
       WHERE job_id = ? AND uid = ? AND kind = 'app_delete'`,
    )
      .bind(now, job.job_id, job.uid)
      .run();
    message.ack();
  }
}

async function completeDeletion(
  env: JobsEnv,
  job: AppDeletionJob,
  paymentLink: AppPaymentLinkRow | null,
  now: number,
) {
  const statements = [
    env.APP_DB.prepare(
      "DELETE FROM cf_user_enabled_apps WHERE app_id = ?",
    ).bind(job.app_id),
  ];
  if (paymentLink) {
    statements.push(
      env.APP_DB.prepare(
        `INSERT INTO cf_retired_paid_apps
           (app_id, stripe_payment_link_id, retired_at)
         VALUES (?, ?, ?)
         ON CONFLICT(app_id) DO UPDATE SET
           stripe_payment_link_id = excluded.stripe_payment_link_id,
           retired_at = MIN(cf_retired_paid_apps.retired_at, excluded.retired_at)`,
      ).bind(job.app_id, paymentLink.stripe_payment_link_id, now),
    );
  }
  statements.push(
    env.APP_DB.prepare(
      "DELETE FROM cf_app_catalog WHERE id = ? AND owner_uid = ?",
    ).bind(job.app_id, job.uid),
    env.APP_DB.prepare(
      `UPDATE cf_jobs SET status = 'completed', result_json = ?,
                          last_error = NULL, updated_at = ?
       WHERE job_id = ? AND uid = ? AND kind = 'app_delete'`,
    ).bind(
      JSON.stringify({ app_id: job.app_id, deleted: true }),
      now,
      job.job_id,
      job.uid,
    ),
  );
  // D1 reports cascading child deletes in the catalog statement's change
  // metadata, so a successful delete is not guaranteed to report exactly one
  // change. SQL errors still reject the atomic batch; zero-row deletes are also
  // valid when account deletion won the race and already owns the cleanup.
  await env.APP_DB.batch(statements);
}

export async function processAppDeletionMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
) {
  const job = await appDeletionJob(env, message.body.jobId);
  if (!job) {
    message.ack();
    return;
  }
  const ownerDeletion = await env.APP_DB.prepare(
    `SELECT
       EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) AS deleting,
       EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?) AS deleted`,
  )
    .bind(job.uid, job.uid)
    .first<{ deleting?: unknown; deleted?: unknown }>();
  if (
    Number(ownerDeletion?.deleting) === 1 ||
    Number(ownerDeletion?.deleted) === 1
  ) {
    message.ack();
    return;
  }
  try {
    const payload = objectValue(JSON.parse(job.payload_json));
    if (
      job.status !== "running" ||
      job.uid !== message.body.uid ||
      job.owner_uid !== job.uid ||
      !validAppId(job.app_id) ||
      payload?.app_id !== job.app_id ||
      message.body.payload.appId !== job.app_id
    ) {
      throw new Error("app deletion job mapping is invalid");
    }
    const isPaid = paidCatalogPayload(job.data_json, job.app_id);
    const paymentLink = await stripeAppPaymentLink(env, job.app_id);
    if (isPaid && !paymentLink) {
      throw new Error("paid app payment mapping is unavailable");
    }
    if (paymentLink && paymentLink.owner_uid !== job.uid) {
      throw new Error("paid app payment owner does not match");
    }
    if (paymentLink) {
      await deactivateAppPaymentLink(
        env,
        paymentLink,
        `app-delete-${job.job_id}`,
      );
      const result = await env.APP_DB.prepare(
        `UPDATE cf_app_payment_links SET active = 0, updated_at = ?
         WHERE app_id = ? AND stripe_payment_link_id = ?`,
      )
        .bind(
          Math.floor(Date.now() / 1_000),
          job.app_id,
          paymentLink.stripe_payment_link_id,
        )
        .run();
      if (result.meta?.changes !== 1) {
        throw new Error("app Payment Link retirement was not persisted");
      }
    }
    const subscriptions = await env.APP_DB.prepare(
      `SELECT uid, app_id, stripe_customer_id, stripe_subscription_id,
              status, cancel_at_period_end
       FROM cf_app_subscriptions
       WHERE app_id = ? AND app_delete_verified_at IS NULL
       ORDER BY uid LIMIT ?`,
    )
      .bind(job.app_id, SUBSCRIPTION_BATCH_SIZE + 1)
      .all<AppSubscriptionRow>();
    const now = Math.floor(Date.now() / 1_000);
    for (const row of (subscriptions.results || []).slice(
      0,
      SUBSCRIPTION_BATCH_SIZE,
    )) {
      await stopSubscriptionRenewal(env, job.job_id, job.app_id, row, now);
    }
    const remaining = await env.APP_DB.prepare(
      `SELECT COUNT(*) AS count FROM cf_app_subscriptions
       WHERE app_id = ? AND app_delete_verified_at IS NULL`,
    )
      .bind(job.app_id)
      .first<{ count?: unknown }>();
    if (Number(remaining?.count) > 0) {
      await requeueRemaining(message, env, job, now);
      return;
    }
    await completeDeletion(env, job, paymentLink, now);
    message.ack();
  } catch {
    const now = Math.floor(Date.now() / 1_000);
    await env.APP_DB.prepare(
      `UPDATE cf_jobs
       SET status = 'failed', last_error = 'app deletion dependency unavailable',
           updated_at = ?
       WHERE job_id = ? AND uid = ? AND kind = 'app_delete'`,
    )
      .bind(now, job.job_id, job.uid)
      .run();
    message.ack();
  }
}

export async function reconcileAppDeletions(
  env: JobsEnv,
  now = Math.floor(Date.now() / 1_000),
) {
  const rows = await env.APP_DB.prepare(
    `SELECT j.job_id, j.uid, j.status, f.app_id
     FROM cf_jobs j
     JOIN cf_app_deletion_fences f ON f.job_id = j.job_id
     WHERE j.kind = 'app_delete' AND j.attempts < ?
       AND NOT EXISTS(
         SELECT 1 FROM cf_account_deletion_intents i WHERE i.uid = j.uid
       )
       AND NOT EXISTS(
         SELECT 1 FROM cf_account_deletion_tombstones t WHERE t.uid = j.uid
       )
       AND ((j.status = 'queued' AND j.updated_at <= ?)
         OR (j.status = 'failed' AND j.updated_at <= ?)
         OR (j.status = 'running' AND j.updated_at <= ?))
     ORDER BY j.updated_at, j.job_id LIMIT ?`,
  )
    .bind(
      MAX_AUTOMATIC_ATTEMPTS,
      now - 60,
      now - FAILED_RETRY_SECONDS,
      now - JOB_LEASE_SECONDS,
      RECONCILE_BATCH_SIZE,
    )
    .all<{
      job_id: string;
      uid: string;
      status: string;
      app_id: string;
    }>();
  for (const row of rows.results || []) {
    if (!validAppId(row.app_id) || !validAccountDeletionUid(row.uid)) continue;
    try {
      await env.APP_DB.prepare(
        `UPDATE cf_jobs SET status = 'queued', last_error = NULL, updated_at = ?
         WHERE job_id = ? AND uid = ? AND kind = 'app_delete'`,
      )
        .bind(now, row.job_id, row.uid)
        .run();
      await queueDeletion(env, row.job_id, row.uid, row.app_id);
    } catch {
      try {
        await env.APP_DB.prepare(
          `UPDATE cf_jobs SET status = 'failed', last_error = 'queue unavailable',
                              updated_at = ?
           WHERE job_id = ? AND uid = ? AND kind = 'app_delete'`,
        )
          .bind(now, row.job_id, row.uid)
          .run();
      } catch {
        // Account deletion may have fenced or purged the generic job after the
        // reconciler selected it. That workflow owns the remaining cleanup.
      }
    }
  }
}
