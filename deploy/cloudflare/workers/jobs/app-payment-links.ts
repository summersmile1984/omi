import type { JobsEnv } from "./env";
import { StripeResponseError, stripeRequest } from "./stripe-client";

const STRIPE_ACCOUNT_ID = /^acct_[A-Za-z0-9]{7,155}$/;
const STRIPE_PRODUCT_ID = /^prod_[A-Za-z0-9]{7,155}$/;
const STRIPE_PRICE_ID = /^price_[A-Za-z0-9]{7,155}$/;
const STRIPE_PAYMENT_LINK_ID = /^plink_[A-Za-z0-9]{7,155}$/;
const STRIPE_CHECKOUT_SESSION_ID = /^cs_[A-Za-z0-9_]{8,156}$/;
const MAX_OWNER_PAYMENT_LINKS = 1_000;
const STRIPE_PAGE_SIZE = 100;

export type AppPaymentLinkRow = {
  app_id: string;
  owner_uid: string;
  stripe_account_id: string;
  stripe_product_id: string;
  stripe_price_id: string;
  stripe_payment_link_id: string;
  payment_link_url: string;
  unit_amount: number;
  currency: string;
  interval: string;
  active: number;
};

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function validHttpsUrl(value: string) {
  try {
    return new URL(value).protocol === "https:";
  } catch {
    return false;
  }
}

function stripeObject(
  value: unknown,
  expectedId: string,
): Record<string, unknown> {
  const object = objectValue(value);
  if (!object || object.id !== expectedId) {
    throw new Error("Stripe app payment object does not match");
  }
  return object;
}

function validAppPaymentLinkRow(row: AppPaymentLinkRow) {
  return (
    row.app_id.length > 0 &&
    row.app_id.length <= 256 &&
    !row.app_id.includes("/") &&
    row.owner_uid.length > 0 &&
    row.owner_uid.length <= 256 &&
    !row.owner_uid.includes("/") &&
    STRIPE_ACCOUNT_ID.test(row.stripe_account_id) &&
    STRIPE_PRODUCT_ID.test(row.stripe_product_id) &&
    STRIPE_PRICE_ID.test(row.stripe_price_id) &&
    STRIPE_PAYMENT_LINK_ID.test(row.stripe_payment_link_id) &&
    row.payment_link_url.length >= 12 &&
    row.payment_link_url.length <= 2_048 &&
    validHttpsUrl(row.payment_link_url) &&
    Number.isSafeInteger(row.unit_amount) &&
    row.unit_amount > 0 &&
    row.currency === "usd" &&
    row.interval === "month" &&
    (Number(row.active) === 0 || Number(row.active) === 1)
  );
}

export async function stripeOwnedAppPaymentLinks(
  env: JobsEnv,
  ownerUid: string,
): Promise<AppPaymentLinkRow[]> {
  const result = await env.APP_DB.prepare(
    `SELECT app_id, owner_uid, stripe_account_id, stripe_product_id,
            stripe_price_id, stripe_payment_link_id, payment_link_url,
            unit_amount, currency, interval, active
     FROM cf_app_payment_links
     WHERE owner_uid = ?
     ORDER BY app_id LIMIT ?`,
  )
    .bind(ownerUid, MAX_OWNER_PAYMENT_LINKS + 1)
    .all<AppPaymentLinkRow>();
  const rows = result.results || [];
  if (rows.length > MAX_OWNER_PAYMENT_LINKS) {
    throw new Error("too many Stripe app payment links");
  }
  if (rows.some((row) => !validAppPaymentLinkRow(row))) {
    throw new Error("invalid Stripe app payment link mapping");
  }
  return rows;
}

export async function stripeAppPaymentLink(
  env: JobsEnv,
  appId: string,
): Promise<AppPaymentLinkRow | null> {
  const row = await env.APP_DB.prepare(
    `SELECT app_id, owner_uid, stripe_account_id, stripe_product_id,
            stripe_price_id, stripe_payment_link_id, payment_link_url,
            unit_amount, currency, interval, active
     FROM cf_app_payment_links
     WHERE app_id = ? LIMIT 1`,
  )
    .bind(appId)
    .first<AppPaymentLinkRow>();
  if (!row) return null;
  if (!validAppPaymentLinkRow(row) || row.app_id !== appId) {
    throw new Error("invalid Stripe app payment link mapping");
  }
  return row;
}

export async function retireOwnedPaidApps(
  env: JobsEnv,
  ownerUid: string,
  now = Math.floor(Date.now() / 1_000),
) {
  await env.APP_DB.prepare(
    `INSERT INTO cf_retired_paid_apps
       (app_id, stripe_payment_link_id, retired_at)
     SELECT app_id, stripe_payment_link_id, ?
     FROM cf_app_payment_links
     WHERE owner_uid = ?
     ON CONFLICT(app_id) DO UPDATE SET
       stripe_payment_link_id = excluded.stripe_payment_link_id,
       retired_at = MIN(cf_retired_paid_apps.retired_at, excluded.retired_at)`,
  )
    .bind(now, ownerUid)
    .run();
}

function verifyPaymentLink(
  raw: unknown,
  row: AppPaymentLinkRow,
): Record<string, unknown> {
  const link = stripeObject(raw, row.stripe_payment_link_id);
  const metadata = objectValue(link.metadata);
  const transferData = objectValue(link.transfer_data);
  if (
    metadata?.app_id !== row.app_id ||
    (metadata.owner_uid !== undefined &&
      metadata.owner_uid !== row.owner_uid) ||
    transferData?.destination !== row.stripe_account_id ||
    typeof link.active !== "boolean"
  ) {
    throw new Error("Stripe app payment link ownership does not match");
  }
  return link;
}

async function expireOpenCheckoutSessions(
  env: JobsEnv,
  row: AppPaymentLinkRow,
  idempotencyPrefix: string,
) {
  const query = new URLSearchParams({
    payment_link: row.stripe_payment_link_id,
    status: "open",
    limit: String(STRIPE_PAGE_SIZE),
  });
  const raw = objectValue(
    await stripeRequest(env, `/v1/checkout/sessions?${query.toString()}`),
  );
  if (!raw || raw.object !== "list" || !Array.isArray(raw.data)) {
    throw new Error("Stripe Checkout Session list is invalid");
  }
  const sessions = raw.data.map((value) => {
    const session = objectValue(value);
    if (
      !session ||
      typeof session.id !== "string" ||
      !STRIPE_CHECKOUT_SESSION_ID.test(session.id) ||
      session.payment_link !== row.stripe_payment_link_id ||
      session.status !== "open"
    ) {
      throw new Error("Stripe Checkout Session mapping does not match");
    }
    return session.id;
  });
  for (const sessionId of sessions) {
    try {
      const expired = stripeObject(
        await stripeRequest(
          env,
          `/v1/checkout/sessions/${encodeURIComponent(sessionId)}/expire`,
          {
            method: "POST",
            idempotencyKey: `${idempotencyPrefix}-session-${sessionId.slice(-48)}`,
          },
        ),
        sessionId,
      );
      if (expired.status !== "expired") {
        throw new Error("Stripe Checkout Session remains open");
      }
    } catch (error) {
      if (!(error instanceof StripeResponseError) || error.status !== 400) {
        throw error;
      }
      const latest = stripeObject(
        await stripeRequest(
          env,
          `/v1/checkout/sessions/${encodeURIComponent(sessionId)}`,
        ),
        sessionId,
      );
      if (latest.status === "open") throw error;
    }
  }
  if (raw.has_more === true) {
    throw new Error("more open Stripe Checkout Sessions remain");
  }
  if (raw.has_more !== false) {
    throw new Error("Stripe Checkout Session pagination is invalid");
  }
}

export async function deactivateAppPaymentLink(
  env: JobsEnv,
  row: AppPaymentLinkRow,
  idempotencyPrefix: string,
) {
  const path = `/v1/payment_links/${encodeURIComponent(row.stripe_payment_link_id)}`;
  let link = verifyPaymentLink(await stripeRequest(env, path), row);
  if (link.active === true) {
    link = verifyPaymentLink(
      await stripeRequest(env, path, {
        method: "POST",
        form: new URLSearchParams({ active: "false" }),
        idempotencyKey: `${idempotencyPrefix}-link-${row.stripe_payment_link_id.slice(-48)}`,
      }),
      row,
    );
  }
  if (link.active !== false) {
    throw new Error("Stripe app payment link remains active");
  }
  await expireOpenCheckoutSessions(env, row, idempotencyPrefix);
}
