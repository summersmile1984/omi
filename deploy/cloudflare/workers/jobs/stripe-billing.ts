import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import { validAccountDeletionUid } from "./account-deletion-residual";
import type { JobMessage, JobsEnv } from "./env";
import {
  StripeConfigurationError,
  StripeResponseError,
  stripeRequest,
} from "./stripe-client";

const MAX_PAYMENT_REQUEST_BYTES = 4_096;
const MAX_WEBHOOK_BYTES = 128 * 1_024;
const WEBHOOK_TOLERANCE_SECONDS = 5 * 60;
const WEBHOOK_RETRY_SECONDS = 60;
const WEBHOOK_RECONCILE_LIMIT = 50;
const WEBHOOK_MAX_SCHEDULED_ATTEMPTS = 10;
const ISOLATED_STAGING_MANIFEST = "isolated-staging-v1";
const SUPPORTED_WEBHOOK_TYPES = new Set([
  "checkout.session.completed",
  "customer.subscription.created",
  "customer.subscription.updated",
  "customer.subscription.deleted",
]);
const PAID_PLANS = new Set([
  "unlimited",
  "plus",
  "unlimited_v2",
  "operator",
  "architect",
]);
const ACTIVE_STRIPE_STATUSES = new Set(["active", "trialing"]);
const STRIPE_EVENT_ID = /^evt_[A-Za-z0-9]{8,156}$/;
const STRIPE_SESSION_ID = /^cs_[A-Za-z0-9_]{8,156}$/;
const STRIPE_SUBSCRIPTION_ID = /^sub_[A-Za-z0-9]{8,156}$/;
const STRIPE_CUSTOMER_ID = /^cus_[A-Za-z0-9]{8,156}$/;
const STRIPE_PRICE_ID = /^price_[A-Za-z0-9]{8,156}$/;

type RequestContext = (
  c: Context<{ Bindings: JobsEnv }>,
) => Promise<SignedAuthContext | null>;

type StripeWebhookRow = {
  event_id: string;
  event_type: string;
  object_id: string;
  uid_hint: string | null;
  customer_id: string | null;
  subscription_id: string | null;
  payload_sha256: string;
  status: "pending" | "processed" | "ignored" | "failed";
  attempts: number;
};

type SubscriptionRow = {
  plan: string;
  status: string;
  current_period_end: number | null;
  stripe_subscription_id: string | null;
  current_price_id: string | null;
  cancel_at_period_end: number;
};

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function stringValue(value: unknown, pattern: RegExp, label: string): string {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new Error(`invalid Stripe ${label}`);
  }
  return value;
}

function optionalInteger(value: unknown): number | null {
  return Number.isSafeInteger(value) && Number(value) >= 0
    ? Number(value)
    : null;
}

function publicApiBaseUrl(env: JobsEnv): string {
  const raw = env.PUBLIC_API_BASE_URL?.trim();
  if (!raw) throw new Error("public API URL unavailable");
  const parsed = new URL(raw);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password) {
    throw new Error("public API URL unavailable");
  }
  return parsed.toString().replace(/\/$/, "");
}

function webhookSecrets(env: JobsEnv): string[] {
  const values = [env.STRIPE_WEBHOOK_SECRET, env.STRIPE_WEBHOOK_SECRET_PREVIOUS]
    .map((value) => value?.trim())
    .filter((value): value is string => Boolean(value));
  if (
    values.length === 0 ||
    values.some(
      (value) =>
        !value.startsWith("whsec_") ||
        value.length > 512 ||
        /[^\x21-\x7e]/.test(value),
    )
  ) {
    throw new StripeConfigurationError("Stripe webhook credential unavailable");
  }
  return [...new Set(values)];
}

function hexBytes(value: string): Uint8Array | null {
  if (!/^[a-f0-9]{64}$/i.test(value)) return null;
  const result = new Uint8Array(32);
  for (let index = 0; index < result.length; index += 1) {
    result[index] = Number.parseInt(value.slice(index * 2, index * 2 + 2), 16);
  }
  return result;
}

function ownedArrayBuffer(value: Uint8Array): ArrayBuffer {
  return Uint8Array.from(value).buffer;
}

async function hmacSha256(secret: string, value: Uint8Array) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return new Uint8Array(
    await crypto.subtle.sign("HMAC", key, ownedArrayBuffer(value)),
  );
}

function constantTimeEqual(left: Uint8Array, right: Uint8Array): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left[index] ^ right[index];
  }
  return difference === 0;
}

export async function verifyStripeWebhookSignature(
  rawBody: Uint8Array,
  signatureHeader: string | null,
  secrets: readonly string[],
  nowSeconds = Math.floor(Date.now() / 1_000),
): Promise<boolean> {
  if (!signatureHeader || secrets.length === 0) return false;
  let timestamp: number | null = null;
  const signatures: Uint8Array[] = [];
  for (const part of signatureHeader.split(",")) {
    const separator = part.indexOf("=");
    if (separator <= 0) continue;
    const key = part.slice(0, separator).trim();
    const value = part.slice(separator + 1).trim();
    if (key === "t" && /^\d{1,12}$/.test(value)) timestamp = Number(value);
    if (key === "v1") {
      const bytes = hexBytes(value);
      if (bytes) signatures.push(bytes);
    }
  }
  if (
    timestamp === null ||
    signatures.length === 0 ||
    Math.abs(nowSeconds - timestamp) > WEBHOOK_TOLERANCE_SECONDS
  ) {
    return false;
  }
  const timestampBytes = new TextEncoder().encode(`${timestamp}.`);
  const signedPayload = new Uint8Array(timestampBytes.length + rawBody.length);
  signedPayload.set(timestampBytes);
  signedPayload.set(rawBody, timestampBytes.length);
  for (const secret of secrets) {
    const expected = await hmacSha256(secret, signedPayload);
    if (
      signatures.some((candidate) => constantTimeEqual(candidate, expected))
    ) {
      return true;
    }
  }
  return false;
}

async function sha256Hex(value: Uint8Array): Promise<string> {
  const digest = new Uint8Array(
    await crypto.subtle.digest("SHA-256", ownedArrayBuffer(value)),
  );
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}

async function boundedRequestBody(request: Request, limit: number) {
  const declared = Number(request.headers.get("content-length"));
  if (Number.isFinite(declared) && (declared < 0 || declared > limit)) {
    throw new Error("request body too large");
  }
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) {
        await reader.cancel();
        throw new Error("request body too large");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const raw = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    raw.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return raw;
}

async function boundedJsonBody(request: Request) {
  const raw = await boundedRequestBody(request, MAX_PAYMENT_REQUEST_BYTES);
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
}

function paymentError(c: Context, error: unknown): Response {
  if (
    error instanceof StripeResponseError &&
    error.status >= 400 &&
    error.status < 500
  ) {
    return c.json(
      { detail: error.userMessage || "Stripe rejected the payment request." },
      400,
    );
  }
  return c.json(
    { detail: "Payment service unavailable. Please try again." },
    503,
  );
}

async function priceRow(env: JobsEnv, priceId: string) {
  return env.APP_DB.prepare(
    "SELECT id, plan_id, interval FROM cf_subscription_prices WHERE id = ? AND active = 1",
  )
    .bind(priceId)
    .first<{ id: string; plan_id: string; interval: string }>();
}

async function subscriptionRow(env: JobsEnv, uid: string) {
  return env.APP_DB.prepare(
    "SELECT plan, status, current_period_end, stripe_subscription_id, current_price_id, cancel_at_period_end " +
      "FROM cf_user_subscriptions WHERE uid = ?",
  )
    .bind(uid)
    .first<SubscriptionRow>();
}

async function customerId(env: JobsEnv, uid: string): Promise<string | null> {
  const row = await env.APP_DB.prepare(
    "SELECT stripe_customer_id FROM cf_stripe_customers WHERE uid = ?",
  )
    .bind(uid)
    .first<{ stripe_customer_id?: unknown }>();
  return typeof row?.stripe_customer_id === "string" &&
    STRIPE_CUSTOMER_ID.test(row.stripe_customer_id)
    ? row.stripe_customer_id
    : null;
}

function stripeObject(value: unknown, expectedId?: string) {
  const object = objectValue(value);
  if (!object || (expectedId && object.id !== expectedId)) {
    throw new Error("invalid Stripe response");
  }
  return object;
}

function hostedUrl(value: unknown, hostname: string): string {
  if (typeof value !== "string" || value.length > 4_096) {
    throw new Error("invalid Stripe hosted URL");
  }
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.hostname !== hostname) {
    throw new Error("invalid Stripe hosted URL");
  }
  return parsed.toString();
}

async function promotionCodeId(
  env: JobsEnv,
  promotionCode: string,
): Promise<string> {
  const query = new URLSearchParams({
    code: promotionCode,
    active: "true",
    limit: "1",
  });
  const response = stripeObject(
    await stripeRequest(env, `/v1/promotion_codes?${query}`),
  );
  const data = Array.isArray(response.data) ? response.data : [];
  const first = objectValue(data[0]);
  if (
    !first ||
    typeof first.id !== "string" ||
    !/^promo_[A-Za-z0-9]+$/.test(first.id)
  ) {
    throw new StripeResponseError(400, "Invalid or expired promotion code.");
  }
  return first.id;
}

async function reactivateSubscription(
  env: JobsEnv,
  uid: string,
  row: SubscriptionRow,
  targetPriceId: string,
  idempotencyKey: string,
) {
  const subscriptionId = stringValue(
    row.stripe_subscription_id,
    STRIPE_SUBSCRIPTION_ID,
    "subscription id",
  );
  const current = stripeObject(
    await stripeRequest(
      env,
      `/v1/subscriptions/${encodeURIComponent(subscriptionId)}`,
    ),
    subscriptionId,
  );
  const items = objectValue(current.items)?.data;
  const first = Array.isArray(items) ? objectValue(items[0]) : null;
  const currentPriceId = objectValue(first?.price)?.id;
  if (
    current.status !== "active" ||
    current.cancel_at_period_end !== true ||
    currentPriceId !== targetPriceId
  ) {
    return null;
  }
  const updated = stripeObject(
    await stripeRequest(
      env,
      `/v1/subscriptions/${encodeURIComponent(subscriptionId)}`,
      {
        method: "POST",
        form: new URLSearchParams({ cancel_at_period_end: "false" }),
        idempotencyKey,
      },
    ),
    subscriptionId,
  );
  if (updated.cancel_at_period_end !== false) {
    throw new Error("Stripe subscription was not reactivated");
  }
  const periodEnd = optionalInteger(updated.current_period_end);
  await env.APP_DB.prepare(
    "UPDATE cf_user_subscriptions SET cancel_at_period_end = 0, stripe_status = ?, updated_at = ? " +
      "WHERE uid = ? AND stripe_subscription_id = ?",
  )
    .bind(
      String(updated.status || "active"),
      Math.floor(Date.now() / 1_000),
      uid,
      subscriptionId,
    )
    .run();
  const billingDate = periodEnd
    ? new Intl.DateTimeFormat("en-US", {
        month: "long",
        day: "2-digit",
        year: "numeric",
        timeZone: "UTC",
      }).format(new Date(periodEnd * 1_000))
    : "your next billing date";
  return {
    status: "reactivated",
    message: `Your subscription has been reactivated! No charge now - your plan will automatically renew on ${billingDate}.`,
    next_billing_date: periodEnd,
  };
}

function idempotencyKey(request: Request, prefix: string): string {
  const supplied = request.headers.get("idempotency-key")?.trim();
  if (supplied) {
    if (!/^[A-Za-z0-9._:-]{1,128}$/.test(supplied)) {
      throw new Error("invalid idempotency key");
    }
    return supplied;
  }
  return `${prefix}-${crypto.randomUUID()}`;
}

async function createCheckoutSession(
  c: Context<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
) {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  let body: Record<string, unknown>;
  let requestIdempotencyKey: string;
  try {
    body = objectValue(await boundedJsonBody(c.req.raw)) || {};
    requestIdempotencyKey = idempotencyKey(c.req.raw, "checkout");
  } catch (error) {
    return c.json(
      {
        detail:
          error instanceof Error && error.message === "request body too large"
            ? "Request body too large"
            : "Invalid payment request",
      },
      error instanceof Error && error.message === "request body too large"
        ? 413
        : 400,
    );
  }
  const priceId = typeof body.price_id === "string" ? body.price_id.trim() : "";
  if (!STRIPE_PRICE_ID.test(priceId)) {
    return c.json(
      { detail: priceId ? "Unknown price_id" : "price_id is required" },
      400,
    );
  }
  const price = await priceRow(c.env, priceId);
  if (!price) return c.json({ detail: "Unknown price_id" }, 400);
  const promotionCode =
    typeof body.promotion_code === "string" ? body.promotion_code.trim() : "";
  if (promotionCode.length > 100) {
    return c.json({ detail: "Invalid or expired promotion code." }, 400);
  }
  const current = await subscriptionRow(c.env, context.uid);
  const now = Math.floor(Date.now() / 1_000);
  if (
    current &&
    PAID_PLANS.has(current.plan) &&
    current.status === "active" &&
    (!current.current_period_end || current.current_period_end > now)
  ) {
    if (current.cancel_at_period_end && current.current_price_id !== priceId) {
      return c.json(
        {
          detail:
            "Plan changes are available after the current subscription ends",
        },
        400,
      );
    }
    if (current.current_price_id === priceId) {
      if (!current.cancel_at_period_end) {
        return c.json(
          { detail: "User already has an active subscription for this plan" },
          400,
        );
      }
      try {
        const reactivated = await reactivateSubscription(
          c.env,
          context.uid,
          current,
          priceId,
          requestIdempotencyKey,
        );
        if (reactivated) return c.json(reactivated);
      } catch (error) {
        return paymentError(c, error);
      }
    }
  }
  try {
    const form = new URLSearchParams({
      client_reference_id: context.uid,
      "payment_method_types[0]": "card",
      "line_items[0][price]": priceId,
      "line_items[0][quantity]": "1",
      mode: "subscription",
      success_url: `${publicApiBaseUrl(c.env)}/v1/payments/success?session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: `${publicApiBaseUrl(c.env)}/v1/payments/cancel`,
      "metadata[uid]": context.uid,
      "metadata[sub_type]": price.plan_id,
      "subscription_data[metadata][uid]": context.uid,
      "subscription_data[metadata][sub_type]": price.plan_id,
    });
    if (promotionCode) {
      form.set(
        "discounts[0][promotion_code]",
        await promotionCodeId(c.env, promotionCode),
      );
    } else {
      form.set("allow_promotion_codes", "true");
    }
    const existingCustomer = await customerId(c.env, context.uid);
    if (existingCustomer) {
      form.set("customer", existingCustomer);
      form.set("customer_update[name]", "auto");
      form.set("customer_update[address]", "auto");
    }
    const session = stripeObject(
      await stripeRequest(c.env, "/v1/checkout/sessions", {
        method: "POST",
        form,
        idempotencyKey: requestIdempotencyKey,
      }),
    );
    const sessionId = stringValue(
      session.id,
      STRIPE_SESSION_ID,
      "checkout session id",
    );
    return c.json({
      url: hostedUrl(session.url, "checkout.stripe.com"),
      session_id: sessionId,
    });
  } catch (error) {
    return paymentError(c, error);
  }
}

async function createCustomerPortal(
  c: Context<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
) {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  try {
    let existingCustomer = await customerId(c.env, context.uid);
    if (!existingCustomer) {
      const current = await subscriptionRow(c.env, context.uid);
      if (!current?.stripe_subscription_id) {
        return c.json(
          {
            detail:
              "No Stripe customer found. Please create a subscription first.",
          },
          400,
        );
      }
      const subscriptionId = stringValue(
        current.stripe_subscription_id,
        STRIPE_SUBSCRIPTION_ID,
        "subscription id",
      );
      const subscription = stripeObject(
        await stripeRequest(
          c.env,
          `/v1/subscriptions/${encodeURIComponent(subscriptionId)}`,
        ),
        subscriptionId,
      );
      existingCustomer = stringValue(
        subscription.customer,
        STRIPE_CUSTOMER_ID,
        "customer id",
      );
      await c.env.APP_DB.prepare(
        "INSERT INTO cf_stripe_customers (uid, stripe_customer_id, updated_at) VALUES (?, ?, ?) " +
          "ON CONFLICT(uid) DO UPDATE SET stripe_customer_id = excluded.stripe_customer_id, updated_at = excluded.updated_at",
      )
        .bind(context.uid, existingCustomer, Math.floor(Date.now() / 1_000))
        .run();
    }
    const portal = stripeObject(
      await stripeRequest(c.env, "/v1/billing_portal/sessions", {
        method: "POST",
        form: new URLSearchParams({
          customer: existingCustomer,
          return_url: `${publicApiBaseUrl(c.env)}/v1/payments/portal-return`,
        }),
        idempotencyKey: idempotencyKey(c.req.raw, "portal"),
      }),
    );
    return c.json({ url: hostedUrl(portal.url, "billing.stripe.com") });
  } catch (error) {
    return paymentError(c, error);
  }
}

async function liveCloudflareAccount(env: JobsEnv, uid: string) {
  if (!validAccountDeletionUid(uid)) return false;
  const row = await env.APP_DB.prepare(
    `SELECT state, checkpoint_phase, manifest_id, destination_backend_bound,
            EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) AS deleting,
            EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?) AS deleted
     FROM cf_account_cutover WHERE uid = ?`,
  )
    .bind(uid, uid, uid)
    .first<{
      state?: unknown;
      checkpoint_phase?: unknown;
      manifest_id?: unknown;
      destination_backend_bound?: unknown;
      deleting?: unknown;
      deleted?: unknown;
    }>();
  return (
    row?.state === "new" &&
    row.checkpoint_phase === "completed" &&
    row.manifest_id === ISOLATED_STAGING_MANIFEST &&
    Number(row.destination_backend_bound) === 1 &&
    Number(row.deleting) === 0 &&
    Number(row.deleted) === 0
  );
}

async function webhookEventRow(env: JobsEnv, eventId: string) {
  return env.APP_DB.prepare(
    "SELECT event_id, event_type, object_id, uid_hint, customer_id, subscription_id, payload_sha256, status, attempts " +
      "FROM cf_stripe_webhook_events WHERE event_id = ?",
  )
    .bind(eventId)
    .first<StripeWebhookRow>();
}

function stripeWebhookMessage(eventId: string): JobMessage {
  return {
    jobId: eventId,
    uid: "stripe-webhook",
    kind: "stripe_webhook",
    payload: { eventId },
  };
}

async function queueStripeWebhook(env: JobsEnv, eventId: string) {
  await env.JOBS.send(stripeWebhookMessage(eventId));
}

async function stripeWebhook(c: Context<{ Bindings: JobsEnv }>) {
  let secrets: string[];
  try {
    secrets = webhookSecrets(c.env);
  } catch {
    return c.json({ error: "stripe_webhook_unavailable" }, 503);
  }
  let raw: Uint8Array;
  try {
    raw = await boundedRequestBody(c.req.raw, MAX_WEBHOOK_BYTES);
  } catch {
    return c.json({ detail: "Invalid payload" }, 400);
  }
  if (
    !(await verifyStripeWebhookSignature(
      raw,
      c.req.header("stripe-signature") || null,
      secrets,
    ))
  ) {
    return c.json({ detail: "Invalid signature" }, 400);
  }
  let event: Record<string, unknown>;
  try {
    event = objectValue(
      JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)),
    ) || { invalid: true };
  } catch {
    return c.json({ detail: "Invalid payload" }, 400);
  }
  if (event.object !== "event" || typeof event.type !== "string") {
    return c.json({ detail: "Invalid payload" }, 400);
  }
  if (!SUPPORTED_WEBHOOK_TYPES.has(event.type)) {
    return c.json({ status: "success" });
  }
  let eventId: string;
  let objectId: string;
  try {
    eventId = stringValue(event.id, STRIPE_EVENT_ID, "event id");
    const data = objectValue(event.data);
    const object = objectValue(data?.object);
    objectId = stringValue(
      object?.id,
      event.type === "checkout.session.completed"
        ? STRIPE_SESSION_ID
        : STRIPE_SUBSCRIPTION_ID,
      "event object id",
    );
  } catch {
    return c.json({ detail: "Invalid payload" }, 400);
  }
  const now = Math.floor(Date.now() / 1_000);
  const payloadHash = await sha256Hex(raw);
  const existing = await webhookEventRow(c.env, eventId);
  if (existing && existing.payload_sha256 !== payloadHash) {
    return c.json({ detail: "Invalid payload" }, 400);
  }
  if (existing?.status === "processed" || existing?.status === "ignored") {
    return c.json({ status: "success" });
  }
  if (!existing) {
    await c.env.APP_DB.prepare(
      `INSERT INTO cf_stripe_webhook_events
         (event_id, event_type, object_id, uid_hint, customer_id,
          subscription_id, payload_sha256, status, attempts, next_attempt_at,
          last_error, processed_at, created_at, updated_at)
       VALUES (?, ?, ?, NULL, NULL, NULL, ?, 'pending', 0, ?, NULL, NULL, ?, ?)`,
    )
      .bind(eventId, event.type, objectId, payloadHash, now, now, now)
      .run();
  } else {
    await c.env.APP_DB.prepare(
      "UPDATE cf_stripe_webhook_events SET status = 'pending', last_error = NULL, next_attempt_at = ?, updated_at = ? " +
        "WHERE event_id = ? AND status = 'failed'",
    )
      .bind(now, now, eventId)
      .run();
  }
  try {
    await queueStripeWebhook(c.env, eventId);
  } catch {
    await c.env.APP_DB.prepare(
      "UPDATE cf_stripe_webhook_events SET status = 'failed', last_error = 'queue unavailable', " +
        "next_attempt_at = ?, updated_at = ? WHERE event_id = ?",
    )
      .bind(now + WEBHOOK_RETRY_SECONDS, now, eventId)
      .run();
    return c.json({ error: "stripe_webhook_unavailable" }, 503);
  }
  return c.json({ status: "success" });
}

async function uidForStripeObject(
  env: JobsEnv,
  object: Record<string, unknown>,
): Promise<{ uid: string; customer: string | null }> {
  const metadata = objectValue(object.metadata);
  const metadataUid =
    typeof metadata?.uid === "string" && validAccountDeletionUid(metadata.uid)
      ? metadata.uid
      : null;
  const customer =
    typeof object.customer === "string" &&
    STRIPE_CUSTOMER_ID.test(object.customer)
      ? object.customer
      : null;
  if (metadataUid) return { uid: metadataUid, customer };
  if (customer) {
    const row = await env.APP_DB.prepare(
      "SELECT uid FROM cf_stripe_customers WHERE stripe_customer_id = ?",
    )
      .bind(customer)
      .first<{ uid?: unknown }>();
    if (typeof row?.uid === "string" && validAccountDeletionUid(row.uid)) {
      return { uid: row.uid, customer };
    }
  }
  throw new Error("Stripe object has no mapped user");
}

async function syncSubscription(
  env: JobsEnv,
  event: StripeWebhookRow,
  rawSubscription: unknown,
  uidOverride?: string,
) {
  const subscription = stripeObject(rawSubscription, event.object_id);
  const subscriptionId = stringValue(
    subscription.id,
    STRIPE_SUBSCRIPTION_ID,
    "subscription id",
  );
  const mapped = await uidForStripeObject(env, subscription);
  if (uidOverride && mapped.uid !== uidOverride) {
    throw new Error("Stripe checkout and subscription users do not match");
  }
  const uid = uidOverride || mapped.uid;
  if (!(await liveCloudflareAccount(env, uid))) return "ignored" as const;
  const status =
    typeof subscription.status === "string" ? subscription.status : "";
  if (!status || status.length > 80)
    throw new Error("invalid Stripe subscription status");
  const active = ACTIVE_STRIPE_STATUSES.has(status);
  const items = objectValue(subscription.items)?.data;
  const first = Array.isArray(items) ? objectValue(items[0]) : null;
  const rawPriceId = objectValue(first?.price)?.id;
  const priceId =
    typeof rawPriceId === "string" && STRIPE_PRICE_ID.test(rawPriceId)
      ? rawPriceId
      : null;
  let plan = "basic";
  if (active) {
    if (!priceId) throw new Error("active Stripe subscription has no price");
    const price = await env.APP_DB.prepare(
      "SELECT plan_id FROM cf_subscription_prices WHERE id = ?",
    )
      .bind(priceId)
      .first<{ plan_id?: unknown }>();
    if (typeof price?.plan_id !== "string" || !PAID_PLANS.has(price.plan_id)) {
      throw new Error("active Stripe subscription has unknown price");
    }
    plan = price.plan_id;
  }
  const current = await subscriptionRow(env, uid);
  const now = Math.floor(Date.now() / 1_000);
  const staleInactiveDifferentSubscription =
    !active &&
    current !== null &&
    PAID_PLANS.has(current.plan) &&
    current.status === "active" &&
    current.stripe_subscription_id !== subscriptionId &&
    current.current_period_end !== null &&
    current.current_period_end > now;
  const statements = [];
  if (!staleInactiveDifferentSubscription) {
    if (mapped.customer) {
      statements.push(
        env.APP_DB.prepare(
          "INSERT INTO cf_stripe_customers (uid, stripe_customer_id, updated_at) VALUES (?, ?, ?) " +
            "ON CONFLICT(uid) DO UPDATE SET stripe_customer_id = excluded.stripe_customer_id, updated_at = excluded.updated_at",
        ).bind(uid, mapped.customer, now),
      );
    }
    statements.push(
      env.APP_DB.prepare(
        `INSERT INTO cf_user_subscriptions
           (uid, plan, status, current_period_start, current_period_end,
            stripe_subscription_id, current_price_id, features_json,
            cancel_at_period_end, show_subscription_ui, updated_at,
            stripe_status, stripe_event_id)
         VALUES (?, ?, 'active', ?, ?, ?, ?, '[]', ?, 1, ?, ?, ?)
         ON CONFLICT(uid) DO UPDATE SET
           plan = excluded.plan,
           status = excluded.status,
           current_period_start = excluded.current_period_start,
           current_period_end = excluded.current_period_end,
           stripe_subscription_id = excluded.stripe_subscription_id,
           current_price_id = excluded.current_price_id,
           cancel_at_period_end = excluded.cancel_at_period_end,
           show_subscription_ui = excluded.show_subscription_ui,
           updated_at = excluded.updated_at,
           stripe_status = excluded.stripe_status,
           stripe_event_id = excluded.stripe_event_id`,
      ).bind(
        uid,
        plan,
        optionalInteger(subscription.current_period_start),
        optionalInteger(subscription.current_period_end),
        subscriptionId,
        active ? priceId : null,
        subscription.cancel_at_period_end === true ? 1 : 0,
        now,
        status,
        event.event_id,
      ),
    );
  }
  statements.push(
    env.APP_DB.prepare(
      "UPDATE cf_stripe_webhook_events SET uid_hint = ?, customer_id = ?, subscription_id = ?, " +
        "status = 'processed', processed_at = ?, last_error = NULL, updated_at = ? WHERE event_id = ?",
    ).bind(uid, mapped.customer, subscriptionId, now, now, event.event_id),
  );
  await env.APP_DB.batch(statements);
  return "processed" as const;
}

async function processCheckoutWebhook(env: JobsEnv, event: StripeWebhookRow) {
  const session = stripeObject(
    await stripeRequest(
      env,
      `/v1/checkout/sessions/${encodeURIComponent(event.object_id)}`,
    ),
    event.object_id,
  );
  if (session.mode !== "subscription") return "ignored" as const;
  const metadata = objectValue(session.metadata);
  const rawUid = session.client_reference_id || metadata?.uid;
  const uid = stringValue(rawUid, /^[A-Za-z0-9_-]{1,128}$/, "checkout user id");
  if (
    !validAccountDeletionUid(uid) ||
    !(await liveCloudflareAccount(env, uid))
  ) {
    return "ignored" as const;
  }
  const customer = stringValue(
    session.customer,
    STRIPE_CUSTOMER_ID,
    "customer id",
  );
  const subscriptionId = stringValue(
    session.subscription,
    STRIPE_SUBSCRIPTION_ID,
    "subscription id",
  );
  const current = await subscriptionRow(env, uid);
  if (
    current &&
    PAID_PLANS.has(current.plan) &&
    current.status === "active" &&
    current.stripe_subscription_id &&
    current.stripe_subscription_id !== subscriptionId
  ) {
    const oldId = stringValue(
      current.stripe_subscription_id,
      STRIPE_SUBSCRIPTION_ID,
      "subscription id",
    );
    await stripeRequest(env, `/v1/subscriptions/${encodeURIComponent(oldId)}`, {
      method: "DELETE",
      idempotencyKey: `stripe-webhook-${event.event_id}-cancel-old`,
    });
  }
  await env.APP_DB.prepare(
    "INSERT INTO cf_stripe_customers (uid, stripe_customer_id, updated_at) VALUES (?, ?, ?) " +
      "ON CONFLICT(uid) DO UPDATE SET stripe_customer_id = excluded.stripe_customer_id, updated_at = excluded.updated_at",
  )
    .bind(uid, customer, Math.floor(Date.now() / 1_000))
    .run();
  const subscription = await stripeRequest(
    env,
    `/v1/subscriptions/${encodeURIComponent(subscriptionId)}`,
  );
  const projectedEvent = { ...event, object_id: subscriptionId };
  return syncSubscription(env, projectedEvent, subscription, uid);
}

async function markWebhookIgnored(env: JobsEnv, eventId: string) {
  const now = Math.floor(Date.now() / 1_000);
  await env.APP_DB.prepare(
    "UPDATE cf_stripe_webhook_events SET status = 'ignored', processed_at = ?, last_error = NULL, updated_at = ? " +
      "WHERE event_id = ?",
  )
    .bind(now, now, eventId)
    .run();
}

export async function processStripeWebhookMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  const eventId =
    typeof message.body.payload.eventId === "string"
      ? message.body.payload.eventId
      : "";
  if (!STRIPE_EVENT_ID.test(eventId)) {
    message.ack();
    return;
  }
  const event = await webhookEventRow(env, eventId);
  if (!event || event.status === "processed" || event.status === "ignored") {
    message.ack();
    return;
  }
  const now = Math.floor(Date.now() / 1_000);
  await env.APP_DB.prepare(
    "UPDATE cf_stripe_webhook_events SET attempts = attempts + 1, updated_at = ? WHERE event_id = ?",
  )
    .bind(now, eventId)
    .run();
  try {
    const result =
      event.event_type === "checkout.session.completed"
        ? await processCheckoutWebhook(env, event)
        : await syncSubscription(
            env,
            event,
            await stripeRequest(
              env,
              `/v1/subscriptions/${encodeURIComponent(event.object_id)}`,
            ),
          );
    if (result === "ignored") await markWebhookIgnored(env, eventId);
    message.ack();
  } catch {
    const failedAt = Math.floor(Date.now() / 1_000);
    try {
      await env.APP_DB.prepare(
        "UPDATE cf_stripe_webhook_events SET status = 'failed', last_error = 'stripe webhook projection unavailable', " +
          "next_attempt_at = ?, updated_at = ? WHERE event_id = ?",
      )
        .bind(failedAt + WEBHOOK_RETRY_SECONDS, failedAt, eventId)
        .run();
    } catch {
      // Queue retry remains the authority when the failure marker cannot land.
    }
    throw new Error("stripe webhook projection unavailable");
  }
}

export async function reconcileStripeWebhookEvents(
  env: JobsEnv,
  now = Math.floor(Date.now() / 1_000),
) {
  const result = await env.APP_DB.prepare(
    "SELECT event_id FROM cf_stripe_webhook_events " +
      "WHERE status IN ('pending', 'failed') AND next_attempt_at <= ? AND attempts < ? " +
      "ORDER BY next_attempt_at, created_at LIMIT ?",
  )
    .bind(now, WEBHOOK_MAX_SCHEDULED_ATTEMPTS, WEBHOOK_RECONCILE_LIMIT)
    .all<{ event_id: string }>();
  for (const row of result.results || []) {
    if (!STRIPE_EVENT_ID.test(row.event_id)) continue;
    try {
      await queueStripeWebhook(env, row.event_id);
      await env.APP_DB.prepare(
        "UPDATE cf_stripe_webhook_events SET next_attempt_at = ?, updated_at = ? WHERE event_id = ?",
      )
        .bind(now + WEBHOOK_RETRY_SECONDS, now, row.event_id)
        .run();
    } catch {
      // The durable row remains eligible for the next scheduled pass.
    }
  }
}

export function registerStripeBillingRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
) {
  app.post("/v1/payments/checkout-session", (c) =>
    createCheckoutSession(c, requestContext),
  );
  app.post("/v1/payments/customer-portal", (c) =>
    createCustomerPortal(c, requestContext),
  );
  app.post("/v1/stripe/webhook", stripeWebhook);
}
