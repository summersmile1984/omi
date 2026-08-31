import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../shared/auth-context";
import { recordFallback } from "../shared/fallback";
import {
  ACCOUNT_DELETION_CONVERSATION_RECORDING_PREFIX_PATTERNS,
  ACCOUNT_DELETION_CHAT_FILES_PREFIX_PATTERNS,
  ACCOUNT_DELETION_D1_PURGE_SURFACES,
  ACCOUNT_DELETION_R2_PREFIX_PATTERNS,
  ACCOUNT_DELETION_SPEECH_PROFILE_PREFIX_PATTERNS,
  readAccountProductResidual,
  validAccountDeletionUid,
} from "./account-deletion-residual";
import {
  deactivateAppPaymentLink,
  retireOwnedPaidApps,
  stripeOwnedAppPaymentLinks,
} from "./app-payment-links";
import type { JobMessage, JobsEnv } from "./env";
import {
  StripeResponseError,
  stripeRequest,
  stripeSecretKey,
} from "./stripe-client";
import { purgeAccountVectorProjections } from "./vector-projection";

const MAX_REQUEST_BODY_BYTES = 4_096;
const FENCE_QUIESCENCE_SECONDS = 60;
const ZERO_SCAN_SETTLE_SECONDS = 30;
const INTENT_LEASE_SECONDS = 5 * 60;
const AUTH_LIFECYCLE_TIMEOUT_MS = 15_000;
const RETRY_BASE_SECONDS = 60;
const RETRY_MAX_SECONDS = 60 * 60;
const TOMBSTONE_SECONDS = 25 * 60 * 60;
const R2_DELETE_BATCH_SIZE = 1_000;
const D1_DELETE_BATCH_SIZE = 250;
const RECONCILE_BATCH_SIZE = 50;
const ISOLATED_STAGING_MANIFEST = "isolated-staging-v1";
const STRIPE_SUBSCRIPTION_ID = /^sub_[A-Za-z0-9]{8,128}$/;
const STRIPE_SCHEDULE_ID = /^sub_sched_[A-Za-z0-9]{8,128}$/;
const STRIPE_CUSTOMER_ID = /^cus_[A-Za-z0-9]{8,128}$/;
const STRIPE_CONNECT_ACCOUNT_ID = /^acct_[A-Za-z0-9]{7,155}$/;
const STRIPE_TERMINAL_STATUSES = new Set(["canceled", "incomplete_expired"]);
const ACCOUNT_DELETION_RUN_JOB_ID = /^[A-Za-z0-9_-]{1,128}$/;

type AccountDeletionIntent = {
  uid: unknown;
  job_id: unknown;
  status: unknown;
  phase: unknown;
  attempts: unknown;
  lease_token: unknown;
  lease_until: unknown;
  next_attempt_at: unknown;
  settled_at: unknown;
  created_at: unknown;
};

type ParsedAccountDeletionIntent = {
  uid: string;
  jobId: string;
  status: "pending" | "running" | "failed";
  phase: "quiescing" | "purging" | "identity";
  attempts: number;
  leaseToken: string | null;
  leaseUntil: number | null;
  nextAttemptAt: number;
  settledAt: number | null;
  createdAt: number;
};

type AccountDeletionFeedback = {
  reason: string | null;
  reasonDetails: string | null;
};

type RequestContext = Context<{ Bindings: JobsEnv }>;

function integer(value: unknown, label: string): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new Error(`invalid account deletion ${label}`);
  }
  return parsed;
}

function optionalInteger(value: unknown, label: string): number | null {
  if (value === null || value === undefined) return null;
  return integer(value, label);
}

function optionalString(value: unknown, label: string): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || !value) {
    throw new Error(`invalid account deletion ${label}`);
  }
  return value;
}

function accountDeletionIntent(
  row: AccountDeletionIntent,
): ParsedAccountDeletionIntent {
  if (!validAccountDeletionUid(String(row.uid || ""))) {
    throw new Error("invalid account deletion uid");
  }
  if (typeof row.job_id !== "string" || !row.job_id) {
    throw new Error("invalid account deletion job id");
  }
  if (
    row.status !== "pending" &&
    row.status !== "running" &&
    row.status !== "failed"
  ) {
    throw new Error("invalid account deletion status");
  }
  if (
    row.phase !== "quiescing" &&
    row.phase !== "purging" &&
    row.phase !== "identity"
  ) {
    throw new Error("invalid account deletion phase");
  }
  return {
    uid: String(row.uid),
    jobId: row.job_id,
    status: row.status,
    phase: row.phase,
    attempts: integer(row.attempts, "attempts"),
    leaseToken: optionalString(row.lease_token, "lease token"),
    leaseUntil: optionalInteger(row.lease_until, "lease until"),
    nextAttemptAt: integer(row.next_attempt_at, "next attempt"),
    settledAt: optionalInteger(row.settled_at, "settled at"),
    createdAt: integer(row.created_at, "created at"),
  };
}

function accountDeletionMessage(jobId: string): JobMessage {
  return { jobId, uid: "", kind: "account_delete", payload: {} };
}

async function queueAccountDeletion(
  env: JobsEnv,
  jobId: string,
  delaySeconds = 0,
): Promise<boolean> {
  try {
    await env.JOBS.send(
      accountDeletionMessage(jobId),
      delaySeconds > 0 ? { delaySeconds } : undefined,
    );
    return true;
  } catch {
    recordFallback({
      component: "other",
      from: "d1",
      to: "none",
      reason: "dependency_unavailable",
      outcome: "degraded",
    });
    return false;
  }
}

async function readBoundedRequestBody(request: Request): Promise<string> {
  const declared = Number(request.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > MAX_REQUEST_BODY_BYTES) {
    throw new Error("account deletion request body too large");
  }
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_REQUEST_BODY_BYTES) {
        throw new Error("account deletion request body too large");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(body);
}

function boundedFeedbackString(
  value: unknown,
  maximum: number,
  label: string,
): string | null {
  if (value === undefined || value === null || value === "") return null;
  if (typeof value !== "string" || value.length > maximum) {
    throw new Error(`invalid ${label}`);
  }
  return value;
}

async function accountDeletionFeedback(
  request: Request,
): Promise<AccountDeletionFeedback> {
  const raw = await readBoundedRequestBody(request);
  if (!raw.trim()) return { reason: null, reasonDetails: null };
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    throw new Error("invalid account deletion JSON");
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new Error("invalid account deletion body");
  }
  const object = body as Record<string, unknown>;
  return {
    reason: boundedFeedbackString(object.reason, 64, "reason"),
    reasonDetails: boundedFeedbackString(
      object.reason_details,
      2_000,
      "reason details",
    ),
  };
}

async function assertCloudflareOwnedAccount(env: JobsEnv, uid: string) {
  const row = await env.APP_DB.prepare(
    `SELECT state, checkpoint_phase, manifest_id, destination_backend_bound
     FROM cf_account_cutover WHERE uid = ?`,
  )
    .bind(uid)
    .first<{
      state?: unknown;
      checkpoint_phase?: unknown;
      manifest_id?: unknown;
      destination_backend_bound?: unknown;
    }>();
  if (
    row?.state !== "new" ||
    row.checkpoint_phase !== "completed" ||
    row.manifest_id !== ISOLATED_STAGING_MANIFEST ||
    Number(row.destination_backend_bound) !== 1
  ) {
    throw new Error("account deletion target is not Cloudflare-owned");
  }
}

async function stripeSubscriptionId(
  env: JobsEnv,
  uid: string,
): Promise<string | null> {
  const row = await env.APP_DB.prepare(
    "SELECT stripe_subscription_id FROM cf_user_subscriptions WHERE uid = ?",
  )
    .bind(uid)
    .first<{ stripe_subscription_id?: unknown }>();
  const raw = row?.stripe_subscription_id;
  if (raw === undefined || raw === null || String(raw).trim() === "") {
    return null;
  }
  const subscriptionId = String(raw).trim();
  if (!STRIPE_SUBSCRIPTION_ID.test(subscriptionId)) {
    throw new Error("invalid Stripe subscription id");
  }
  return subscriptionId;
}

async function stripeAppSubscriptionIds(
  env: JobsEnv,
  uid: string,
): Promise<string[]> {
  const result = await env.APP_DB.prepare(
    "SELECT stripe_subscription_id FROM cf_app_subscriptions WHERE uid = ? ORDER BY app_id LIMIT 501",
  )
    .bind(uid)
    .all<{ stripe_subscription_id?: unknown }>();
  if ((result.results || []).length > 500) {
    throw new Error("too many Stripe app subscriptions");
  }
  const ids: string[] = [];
  for (const row of result.results || []) {
    const raw = row.stripe_subscription_id;
    if (raw === undefined || raw === null || String(raw).trim() === "") {
      continue;
    }
    const subscriptionId = String(raw).trim();
    if (!STRIPE_SUBSCRIPTION_ID.test(subscriptionId)) {
      throw new Error("invalid Stripe subscription id");
    }
    ids.push(subscriptionId);
  }
  return [...new Set(ids)];
}

async function stripeOwnedAppSubscriptionIds(
  env: JobsEnv,
  ownerUid: string,
): Promise<string[]> {
  const result = await env.APP_DB.prepare(
    `SELECT s.stripe_subscription_id
     FROM cf_app_subscriptions s
     JOIN cf_app_catalog a ON a.id = s.app_id
     WHERE a.owner_uid = ?
     ORDER BY s.app_id, s.uid LIMIT 501`,
  )
    .bind(ownerUid)
    .all<{ stripe_subscription_id?: unknown }>();
  if ((result.results || []).length > 500) {
    throw new Error("too many Stripe app subscriptions");
  }
  const ids: string[] = [];
  for (const row of result.results || []) {
    const subscriptionId = String(row.stripe_subscription_id || "").trim();
    if (!STRIPE_SUBSCRIPTION_ID.test(subscriptionId)) {
      throw new Error("invalid Stripe subscription id");
    }
    ids.push(subscriptionId);
  }
  return [...new Set(ids)];
}

async function stripeConnectAccountId(
  env: JobsEnv,
  uid: string,
): Promise<string | null> {
  const row = await env.APP_DB.prepare(
    "SELECT stripe_account_id FROM cf_creator_payment_profiles WHERE uid = ?",
  )
    .bind(uid)
    .first<{ stripe_account_id?: unknown }>();
  const raw = row?.stripe_account_id;
  if (raw === undefined || raw === null || String(raw).trim() === "") {
    return null;
  }
  const accountId = String(raw).trim();
  if (!STRIPE_CONNECT_ACCOUNT_ID.test(accountId)) {
    throw new Error("invalid Stripe Connect account id");
  }
  return accountId;
}

async function assertExternalProviderCleanupConfigured(
  env: JobsEnv,
  uid: string,
) {
  const [subscriptionId, accountId] = await Promise.all([
    stripeSubscriptionId(env, uid),
    stripeConnectAccountId(env, uid),
  ]);
  const appSubscriptionIds = await stripeAppSubscriptionIds(env, uid);
  const ownedAppSubscriptionIds = await stripeOwnedAppSubscriptionIds(env, uid);
  const ownedAppPaymentLinks = await stripeOwnedAppPaymentLinks(env, uid);
  if (
    subscriptionId ||
    accountId ||
    appSubscriptionIds.length > 0 ||
    ownedAppSubscriptionIds.length > 0 ||
    ownedAppPaymentLinks.length > 0
  ) {
    stripeSecretKey(env);
  }
}

type StripeSubscription = {
  status: string;
  cancelAtPeriodEnd: boolean;
  customerId: string | null;
};

async function parseStripeSubscription(
  body: unknown,
  expectedId: string,
): Promise<StripeSubscription> {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new Error("Stripe subscription response is invalid");
  }
  const subscription = body as Record<string, unknown>;
  if (
    subscription.id !== expectedId ||
    typeof subscription.status !== "string" ||
    (subscription.cancel_at_period_end !== undefined &&
      typeof subscription.cancel_at_period_end !== "boolean") ||
    (subscription.customer !== undefined &&
      subscription.customer !== null &&
      (typeof subscription.customer !== "string" ||
        !STRIPE_CUSTOMER_ID.test(subscription.customer)))
  ) {
    throw new Error("Stripe subscription response is invalid");
  }
  return {
    status: subscription.status,
    cancelAtPeriodEnd: subscription.cancel_at_period_end === true,
    customerId:
      typeof subscription.customer === "string" ? subscription.customer : null,
  };
}

function parseStripeSchedule(body: unknown, expectedId: string) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new Error("Stripe subscription schedule response is invalid");
  }
  const schedule = body as Record<string, unknown>;
  if (
    schedule.id !== expectedId ||
    typeof schedule.status !== "string" ||
    !["active", "not_started", "completed", "canceled", "released"].includes(
      schedule.status,
    )
  ) {
    throw new Error("Stripe subscription schedule response is invalid");
  }
  return schedule.status;
}

async function releaseActiveStripeSchedules(
  env: JobsEnv,
  intent: ParsedAccountDeletionIntent,
  subscriptionId: string,
  customerId: string,
) {
  const query = new URLSearchParams({ customer: customerId, limit: "10" });
  const body = await stripeRequest(
    env,
    `/v1/subscription_schedules?${query.toString()}`,
  );
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new Error("Stripe subscription schedule list is invalid");
  }
  const data = (body as Record<string, unknown>).data;
  if (!Array.isArray(data)) {
    throw new Error("Stripe subscription schedule list is invalid");
  }
  for (const raw of data) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const schedule = raw as Record<string, unknown>;
    if (
      (schedule.status !== "active" && schedule.status !== "not_started") ||
      schedule.subscription !== subscriptionId
    ) {
      continue;
    }
    if (
      typeof schedule.id !== "string" ||
      !STRIPE_SCHEDULE_ID.test(schedule.id)
    ) {
      throw new Error("Stripe subscription schedule list is invalid");
    }
    parseStripeSchedule(
      await stripeRequest(
        env,
        `/v1/subscription_schedules/${encodeURIComponent(schedule.id)}/release`,
        {
          method: "POST",
          idempotencyKey: `account-delete-${intent.jobId}-release-${schedule.id.slice(-48)}`,
        },
      ),
      schedule.id,
    );
  }
}

async function cleanupExternalProviders(
  env: JobsEnv,
  intent: ParsedAccountDeletionIntent,
) {
  const ownedAppPaymentLinks = await stripeOwnedAppPaymentLinks(
    env,
    intent.uid,
  );
  if (ownedAppPaymentLinks.length > 0) {
    for (const paymentLink of ownedAppPaymentLinks) {
      await deactivateAppPaymentLink(
        env,
        paymentLink,
        `account-delete-${intent.jobId}`,
      );
    }
    await retireOwnedPaidApps(env, intent.uid);
  }
  const subscriptionId = await stripeSubscriptionId(env, intent.uid);
  const appSubscriptionIds = await stripeAppSubscriptionIds(env, intent.uid);
  const ownedAppSubscriptionIds = await stripeOwnedAppSubscriptionIds(
    env,
    intent.uid,
  );
  const seenSubscriptions = new Set<string>();
  const subscriptions = [
    ...(subscriptionId ? [{ id: subscriptionId, suffix: "" }] : []),
    ...appSubscriptionIds
      .filter((id) => id !== subscriptionId)
      .map((id) => ({ id, suffix: `-app-${id.slice(-48)}` })),
    ...ownedAppSubscriptionIds.map((id) => ({
      id,
      suffix: `-creator-app-${id.slice(-40)}`,
    })),
  ].filter(({ id }) => {
    if (seenSubscriptions.has(id)) return false;
    seenSubscriptions.add(id);
    return true;
  });
  for (const subscription of subscriptions) {
    const subscriptionId = subscription.id;
    const path = `/v1/subscriptions/${encodeURIComponent(subscriptionId)}`;
    const current = await parseStripeSubscription(
      await stripeRequest(env, path),
      subscriptionId,
    );
    if (!STRIPE_TERMINAL_STATUSES.has(current.status)) {
      if (!current.customerId) {
        throw new Error("Stripe subscription customer is unavailable");
      }
      await releaseActiveStripeSchedules(
        env,
        intent,
        subscriptionId,
        current.customerId,
      );
      if (!current.cancelAtPeriodEnd) {
        const form = new URLSearchParams({ cancel_at_period_end: "true" });
        const canceled = await parseStripeSubscription(
          await stripeRequest(env, path, {
            method: "POST",
            form,
            idempotencyKey: `account-delete-${intent.jobId}${subscription.suffix}`,
          }),
          subscriptionId,
        );
        if (
          !canceled.cancelAtPeriodEnd &&
          !STRIPE_TERMINAL_STATUSES.has(canceled.status)
        ) {
          throw new Error("Stripe subscription remains billable");
        }
      }
    }
  }

  const accountId = await stripeConnectAccountId(env, intent.uid);
  if (!accountId) return;
  try {
    const deleted = await stripeRequest(
      env,
      `/v1/accounts/${encodeURIComponent(accountId)}`,
      {
        method: "DELETE",
        idempotencyKey: `account-delete-${intent.jobId}-connect`,
      },
    );
    if (
      !deleted ||
      typeof deleted !== "object" ||
      Array.isArray(deleted) ||
      (deleted as Record<string, unknown>).id !== accountId ||
      (deleted as Record<string, unknown>).deleted !== true
    ) {
      throw new Error("Stripe Connect account remains active");
    }
  } catch (error) {
    // A retry can arrive after Stripe committed the deletion but before D1/R2
    // cleanup advanced. Stripe's 404 is the idempotent goal state here.
    if (!(error instanceof StripeResponseError && error.status === 404)) {
      throw error;
    }
  }
}

async function activeDeletionTombstone(env: JobsEnv, uid: string, now: number) {
  return env.APP_DB.prepare(
    "SELECT 1 AS active FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ?",
  )
    .bind(uid, now)
    .first<{ active?: unknown }>();
}

async function existingDeletionJobId(
  env: JobsEnv,
  uid: string,
): Promise<string | null> {
  const existing = await env.APP_DB.prepare(
    "SELECT job_id FROM cf_account_deletion_intents WHERE uid = ?",
  )
    .bind(uid)
    .first<{ job_id?: unknown }>();
  if (!existing) return null;
  if (typeof existing.job_id !== "string" || !existing.job_id) {
    throw new Error("invalid account deletion intent");
  }
  return existing.job_id;
}

async function admitAccountDeletion(
  c: RequestContext,
  context: SignedAuthContext,
): Promise<Response> {
  if (context.authority !== "better-auth") {
    return c.json({ error: "account deletion requires Better Auth" }, 409);
  }
  let feedback: AccountDeletionFeedback;
  try {
    feedback = await accountDeletionFeedback(c.req.raw);
  } catch (error) {
    return c.json(
      {
        error:
          error instanceof Error && error.message.includes("too large")
            ? "request body too large"
            : "invalid request",
      },
      error instanceof Error && error.message.includes("too large") ? 413 : 400,
    );
  }

  const now = Math.floor(Date.now() / 1_000);
  try {
    if (await activeDeletionTombstone(c.env, context.uid, now)) {
      return c.json({ status: "ok", message: "Account deletion started" });
    }
    const existingJobId = await existingDeletionJobId(c.env, context.uid);
    if (existingJobId) {
      return c.json({ status: "ok", message: "Account deletion started" });
    }
    await assertCloudflareOwnedAccount(c.env, context.uid);
    await assertExternalProviderCleanupConfigured(c.env, context.uid);
    const jobId = crypto.randomUUID();
    const inserted = await c.env.APP_DB.prepare(
      `INSERT OR IGNORE INTO cf_account_deletion_intents
         (uid, job_id, status, phase, reason, reason_details, attempts,
          lease_token, lease_until, next_attempt_at, settled_at, last_error,
          created_at, updated_at)
       VALUES (?, ?, 'pending', 'quiescing', ?, ?, 0, NULL, NULL, ?, NULL,
               NULL, ?, ?)`,
    )
      .bind(
        context.uid,
        jobId,
        feedback.reason,
        feedback.reasonDetails,
        now + FENCE_QUIESCENCE_SECONDS,
        now,
        now,
      )
      .run();
    if (inserted.meta?.changes !== 1) {
      const racedJobId = await existingDeletionJobId(c.env, context.uid);
      if (!racedJobId) {
        throw new Error("account deletion intent conflict");
      }
      return c.json({ status: "ok", message: "Account deletion started" });
    }
    await queueAccountDeletion(c.env, jobId, FENCE_QUIESCENCE_SECONDS);
    return c.json({ status: "ok", message: "Account deletion started" });
  } catch (error) {
    const providerCleanup =
      error instanceof Error &&
      (error.message === "Stripe cleanup credential unavailable" ||
        error.message === "invalid Stripe subscription id");
    return c.json(
      {
        error: providerCleanup
          ? "external_provider_cleanup_required"
          : "account_deletion_unavailable",
      },
      503,
    );
  }
}

export function registerAccountDeletionRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: (
    c: Context<{ Bindings: JobsEnv }>,
  ) => Promise<SignedAuthContext | null>,
) {
  app.delete("/v1/users/delete-account", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    return admitAccountDeletion(c, context);
  });

  // The legacy path was a Cloud Tasks/OIDC handler. In the Cloudflare profile
  // the durable queue is the source of truth, so this compatibility boundary
  // accepts only a Better Auth principal and advances that principal's own
  // D1 deletion intent. It never accepts a caller-supplied uid and cannot
  // trigger another account's job.
  app.post("/v1/users/account-deletion-wipes/run", async (c) => {
    const context = await requestContext(c);
    if (!context || context.authority !== "better-auth") {
      return c.json({ error: "unauthorized" }, 401);
    }
    let raw: unknown;
    try {
      raw = JSON.parse(await readBoundedRequestBody(c.req.raw));
    } catch {
      return c.json({ error: "invalid request" }, 400);
    }
    const payload =
      raw && typeof raw === "object" && !Array.isArray(raw)
        ? (raw as Record<string, unknown>)
        : null;
    const jobId = typeof payload?.job_id === "string" ? payload.job_id : "";
    if (!ACCOUNT_DELETION_RUN_JOB_ID.test(jobId)) {
      return c.json({ error: "invalid request" }, 400);
    }

    try {
      const intent = await readIntentByJobId(c.env, jobId);
      // Keep the old handler's non-disclosure behavior for unknown or
      // cross-account job IDs: acknowledge without mutating any state.
      if (!intent || intent.uid !== context.uid) {
        return c.json({ status: "dropped", reason: "unknown_job" });
      }
      const message = {
        body: accountDeletionMessage(jobId),
        attempts: 0,
        ack() {},
        retry() {},
      } as unknown as Message<JobMessage>;
      await processAccountDeletionMessage(message, c.env);

      const tombstone = await activeDeletionTombstone(
        c.env,
        context.uid,
        Math.floor(Date.now() / 1_000),
      );
      if (tombstone) return c.json({ status: "done" });
      const remaining = await readIntentByJobId(c.env, jobId);
      if (!remaining) return c.json({ status: "dropped", reason: "completed" });
      return c.json({ status: "queued" });
    } catch {
      return c.json({ status: "retry" }, 503);
    }
  });
}

async function readIntentByJobId(
  env: JobsEnv,
  jobId: string,
): Promise<ParsedAccountDeletionIntent | null> {
  const row = await env.APP_DB.prepare(
    `SELECT uid, job_id, status, phase, attempts, lease_token, lease_until,
            next_attempt_at, settled_at, created_at
     FROM cf_account_deletion_intents WHERE job_id = ?`,
  )
    .bind(jobId)
    .first<AccountDeletionIntent>();
  return row ? accountDeletionIntent(row) : null;
}

async function claimIntent(
  env: JobsEnv,
  jobId: string,
  now: number,
): Promise<ParsedAccountDeletionIntent | null> {
  const leaseToken = crypto.randomUUID();
  const claimed = await env.APP_DB.prepare(
    `UPDATE cf_account_deletion_intents
     SET status = 'running', attempts = attempts + 1, lease_token = ?,
         lease_until = ?, last_error = NULL, updated_at = ?
     WHERE job_id = ?
       AND ((status IN ('pending', 'failed') AND next_attempt_at <= ?)
         OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?))`,
  )
    .bind(leaseToken, now + INTENT_LEASE_SECONDS, now, jobId, now, now)
    .run();
  if (claimed.meta?.changes !== 1) return null;
  const intent = await readIntentByJobId(env, jobId);
  if (!intent || intent.leaseToken !== leaseToken) {
    throw new Error("account deletion lease was not persisted");
  }
  return intent;
}

function prefixFor(pattern: string, uid: string): string {
  return pattern.replace("{uid}", uid);
}

async function assertStorageKeysBoundToAccount(env: JobsEnv, uid: string) {
  const prefixes = ACCOUNT_DELETION_R2_PREFIX_PATTERNS.map((pattern) =>
    prefixFor(pattern, uid),
  );
  const storagePrefixes = [
    ...prefixes,
    ...ACCOUNT_DELETION_CHAT_FILES_PREFIX_PATTERNS.map((pattern) =>
      prefixFor(pattern, uid),
    ),
  ];
  const checks = [
    ["cf_asset_objects", "storage_key"],
    ["cf_asset_cleanup_tasks", "storage_key"],
    ["cf_sync_playback_objects", "storage_key"],
    ["cf_sync_job_files", "object_key"],
    ["cf_import_jobs", "source_object_key"],
    ["cf_chat_files", "storage_key"],
    ["cf_audio_merge_jobs", "source_prefix"],
    ["cf_audio_merge_jobs", "artifact_key"],
    ["cf_audio_merge_legacy_jobs", "source_prefix"],
    ["cf_audio_merge_legacy_jobs", "artifact_key"],
  ] as const;
  for (const [table, column] of checks) {
    const columnPrefixPredicate = storagePrefixes
      .map(() => `instr(${column}, ?) = 1`)
      .join(" OR ");
    const row = await env.APP_DB.prepare(
      `SELECT ${column} AS storage_key FROM ${table}
       WHERE uid = ? AND ${column} IS NOT NULL
         AND NOT (${columnPrefixPredicate}) LIMIT 1`,
    )
      .bind(uid, ...storagePrefixes)
      .first<{ storage_key?: unknown }>();
    if (row) throw new Error("account storage key escaped uid prefix");
  }
}

async function purgeOneR2Page(env: JobsEnv, uid: string): Promise<boolean> {
  const surfaces = [
    ...ACCOUNT_DELETION_CONVERSATION_RECORDING_PREFIX_PATTERNS.map(
      (pattern) => ({
        bucket: env.CONVERSATION_RECORDINGS,
        prefix: prefixFor(pattern, uid),
      }),
    ),
    ...ACCOUNT_DELETION_R2_PREFIX_PATTERNS.map((pattern) => ({
      bucket: env.ASSETS,
      prefix: prefixFor(pattern, uid),
    })),
    ...(env.CHAT_FILES
      ? ACCOUNT_DELETION_CHAT_FILES_PREFIX_PATTERNS.map((pattern) => ({
          bucket: env.CHAT_FILES!,
          prefix: prefixFor(pattern, uid),
        }))
      : []),
    ...ACCOUNT_DELETION_SPEECH_PROFILE_PREFIX_PATTERNS.map((pattern) => ({
      bucket: env.SPEECH_PROFILES,
      prefix: prefixFor(pattern, uid),
    })),
  ];
  for (const surface of surfaces) {
    const listed = await surface.bucket.list({
      prefix: surface.prefix,
      limit: R2_DELETE_BATCH_SIZE,
    });
    const keys = listed.objects.map(({ key }) => key);
    if (!keys.length) continue;
    await surface.bucket.delete(keys);
    return true;
  }
  return false;
}

async function purgeOneD1Batch(env: JobsEnv, uid: string): Promise<number> {
  const results = await env.APP_DB.batch(
    ACCOUNT_DELETION_D1_PURGE_SURFACES.map((surface) =>
      env.APP_DB.prepare(
        `DELETE FROM ${surface.table}
         WHERE rowid IN (
           SELECT rowid FROM ${surface.table}
           WHERE ${surface.column} = ? LIMIT ?
         )`,
      ).bind(uid, D1_DELETE_BATCH_SIZE),
    ),
  );
  if (results.length !== ACCOUNT_DELETION_D1_PURGE_SURFACES.length) {
    throw new Error("account deletion D1 batch is incomplete");
  }
  let changes = 0;
  for (const result of results) {
    if (!result.success) throw new Error("account deletion D1 purge failed");
    const count = Number(result.meta?.changes ?? 0);
    if (!Number.isSafeInteger(count) || count < 0) {
      throw new Error("account deletion D1 purge returned invalid changes");
    }
    changes += count;
  }
  return changes;
}

async function releaseIntent(
  env: JobsEnv,
  intent: ParsedAccountDeletionIntent,
  options: {
    phase?: ParsedAccountDeletionIntent["phase"];
    settledAt?: number | null;
    delaySeconds: number;
  },
) {
  if (!intent.leaseToken) throw new Error("account deletion lease is missing");
  const now = Math.floor(Date.now() / 1_000);
  const phase = options.phase || intent.phase;
  const updated = await env.APP_DB.prepare(
    `UPDATE cf_account_deletion_intents
     SET status = 'pending', phase = ?, lease_token = NULL, lease_until = NULL,
         next_attempt_at = ?, settled_at = ?, updated_at = ?
     WHERE job_id = ? AND lease_token = ?`,
  )
    .bind(
      phase,
      now + options.delaySeconds,
      options.settledAt === undefined ? intent.settledAt : options.settledAt,
      now,
      intent.jobId,
      intent.leaseToken,
    )
    .run();
  if (updated.meta?.changes !== 1) {
    throw new Error("account deletion lease was lost");
  }
  await queueAccountDeletion(env, intent.jobId, options.delaySeconds);
}

async function markIntentFailed(
  env: JobsEnv,
  intent: ParsedAccountDeletionIntent,
) {
  if (!intent.leaseToken) return;
  const now = Math.floor(Date.now() / 1_000);
  const delay = Math.min(
    RETRY_MAX_SECONDS,
    RETRY_BASE_SECONDS * 2 ** Math.min(intent.attempts, 5),
  );
  await env.APP_DB.prepare(
    `UPDATE cf_account_deletion_intents
     SET status = 'failed', lease_token = NULL, lease_until = NULL,
         next_attempt_at = ?, last_error = 'account deletion dependency unavailable',
         updated_at = ?
     WHERE job_id = ? AND lease_token = ?`,
  )
    .bind(now + delay, now, intent.jobId, intent.leaseToken)
    .run();
  await queueAccountDeletion(env, intent.jobId, delay);
}

async function authLifecycleRequest(
  env: JobsEnv,
  uid: string,
  method: "GET" | "DELETE",
  path: string,
  requestId: string,
): Promise<Response> {
  const signed = await createSignedAuthContext(
    { uid, authority: "internal", requestId },
    "auth",
    method,
    path,
    env.INTERNAL_ASSERTION_SECRET,
  );
  if (!signed) throw new Error("Auth lifecycle assertion unavailable");
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    AUTH_LIFECYCLE_TIMEOUT_MS,
  );
  try {
    return await env.AUTH.fetch(
      new Request(`https://auth.internal${path}`, {
        method,
        headers: {
          [AUTH_CONTEXT_HEADER]: signed.encoded,
          [AUTH_SIGNATURE_HEADER]: signed.signature,
          "x-request-id": requestId,
        },
        signal: controller.signal,
      }),
    );
  } finally {
    clearTimeout(timeout);
  }
}

function authIdentityResidualIsEmpty(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const residual = value as Record<string, unknown>;
  return ["users", "sessions", "accounts", "deletionVerifications"].every(
    (field) => residual[field] === 0,
  );
}

async function deleteAuthIdentity(
  env: JobsEnv,
  intent: ParsedAccountDeletionIntent,
) {
  const path = `/internal/users/${encodeURIComponent(intent.uid)}`;
  const deleted = await authLifecycleRequest(
    env,
    intent.uid,
    "DELETE",
    path,
    `account-deletion:${intent.jobId}:delete`,
  );
  if (!deleted.ok) throw new Error("Auth identity deletion failed");
  const body = (await deleted.json()) as { residual?: unknown };
  if (!authIdentityResidualIsEmpty(body.residual)) {
    throw new Error("Auth identity residual is not empty");
  }
}

async function transferFenceToTombstone(
  env: JobsEnv,
  intent: ParsedAccountDeletionIntent,
) {
  if (!intent.leaseToken) throw new Error("account deletion lease is missing");
  const now = Math.floor(Date.now() / 1_000);
  const results = await env.APP_DB.batch([
    env.APP_DB.prepare(
      `INSERT INTO cf_account_deletion_tombstones (uid, completed_at, expires_at)
       VALUES (?, ?, ?)
       ON CONFLICT(uid) DO UPDATE SET completed_at = excluded.completed_at,
         expires_at = excluded.expires_at`,
    ).bind(intent.uid, now, now + TOMBSTONE_SECONDS),
    env.APP_DB.prepare(
      "DELETE FROM cf_account_deletion_intents WHERE job_id = ? AND lease_token = ?",
    ).bind(intent.jobId, intent.leaseToken),
  ]);
  if (
    results.length !== 2 ||
    results.some((result) => !result.success) ||
    results[1].meta?.changes !== 1
  ) {
    throw new Error("account deletion tombstone transfer failed");
  }
}

export async function processAccountDeletionMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  if (message.body.kind !== "account_delete" || message.body.uid !== "") {
    throw new Error("invalid account deletion queue message");
  }
  const now = Math.floor(Date.now() / 1_000);
  const intent = await claimIntent(env, message.body.jobId, now);
  if (!intent) {
    message.ack();
    return;
  }
  try {
    if (intent.phase === "quiescing") {
      await assertCloudflareOwnedAccount(env, intent.uid);
      await assertExternalProviderCleanupConfigured(env, intent.uid);
    }
    if (intent.phase !== "identity") {
      await assertStorageKeysBoundToAccount(env, intent.uid);
    }
    if (intent.phase === "quiescing") {
      const remaining = Math.max(
        0,
        intent.createdAt + FENCE_QUIESCENCE_SECONDS - now,
      );
      if (remaining > 0) {
        await releaseIntent(env, intent, { delaySeconds: remaining });
        message.ack();
        return;
      }
      await cleanupExternalProviders(env, intent);
      intent.phase = "purging";
    }
    if (intent.phase === "purging") {
      const removedVectors = await purgeAccountVectorProjections(
        env,
        intent.uid,
      );
      if (removedVectors) {
        await releaseIntent(env, intent, {
          phase: "purging",
          settledAt: null,
          delaySeconds: 1,
        });
        message.ack();
        return;
      }
      const removedR2 = await purgeOneR2Page(env, intent.uid);
      if (removedR2) {
        await releaseIntent(env, intent, {
          phase: "purging",
          settledAt: null,
          delaySeconds: 1,
        });
        message.ack();
        return;
      }
      await purgeOneD1Batch(env, intent.uid);
      const residual = await readAccountProductResidual(env, intent.uid);
      if (!residual.empty) {
        await releaseIntent(env, intent, {
          phase: "purging",
          settledAt: null,
          delaySeconds: 1,
        });
        message.ack();
        return;
      }
      if (intent.settledAt === null) {
        await releaseIntent(env, intent, {
          phase: "purging",
          settledAt: now,
          delaySeconds: ZERO_SCAN_SETTLE_SECONDS,
        });
        message.ack();
        return;
      }
      const settleRemaining = Math.max(
        0,
        intent.settledAt + ZERO_SCAN_SETTLE_SECONDS - now,
      );
      if (settleRemaining > 0) {
        await releaseIntent(env, intent, {
          phase: "purging",
          delaySeconds: settleRemaining,
        });
        message.ack();
        return;
      }
      await releaseIntent(env, intent, {
        phase: "identity",
        settledAt: intent.settledAt,
        delaySeconds: 0,
      });
      message.ack();
      return;
    }
    await deleteAuthIdentity(env, intent);
    await transferFenceToTombstone(env, intent);
    message.ack();
  } catch {
    await markIntentFailed(env, intent);
    message.ack();
  }
}

export async function reconcileAccountDeletions(
  env: JobsEnv,
  now = Math.floor(Date.now() / 1_000),
) {
  const rows = await env.APP_DB.prepare(
    `SELECT job_id FROM cf_account_deletion_intents
     WHERE (status IN ('pending', 'failed') AND next_attempt_at <= ?)
        OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?)
     ORDER BY next_attempt_at, created_at LIMIT ?`,
  )
    .bind(now, now, RECONCILE_BATCH_SIZE)
    .all<{ job_id?: unknown }>();
  let dispatched = 0;
  for (const row of rows.results || []) {
    if (typeof row.job_id !== "string" || !row.job_id) continue;
    if (await queueAccountDeletion(env, row.job_id)) dispatched += 1;
  }
  return dispatched;
}

export async function cleanupExpiredAccountDeletionTombstones(
  env: JobsEnv,
  now = Math.floor(Date.now() / 1_000),
) {
  await env.APP_DB.prepare(
    "DELETE FROM cf_account_deletion_tombstones WHERE expires_at <= ?",
  )
    .bind(now)
    .run();
}
