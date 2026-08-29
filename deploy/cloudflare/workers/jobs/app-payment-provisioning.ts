import type { JobsEnv } from "./env";
import type { AppPaymentLinkRow } from "./app-payment-links";
import { stripeRequest, stripeSecretKey } from "./stripe-client";

const STRIPE_ACCOUNT_ID = /^acct_[A-Za-z0-9]{7,155}$/;
const STRIPE_PRODUCT_ID = /^prod_[A-Za-z0-9]{7,155}$/;
const STRIPE_PRICE_ID = /^price_[A-Za-z0-9]{7,155}$/;
const STRIPE_PAYMENT_LINK_ID = /^plink_[A-Za-z0-9]{7,155}$/;

type CreatorPaymentProfile = {
  stripe_account_id: string | null;
  stripe_charges_enabled: number;
  stripe_payouts_enabled: number;
  stripe_details_submitted: number;
  stripe_onboarding_complete: number;
};

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function metadataMatches(value: unknown, appId: string, ownerUid: string) {
  const metadata = objectValue(value);
  return metadata?.app_id === appId && metadata.owner_uid === ownerUid;
}

function validHttpsUrl(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 2_048) return false;
  try {
    return new URL(value).protocol === "https:";
  } catch {
    return false;
  }
}

async function creatorStripeAccount(
  env: JobsEnv,
  ownerUid: string,
): Promise<string> {
  stripeSecretKey(env);
  const profile = await env.APP_DB.prepare(
    `SELECT stripe_account_id, stripe_charges_enabled,
            stripe_payouts_enabled, stripe_details_submitted,
            stripe_onboarding_complete
     FROM cf_creator_payment_profiles WHERE uid = ? LIMIT 1`,
  )
    .bind(ownerUid)
    .first<CreatorPaymentProfile>();
  if (
    !profile ||
    typeof profile.stripe_account_id !== "string" ||
    !STRIPE_ACCOUNT_ID.test(profile.stripe_account_id) ||
    Number(profile.stripe_charges_enabled) !== 1 ||
    Number(profile.stripe_payouts_enabled) !== 1 ||
    Number(profile.stripe_details_submitted) !== 1 ||
    Number(profile.stripe_onboarding_complete) !== 1
  ) {
    throw new Error("creator Stripe account is not ready");
  }
  return profile.stripe_account_id;
}

async function createProduct(
  env: JobsEnv,
  input: {
    appId: string;
    ownerUid: string;
    name: string;
    description: string;
    image: string;
  },
) {
  const form = new URLSearchParams({
    name: `${input.name} Monthly Plan`,
    description: input.description,
    tax_code: "txcd_10103000",
    "metadata[app_id]": input.appId,
    "metadata[owner_uid]": input.ownerUid,
  });
  if (input.image) form.set("images[0]", input.image);
  const product = objectValue(
    await stripeRequest(env, "/v1/products", {
      method: "POST",
      form,
      idempotencyKey: `app-${input.appId}-product`,
    }),
  );
  if (
    !product ||
    typeof product.id !== "string" ||
    !STRIPE_PRODUCT_ID.test(product.id) ||
    !metadataMatches(product.metadata, input.appId, input.ownerUid)
  ) {
    throw new Error("Stripe product response is invalid");
  }
  return product.id;
}

async function createPrice(
  env: JobsEnv,
  input: {
    appId: string;
    ownerUid: string;
    productId: string;
    unitAmount: number;
    idempotencySuffix: string;
  },
) {
  const price = objectValue(
    await stripeRequest(env, "/v1/prices", {
      method: "POST",
      form: new URLSearchParams({
        product: input.productId,
        unit_amount: String(input.unitAmount),
        currency: "usd",
        "recurring[interval]": "month",
        "metadata[app_id]": input.appId,
        "metadata[owner_uid]": input.ownerUid,
      }),
      idempotencyKey: `app-${input.appId}-price-${input.idempotencySuffix}`,
    }),
  );
  const recurring = objectValue(price?.recurring);
  if (
    !price ||
    typeof price.id !== "string" ||
    !STRIPE_PRICE_ID.test(price.id) ||
    price.product !== input.productId ||
    Number(price.unit_amount) !== input.unitAmount ||
    price.currency !== "usd" ||
    recurring?.interval !== "month" ||
    !metadataMatches(price.metadata, input.appId, input.ownerUid)
  ) {
    throw new Error("Stripe price response is invalid");
  }
  return price.id;
}

async function createPaymentLink(
  env: JobsEnv,
  input: {
    appId: string;
    ownerUid: string;
    accountId: string;
    priceId: string;
    idempotencySuffix: string;
  },
) {
  const link = objectValue(
    await stripeRequest(env, "/v1/payment_links", {
      method: "POST",
      form: new URLSearchParams({
        "line_items[0][price]": input.priceId,
        "line_items[0][quantity]": "1",
        "transfer_data[destination]": input.accountId,
        "subscription_data[metadata][app_id]": input.appId,
        "subscription_data[metadata][owner_uid]": input.ownerUid,
        "metadata[app_id]": input.appId,
        "metadata[owner_uid]": input.ownerUid,
      }),
      idempotencyKey: `app-${input.appId}-link-${input.idempotencySuffix}`,
    }),
  );
  const transfer = objectValue(link?.transfer_data);
  if (
    !link ||
    typeof link.id !== "string" ||
    !STRIPE_PAYMENT_LINK_ID.test(link.id) ||
    link.active !== true ||
    !validHttpsUrl(link.url) ||
    transfer?.destination !== input.accountId ||
    !metadataMatches(link.metadata, input.appId, input.ownerUid)
  ) {
    throw new Error("Stripe payment link response is invalid");
  }
  return { id: link.id, url: link.url };
}

export async function setPaidAppLinkActive(
  env: JobsEnv,
  row: Pick<
    AppPaymentLinkRow,
    "app_id" | "owner_uid" | "stripe_account_id" | "stripe_payment_link_id"
  >,
  active: boolean,
  idempotencySuffix: string,
) {
  const link = objectValue(
    await stripeRequest(
      env,
      `/v1/payment_links/${encodeURIComponent(row.stripe_payment_link_id)}`,
      {
        method: "POST",
        form: new URLSearchParams({ active: String(active) }),
        idempotencyKey: `app-${row.app_id}-link-${idempotencySuffix}`,
      },
    ),
  );
  const transfer = objectValue(link?.transfer_data);
  if (
    !link ||
    link.id !== row.stripe_payment_link_id ||
    link.active !== active ||
    transfer?.destination !== row.stripe_account_id ||
    !metadataMatches(link.metadata, row.app_id, row.owner_uid)
  ) {
    throw new Error("Stripe payment link response is invalid");
  }
}

export async function provisionPaidApp(
  env: JobsEnv,
  input: {
    appId: string;
    ownerUid: string;
    name: string;
    description: string;
    image: string;
    unitAmount: number;
    productId?: string;
    idempotencySuffix: string;
  },
): Promise<AppPaymentLinkRow> {
  const accountId = await creatorStripeAccount(env, input.ownerUid);
  const productId =
    input.productId ||
    (await createProduct(env, {
      appId: input.appId,
      ownerUid: input.ownerUid,
      name: input.name,
      description: input.description,
      image: input.image,
    }));
  if (!STRIPE_PRODUCT_ID.test(productId)) {
    throw new Error("Stripe product mapping is invalid");
  }
  const priceId = await createPrice(env, {
    appId: input.appId,
    ownerUid: input.ownerUid,
    productId,
    unitAmount: input.unitAmount,
    idempotencySuffix: input.idempotencySuffix,
  });
  const paymentLink = await createPaymentLink(env, {
    appId: input.appId,
    ownerUid: input.ownerUid,
    accountId,
    priceId,
    idempotencySuffix: input.idempotencySuffix,
  });
  return {
    app_id: input.appId,
    owner_uid: input.ownerUid,
    stripe_account_id: accountId,
    stripe_product_id: productId,
    stripe_price_id: priceId,
    stripe_payment_link_id: paymentLink.id,
    payment_link_url: paymentLink.url,
    unit_amount: input.unitAmount,
    currency: "usd",
    interval: "month",
    active: 1,
  };
}
