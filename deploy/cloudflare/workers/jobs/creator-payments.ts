import { Hono, type Context } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import { validAccountDeletionUid } from "./account-deletion-residual";
import type { JobsEnv } from "./env";
import {
  StripeConfigurationError,
  StripeResponseError,
  stripeRequest,
} from "./stripe-client";
import { verifyStripeWebhookSignature } from "./stripe-billing";

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;

type CreatorPaymentProfile = {
  uid: string;
  stripe_account_id: string | null;
  stripe_charges_enabled: number;
  stripe_payouts_enabled: number;
  stripe_details_submitted: number;
  stripe_onboarding_complete: number;
  paypal_email: string | null;
  paypalme_url: string | null;
  default_payment_method: "stripe" | "paypal" | null;
};

type StripeAccount = {
  id: string;
  chargesEnabled: boolean;
  payoutsEnabled: boolean;
  detailsSubmitted: boolean;
  onboardingComplete: boolean;
  metadataUid: string | null;
};

const STRIPE_ACCOUNT_ID = /^acct_[A-Za-z0-9]{7,155}$/;
const STRIPE_EVENT_ID = /^evt_[A-Za-z0-9]{4,156}$/;
const COUNTRY_CODE = /^[A-Z]{2}$/;
const MAX_JSON_BYTES = 16_000;
const MAX_WEBHOOK_BYTES = 256_000;
const REFRESH_TOKEN_SECONDS = 24 * 60 * 60;
const ISOLATED_STAGING_MANIFEST = "isolated-staging-v1";

let countryCache: {
  expiresAt: number;
  value: { id: string; name: string }[];
} | null = null;

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function publicApiBaseUrl(env: JobsEnv): string {
  const raw = env.PUBLIC_API_BASE_URL?.trim();
  if (!raw) throw new StripeConfigurationError("public API URL unavailable");
  const parsed = new URL(raw);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password) {
    throw new StripeConfigurationError("public API URL unavailable");
  }
  return parsed.toString().replace(/\/$/, "");
}

function connectRefreshSecret(env: JobsEnv): string {
  const value = env.STRIPE_CONNECT_REFRESH_SECRET?.trim();
  if (
    !value ||
    value.length < 32 ||
    value.length > 512 ||
    /[^\x21-\x7e]/.test(value)
  ) {
    throw new StripeConfigurationError(
      "Stripe Connect refresh credential unavailable",
    );
  }
  return value;
}

function connectWebhookSecrets(env: JobsEnv): string[] {
  const values = [
    env.STRIPE_CONNECT_WEBHOOK_SECRET,
    env.STRIPE_CONNECT_WEBHOOK_SECRET_PREVIOUS,
  ]
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
    throw new StripeConfigurationError(
      "Stripe Connect webhook credential unavailable",
    );
  }
  return [...new Set(values)];
}

function paymentError(c: JobsContext, error: unknown): Response {
  if (error instanceof StripeConfigurationError) {
    return c.json({ error: "creator_payments_unavailable" }, 503);
  }
  if (error instanceof StripeResponseError) {
    return c.json(
      { detail: error.userMessage || "Stripe request failed" },
      400,
    );
  }
  return c.json({ error: "creator_payments_unavailable" }, 503);
}

async function boundedBytes(request: Request, limit: number) {
  const declared = Number(request.headers.get("content-length"));
  if (Number.isFinite(declared) && (declared < 0 || declared > limit)) {
    throw new Error("request body too large");
  }
  const body = new Uint8Array(await request.arrayBuffer());
  if (body.byteLength > limit) throw new Error("request body too large");
  return body;
}

async function jsonBody(request: Request): Promise<Record<string, unknown>> {
  const raw = await boundedBytes(request, MAX_JSON_BYTES);
  try {
    const value = objectValue(
      JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)),
    );
    if (!value) throw new Error("invalid JSON body");
    return value;
  } catch {
    throw new Error("invalid JSON body");
  }
}

async function sha256Hex(value: Uint8Array | string): Promise<string> {
  const bytes =
    typeof value === "string" ? new TextEncoder().encode(value) : value;
  const digest = await crypto.subtle.digest(
    "SHA-256",
    Uint8Array.from(bytes).buffer,
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function base64Url(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

async function refreshSignature(
  secret: string,
  accountId: string,
  expiresAt: number,
) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return base64Url(
    new Uint8Array(
      await crypto.subtle.sign(
        "HMAC",
        key,
        new TextEncoder().encode(`${accountId}.${expiresAt}`),
      ),
    ),
  );
}

async function refreshToken(env: JobsEnv, accountId: string) {
  const expiresAt = Math.floor(Date.now() / 1_000) + REFRESH_TOKEN_SECONDS;
  const signature = await refreshSignature(
    connectRefreshSecret(env),
    accountId,
    expiresAt,
  );
  return `${expiresAt}.${signature}`;
}

async function validRefreshToken(
  env: JobsEnv,
  accountId: string,
  token: string | undefined,
) {
  if (!token) return false;
  const separator = token.indexOf(".");
  if (separator <= 0) return false;
  const rawExpiry = token.slice(0, separator);
  const signature = token.slice(separator + 1);
  if (
    !/^\d{10,12}$/.test(rawExpiry) ||
    !/^[A-Za-z0-9_-]{43}$/.test(signature)
  ) {
    return false;
  }
  const expiresAt = Number(rawExpiry);
  const now = Math.floor(Date.now() / 1_000);
  if (expiresAt < now || expiresAt > now + REFRESH_TOKEN_SECONDS + 60)
    return false;
  const expected = await refreshSignature(
    connectRefreshSecret(env),
    accountId,
    expiresAt,
  );
  if (expected.length !== signature.length) return false;
  let difference = 0;
  for (let index = 0; index < expected.length; index += 1) {
    difference |= expected.charCodeAt(index) ^ signature.charCodeAt(index);
  }
  return difference === 0;
}

function parseStripeAccount(
  value: unknown,
  expectedId?: string,
): StripeAccount {
  const account = objectValue(value);
  if (
    !account ||
    typeof account.id !== "string" ||
    !STRIPE_ACCOUNT_ID.test(account.id) ||
    (expectedId !== undefined && account.id !== expectedId) ||
    typeof account.charges_enabled !== "boolean" ||
    typeof account.payouts_enabled !== "boolean" ||
    typeof account.details_submitted !== "boolean"
  ) {
    throw new Error("Stripe account response is invalid");
  }
  const metadata = objectValue(account.metadata);
  const metadataUid =
    typeof metadata?.uid === "string" && validAccountDeletionUid(metadata.uid)
      ? metadata.uid
      : null;
  return {
    id: account.id,
    chargesEnabled: account.charges_enabled,
    payoutsEnabled: account.payouts_enabled,
    detailsSubmitted: account.details_submitted,
    onboardingComplete:
      account.charges_enabled &&
      account.payouts_enabled &&
      account.details_submitted,
    metadataUid,
  };
}

function accountLinkUrl(value: unknown): string {
  const link = objectValue(value);
  if (typeof link?.url !== "string") {
    throw new Error("Stripe account link response is invalid");
  }
  const parsed = new URL(link.url);
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname !== "connect.stripe.com"
  ) {
    throw new Error("Stripe account link response is invalid");
  }
  return parsed.toString();
}

async function readProfile(env: JobsEnv, uid: string) {
  return env.APP_DB.prepare(
    `SELECT uid, stripe_account_id, stripe_charges_enabled,
            stripe_payouts_enabled, stripe_details_submitted,
            stripe_onboarding_complete, paypal_email, paypalme_url,
            default_payment_method
       FROM cf_creator_payment_profiles WHERE uid = ?`,
  )
    .bind(uid)
    .first<CreatorPaymentProfile>();
}

async function profileForStripeAccount(env: JobsEnv, accountId: string) {
  return env.APP_DB.prepare(
    `SELECT uid, stripe_account_id, stripe_charges_enabled,
            stripe_payouts_enabled, stripe_details_submitted,
            stripe_onboarding_complete, paypal_email, paypalme_url,
            default_payment_method
       FROM cf_creator_payment_profiles WHERE stripe_account_id = ?`,
  )
    .bind(accountId)
    .first<CreatorPaymentProfile>();
}

async function saveStripeAccount(
  env: JobsEnv,
  uid: string,
  account: StripeAccount,
  eventId?: string,
) {
  const now = Math.floor(Date.now() / 1_000);
  await env.APP_DB.prepare(
    `INSERT INTO cf_creator_payment_profiles
       (uid, stripe_account_id, stripe_charges_enabled,
        stripe_payouts_enabled, stripe_details_submitted,
        stripe_onboarding_complete, default_payment_method,
        stripe_event_id, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(uid) DO UPDATE SET
       stripe_account_id = excluded.stripe_account_id,
       stripe_charges_enabled = excluded.stripe_charges_enabled,
       stripe_payouts_enabled = excluded.stripe_payouts_enabled,
       stripe_details_submitted = excluded.stripe_details_submitted,
       stripe_onboarding_complete = excluded.stripe_onboarding_complete,
       default_payment_method = COALESCE(
         cf_creator_payment_profiles.default_payment_method,
         excluded.default_payment_method
       ),
       stripe_event_id = COALESCE(excluded.stripe_event_id, cf_creator_payment_profiles.stripe_event_id),
       updated_at = excluded.updated_at`,
  )
    .bind(
      uid,
      account.id,
      Number(account.chargesEnabled),
      Number(account.payoutsEnabled),
      Number(account.detailsSubmitted),
      Number(account.onboardingComplete),
      account.chargesEnabled && account.detailsSubmitted ? "stripe" : null,
      eventId || null,
      now,
    )
    .run();
}

async function syncStripeAccount(env: JobsEnv, uid: string, accountId: string) {
  const account = parseStripeAccount(
    await stripeRequest(env, `/v1/accounts/${encodeURIComponent(accountId)}`),
    accountId,
  );
  if (account.metadataUid && account.metadataUid !== uid) {
    throw new Error("Stripe account ownership mismatch");
  }
  await saveStripeAccount(env, uid, account);
  return account;
}

async function createAccountLink(env: JobsEnv, accountId: string) {
  const base = publicApiBaseUrl(env);
  const token = await refreshToken(env, accountId);
  const form = new URLSearchParams({
    account: accountId,
    refresh_url: `${base}/v1/stripe/refresh/${encodeURIComponent(accountId)}?token=${encodeURIComponent(token)}`,
    return_url: `${base}/v1/stripe/return/${encodeURIComponent(accountId)}`,
    type: "account_onboarding",
  });
  return accountLinkUrl(
    await stripeRequest(env, "/v1/account_links", { method: "POST", form }),
  );
}

async function authenticated(c: JobsContext, requestContext: RequestContext) {
  const context = await requestContext(c);
  if (!context) return null;
  return context;
}

async function createConnectAccount(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const context = await authenticated(c, requestContext);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  try {
    const existing = await readProfile(c.env, context.uid);
    if (existing?.stripe_account_id) {
      return c.json({
        account_id: existing.stripe_account_id,
        url: await createAccountLink(c.env, existing.stripe_account_id),
      });
    }
    const country = (c.req.query("country") || "").trim().toUpperCase();
    if (!country) return c.json({ detail: "Country is required" }, 400);
    if (!COUNTRY_CODE.test(country)) {
      return c.json({ detail: "Invalid country" }, 400);
    }
    const form = new URLSearchParams({
      country,
      "controller[stripe_dashboard][type]": "express",
      "controller[fees][payer]": "application",
      "controller[losses][payments]": "application",
      "tos_acceptance[service_agreement]":
        country === "US" ? "full" : "recipient",
      "capabilities[transfers][requested]": "true",
      "metadata[uid]": context.uid,
      "settings[payouts][schedule][interval]": "monthly",
      "settings[payouts][schedule][monthly_anchor]": "2",
    });
    const idempotencyHash = await sha256Hex(context.uid);
    const account = parseStripeAccount(
      await stripeRequest(c.env, "/v1/accounts", {
        method: "POST",
        form,
        idempotencyKey: `creator-connect-${idempotencyHash}`,
      }),
    );
    if (account.metadataUid !== context.uid) {
      throw new Error("Stripe account ownership mismatch");
    }
    await saveStripeAccount(c.env, context.uid, account);
    const persisted = await readProfile(c.env, context.uid);
    if (persisted?.stripe_account_id !== account.id) {
      throw new Error("Stripe account mapping conflict");
    }
    return c.json({
      account_id: account.id,
      url: await createAccountLink(c.env, account.id),
    });
  } catch (error) {
    return paymentError(c, error);
  }
}

async function onboardingStatus(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const context = await authenticated(c, requestContext);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  try {
    const profile = await readProfile(c.env, context.uid);
    if (!profile?.stripe_account_id)
      return c.json({ onboarding_complete: false });
    const account = await syncStripeAccount(
      c.env,
      context.uid,
      profile.stripe_account_id,
    );
    return c.json({ onboarding_complete: account.onboardingComplete });
  } catch (error) {
    return paymentError(c, error);
  }
}

async function refreshAccountLink(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const context = await authenticated(c, requestContext);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  const accountId = c.req.param("accountId") || "";
  if (!STRIPE_ACCOUNT_ID.test(accountId))
    return c.json({ detail: "Invalid account" }, 400);
  try {
    const profile = await readProfile(c.env, context.uid);
    if (profile?.stripe_account_id !== accountId) {
      return c.json({ error: "forbidden" }, 403);
    }
    return c.json({
      account_id: accountId,
      url: await createAccountLink(c.env, accountId),
    });
  } catch (error) {
    return paymentError(c, error);
  }
}

async function browserRefresh(c: JobsContext) {
  const accountId = c.req.param("accountId") || "";
  if (!STRIPE_ACCOUNT_ID.test(accountId)) return c.text("Invalid account", 400);
  try {
    if (!(await validRefreshToken(c.env, accountId, c.req.query("token")))) {
      return c.text("Invalid or expired refresh link", 403);
    }
    if (!(await profileForStripeAccount(c.env, accountId)))
      return c.text("Account not found", 404);
    return c.redirect(await createAccountLink(c.env, accountId), 303);
  } catch (error) {
    if (error instanceof StripeResponseError)
      return c.text("Unable to refresh Stripe onboarding", 400);
    return c.text("Stripe onboarding is temporarily unavailable", 503);
  }
}

function onboardingHtml(complete: boolean) {
  const title = complete
    ? "Stripe Account Setup Complete"
    : "Stripe Account Setup Incomplete";
  const message = complete
    ? "Your Stripe account has been successfully set up with Omi AI. You can now start receiving payments."
    : "The account setup process was not completed. Please try again in a few minutes. If the issue persists, contact support.";
  return `<!DOCTYPE html><html><head><title>Return to App</title><meta name="viewport" content="width=device-width, initial-scale=1.0"><style>body{display:flex;flex-direction:column;justify-content:center;align-items:center;min-height:100vh;margin:0;padding:20px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;box-sizing:border-box}.heading{font-size:clamp(20px,5vw,24px);font-weight:bold;margin-bottom:20px;color:#333;text-align:center}.message{font-size:clamp(14px,4vw,16px);color:${complete ? "#666" : "#d32f2f"};text-align:center;margin-bottom:30px;max-width:600px;line-height:1.5}.close-instruction{font-size:clamp(14px,4vw,16px);color:#4CAF50;text-align:center;margin-top:20px}</style></head><body><h1 class="heading">${title}</h1><p class="message">${message}</p><p class="close-instruction">You can now close this window and return to the app</p></body></html>`;
}

async function stripeReturn(c: JobsContext) {
  const accountId = c.req.param("accountId") || "";
  if (!STRIPE_ACCOUNT_ID.test(accountId)) return c.text("Invalid account", 400);
  try {
    const profile = await profileForStripeAccount(c.env, accountId);
    if (!profile) return c.text("Account not found", 404);
    const account = await syncStripeAccount(c.env, profile.uid, accountId);
    return c.html(onboardingHtml(account.onboardingComplete));
  } catch (error) {
    if (error instanceof StripeResponseError)
      return c.text("Unable to read Stripe onboarding status", 400);
    return c.text("Stripe onboarding is temporarily unavailable", 503);
  }
}

async function supportedCountries(c: JobsContext) {
  try {
    const now = Date.now();
    if (countryCache && countryCache.expiresAt > now)
      return c.json(countryCache.value);
    const codes = new Set<string>();
    let startingAfter: string | null = null;
    for (let pageNumber = 0; pageNumber < 30; pageNumber += 1) {
      const query = new URLSearchParams({ limit: "10" });
      if (startingAfter) query.set("starting_after", startingAfter);
      const page = objectValue(
        await stripeRequest(c.env, `/v1/country_specs?${query}`),
      );
      if (
        !page ||
        !Array.isArray(page.data) ||
        typeof page.has_more !== "boolean"
      ) {
        throw new Error("Stripe country list is invalid");
      }
      let last: string | null = null;
      for (const raw of page.data) {
        const spec = objectValue(raw);
        if (typeof spec?.id === "string" && COUNTRY_CODE.test(spec.id)) {
          codes.add(spec.id);
          last = spec.id;
        }
      }
      if (!page.has_more) break;
      if (!last) throw new Error("Stripe country list pagination is invalid");
      startingAfter = last;
      if (pageNumber === 29)
        throw new Error("Stripe country list is too large");
    }
    codes.delete("GI");
    codes.add("US");
    const names = new Intl.DisplayNames(["en"], { type: "region" });
    const value = [...codes]
      .sort()
      .map((id) => ({ id, name: names.of(id) || id }));
    countryCache = { expiresAt: now + 7 * 24 * 60 * 60 * 1_000, value };
    return c.json(value);
  } catch (error) {
    return paymentError(c, error);
  }
}

async function savePayPal(c: JobsContext, requestContext: RequestContext) {
  const context = await authenticated(c, requestContext);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  let body: Record<string, unknown>;
  try {
    body = await jsonBody(c.req.raw);
  } catch {
    return c.json({ detail: "Invalid request" }, 400);
  }
  if (
    typeof body.email !== "string" ||
    body.email.length > 254 ||
    typeof body.paypalme_url !== "string" ||
    body.paypalme_url.length > 2040 ||
    /[\u0000-\u001f\u007f]/.test(body.email + body.paypalme_url)
  ) {
    return c.json({ detail: "Invalid request" }, 400);
  }
  const email = body.email.toLowerCase();
  let paypalmeUrl = body.paypalme_url.toLowerCase();
  if (paypalmeUrl && !paypalmeUrl.startsWith("http"))
    paypalmeUrl = `https://${paypalmeUrl}`;
  if (paypalmeUrl.length > 2048)
    return c.json({ detail: "Invalid request" }, 400);
  try {
    const now = Math.floor(Date.now() / 1_000);
    await c.env.APP_DB.prepare(
      `INSERT INTO cf_creator_payment_profiles
         (uid, paypal_email, paypalme_url, default_payment_method, updated_at)
       VALUES (?, ?, ?, 'paypal', ?)
       ON CONFLICT(uid) DO UPDATE SET
         paypal_email = excluded.paypal_email,
         paypalme_url = excluded.paypalme_url,
         default_payment_method = COALESCE(cf_creator_payment_profiles.default_payment_method, 'paypal'),
         updated_at = excluded.updated_at`,
    )
      .bind(context.uid, email, paypalmeUrl, now)
      .run();
    return c.json({ status: "success" });
  } catch {
    return c.json(
      { detail: "Could not save PayPal payment details. Please try again." },
      400,
    );
  }
}

async function getPayPal(c: JobsContext, requestContext: RequestContext) {
  const context = await authenticated(c, requestContext);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  try {
    const profile = await readProfile(c.env, context.uid);
    if (
      !profile ||
      (profile.paypal_email === null && profile.paypalme_url === null)
    ) {
      return c.json(null);
    }
    return c.json({
      email: profile.paypal_email || "",
      paypalme_url: (profile.paypalme_url || "").replace(/^https:\/\//, ""),
    });
  } catch {
    return c.json({ error: "creator_payments_unavailable" }, 503);
  }
}

async function paymentMethodStatus(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const context = await authenticated(c, requestContext);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  try {
    const profile = await readProfile(c.env, context.uid);
    let stripe = "not_connected";
    if (profile?.stripe_account_id) {
      const account = await syncStripeAccount(
        c.env,
        context.uid,
        profile.stripe_account_id,
      );
      stripe = account.onboardingComplete ? "connected" : "incomplete";
    }
    return c.json({
      stripe,
      paypal:
        profile &&
        (profile.paypal_email !== null || profile.paypalme_url !== null)
          ? "connected"
          : "not_connected",
      default: profile?.default_payment_method || null,
    });
  } catch (error) {
    return paymentError(c, error);
  }
}

async function setDefaultPaymentMethod(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const context = await authenticated(c, requestContext);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  let body: Record<string, unknown>;
  try {
    body = await jsonBody(c.req.raw);
  } catch {
    return c.json({ detail: "Invalid request" }, 400);
  }
  if (body.method !== "stripe" && body.method !== "paypal") {
    return c.json({ detail: "Invalid method" }, 400);
  }
  try {
    const now = Math.floor(Date.now() / 1_000);
    await c.env.APP_DB.prepare(
      `INSERT INTO cf_creator_payment_profiles (uid, default_payment_method, updated_at)
       VALUES (?, ?, ?)
       ON CONFLICT(uid) DO UPDATE SET
         default_payment_method = excluded.default_payment_method,
         updated_at = excluded.updated_at`,
    )
      .bind(context.uid, body.method, now)
      .run();
    return c.json({ status: "success" });
  } catch {
    return c.json({ error: "creator_payments_unavailable" }, 503);
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

async function stripeConnectWebhook(c: JobsContext) {
  let secrets: string[];
  try {
    secrets = connectWebhookSecrets(c.env);
  } catch {
    return c.json({ error: "stripe_connect_webhook_unavailable" }, 503);
  }
  let raw: Uint8Array;
  try {
    raw = await boundedBytes(c.req.raw, MAX_WEBHOOK_BYTES);
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
    event =
      objectValue(
        JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)),
      ) || {};
  } catch {
    return c.json({ detail: "Invalid payload" }, 400);
  }
  if (
    event.object !== "event" ||
    typeof event.id !== "string" ||
    !STRIPE_EVENT_ID.test(event.id)
  ) {
    return c.json({ detail: "Invalid payload" }, 400);
  }
  if (event.type !== "account.updated") return c.json({ status: "success" });
  let account: StripeAccount;
  try {
    account = parseStripeAccount(objectValue(event.data)?.object);
  } catch {
    return c.json({ detail: "Invalid payload" }, 400);
  }
  const payloadHash = await sha256Hex(raw);
  try {
    const existing = await c.env.APP_DB.prepare(
      "SELECT payload_sha256 FROM cf_stripe_connect_events WHERE event_id = ?",
    )
      .bind(event.id)
      .first<{ payload_sha256?: unknown }>();
    if (existing) {
      if (existing.payload_sha256 !== payloadHash)
        return c.json({ detail: "Invalid payload" }, 400);
      return c.json({ status: "success" });
    }
    const mapped = await profileForStripeAccount(c.env, account.id);
    if (mapped && account.metadataUid && mapped.uid !== account.metadataUid) {
      return c.json({ detail: "Invalid payload" }, 400);
    }
    const uid = mapped?.uid || account.metadataUid;
    const activeUid =
      uid && (await liveCloudflareAccount(c.env, uid)) ? uid : null;
    const now = Math.floor(Date.now() / 1_000);
    if (!activeUid) {
      await c.env.APP_DB.prepare(
        `INSERT INTO cf_stripe_connect_events
           (event_id, event_type, account_id, uid_hint, payload_sha256, status, processed_at, created_at)
         VALUES (?, 'account.updated', ?, NULL, ?, 'ignored', ?, ?)`,
      )
        .bind(event.id, account.id, payloadHash, now, now)
        .run();
      return c.json({ status: "success" });
    }
    const profile = await readProfile(c.env, activeUid);
    if (
      profile?.stripe_account_id &&
      profile.stripe_account_id !== account.id
    ) {
      return c.json({ detail: "Invalid payload" }, 400);
    }
    await c.env.APP_DB.batch([
      c.env.APP_DB.prepare(
        `INSERT INTO cf_creator_payment_profiles
           (uid, stripe_account_id, stripe_charges_enabled,
            stripe_payouts_enabled, stripe_details_submitted,
            stripe_onboarding_complete, default_payment_method,
            stripe_event_id, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(uid) DO UPDATE SET
           stripe_account_id = excluded.stripe_account_id,
           stripe_charges_enabled = excluded.stripe_charges_enabled,
           stripe_payouts_enabled = excluded.stripe_payouts_enabled,
           stripe_details_submitted = excluded.stripe_details_submitted,
           stripe_onboarding_complete = excluded.stripe_onboarding_complete,
           default_payment_method = COALESCE(
             cf_creator_payment_profiles.default_payment_method,
             excluded.default_payment_method
           ),
           stripe_event_id = excluded.stripe_event_id,
           updated_at = excluded.updated_at`,
      ).bind(
        activeUid,
        account.id,
        Number(account.chargesEnabled),
        Number(account.payoutsEnabled),
        Number(account.detailsSubmitted),
        Number(account.onboardingComplete),
        account.chargesEnabled && account.detailsSubmitted ? "stripe" : null,
        event.id,
        now,
      ),
      c.env.APP_DB.prepare(
        `INSERT INTO cf_stripe_connect_events
           (event_id, event_type, account_id, uid_hint, payload_sha256, status, processed_at, created_at)
         VALUES (?, 'account.updated', ?, ?, ?, 'processed', ?, ?)`,
      ).bind(event.id, account.id, activeUid, payloadHash, now, now),
    ]);
    return c.json({ status: "success" });
  } catch {
    return c.json({ error: "stripe_connect_webhook_unavailable" }, 503);
  }
}

export function registerCreatorPaymentRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
) {
  app.post("/v1/stripe/connect-accounts", (c) =>
    createConnectAccount(c, requestContext),
  );
  app.get("/v1/stripe/onboarded", (c) => onboardingStatus(c, requestContext));
  app.post("/v1/stripe/refresh/:accountId", (c) =>
    refreshAccountLink(c, requestContext),
  );
  app.get("/v1/stripe/refresh/:accountId", browserRefresh);
  app.get("/v1/stripe/return/:accountId", stripeReturn);
  app.get("/v1/stripe/supported-countries", supportedCountries);
  app.post("/v1/stripe/connect/webhook", stripeConnectWebhook);
  app.post("/v1/paypal/payment-details", (c) => savePayPal(c, requestContext));
  app.get("/v1/paypal/payment-details", (c) => getPayPal(c, requestContext));
  app.get("/v1/payment-methods/status", (c) =>
    paymentMethodStatus(c, requestContext),
  );
  app.post("/v1/payment-methods/default", (c) =>
    setDefaultPaymentMethod(c, requestContext),
  );
}
