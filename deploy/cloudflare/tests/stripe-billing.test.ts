import { createHmac } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import jobs from "../workers/jobs/index";
import {
  processStripeWebhookMessage,
  verifyStripeWebhookSignature,
} from "../workers/jobs/stripe-billing";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../workers/shared/auth-context";

type BoundStatement = {
  sql: string;
  args: unknown[];
  execute(): D1Result<unknown>;
};

type D1Result<T> = {
  success: true;
  results: T[];
  meta: { changes: number };
};

type PreparedStatement = BoundStatement & {
  bind(...values: unknown[]): PreparedStatement;
  first<T>(): Promise<T | null>;
  all<T>(): Promise<D1Result<T>>;
  run(): Promise<D1Result<unknown>>;
};

function sqliteValue(value: unknown) {
  return typeof value === "boolean" ? Number(value) : (value as never);
}

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    this.database.exec("PRAGMA foreign_keys = ON");
    const directory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/app",
    );
    for (const filename of readdirSync(directory)
      .filter((value) => value.endsWith(".sql"))
      .sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string): PreparedStatement {
    const build = (args: unknown[] = []): PreparedStatement => ({
      sql,
      args,
      bind: (...values: unknown[]) => build(values),
      first: async <T>() => {
        const row = this.database.prepare(sql).get(...args.map(sqliteValue)) as
          | T
          | undefined;
        return row ?? null;
      },
      all: async <T>() => ({
        success: true,
        results: this.database
          .prepare(sql)
          .all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0 },
      }),
      run: async () => build(args).execute(),
      execute: () => {
        const statement = this.database.prepare(sql);
        if (/^SELECT\b/i.test(sql.trimStart())) {
          return {
            success: true,
            results: statement.all(...args.map(sqliteValue)),
            meta: { changes: 0 },
          };
        }
        const result = statement.run(...args.map(sqliteValue));
        return {
          success: true,
          results: [],
          meta: { changes: Number(result.changes) },
        };
      },
    });
    return build();
  }

  async batch(statements: BoundStatement[]) {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const results = statements.map((statement) => statement.execute());
      this.database.exec("COMMIT");
      return results;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  row<T>(sql: string, ...args: unknown[]): T | null {
    return (
      (this.database.prepare(sql).get(...args.map(sqliteValue)) as
        | T
        | undefined) ?? null
    );
  }

  close() {
    this.database.close();
  }
}

function testEnvironment() {
  const database = new SqliteD1();
  const sent: JobMessage[] = [];
  const env = {
    APP_DB: database as unknown as D1Database,
    JOBS: {
      send: vi.fn(async (message: JobMessage) => {
        sent.push(message);
      }),
    } as unknown as Queue<JobMessage>,
    INTERNAL_ASSERTION_SECRET: "stripe-billing-internal-secret",
    STRIPE_SECRET_KEY: "sk_test_stripe_billing",
    STRIPE_WEBHOOK_SECRET: "whsec_stripe_billing",
    PUBLIC_API_BASE_URL: "https://edge.example.test",
  } as JobsEnv;
  return { database, env, sent };
}

function seedCloudflareAccount(database: SqliteD1, uid = "billing-user") {
  database.database
    .prepare(
      `INSERT INTO cf_account_cutover
         (uid, state, account_generation, ui_generation, api_generation,
          checkpoint_phase, manifest_id, destination_backend_bound, updated_at)
       VALUES (?, 'new', 1, 1, 1, 'completed', 'isolated-staging-v1', 1, 1)`,
    )
    .run(uid);
}

function seedPrice(
  database: SqliteD1,
  priceId = "price_testOperator123",
  plan = "operator",
  interval = "month",
) {
  database.database
    .prepare(
      `INSERT INTO cf_subscription_prices
         (id, plan_id, title, price_string, interval, unit_amount, active, updated_at)
       VALUES (?, ?, 'Monthly', '$20/month', ?, 2000, 1, 1)`,
    )
    .run(priceId, plan, interval);
}

function seedSubscription(
  database: SqliteD1,
  {
    uid = "billing-user",
    plan = "plus",
    priceId = "price_testPlusMonthly123",
    subscriptionId = "sub_testBilling123",
    customerId = "cus_testBilling123",
  } = {},
) {
  database.database
    .prepare(
      `INSERT INTO cf_user_subscriptions
         (uid, plan, status, stripe_subscription_id, current_price_id,
          cancel_at_period_end, show_subscription_ui, updated_at)
       VALUES (?, ?, 'active', ?, ?, 0, 1, 1)`,
    )
    .run(uid, plan, subscriptionId, priceId);
  database.database
    .prepare(
      "INSERT INTO cf_stripe_customers (uid, stripe_customer_id, updated_at) VALUES (?, ?, 1)",
    )
    .run(uid, customerId);
}

function seedPaidApp(database: SqliteD1, appId = "paid-app") {
  database.database
    .prepare(
      `INSERT INTO cf_app_catalog
         (id, approved, disabled, data_json, updated_at)
       VALUES (?, 1, 0, ?, 1)`,
    )
    .run(
      appId,
      JSON.stringify({
        id: appId,
        is_paid: true,
        payment_link: "https://buy.stripe.com/test",
      }),
    );
}

function seedAppSubscription(
  database: SqliteD1,
  {
    uid = "billing-user",
    appId = "paid-app",
    subscriptionId = "sub_testPaidApp123",
    customerId = "cus_testPaidApp123",
    cancelAtPeriodEnd = 0,
  } = {},
) {
  const now = Math.floor(Date.now() / 1_000);
  database.database
    .prepare(
      `INSERT INTO cf_app_subscriptions
         (uid, app_id, stripe_customer_id, stripe_subscription_id, status,
          current_period_start, current_period_end, cancel_at_period_end,
          price_id, created_at, updated_at)
       VALUES (?, ?, ?, ?, 'active', ?, ?, ?, 'price_testPaidApp123', 1, 1)`,
    )
    .run(
      uid,
      appId,
      customerId,
      subscriptionId,
      now - 86_400,
      now + 30 * 86_400,
      cancelAtPeriodEnd,
    );
}

async function billingHeaders(uid: string, path: string, method = "POST") {
  const signed = await createSignedAuthContext(
    { uid, authority: "better-auth", requestId: "stripe-billing-request" },
    "jobs",
    method,
    path,
    "stripe-billing-internal-secret",
  );
  if (!signed) throw new Error("signed billing context unavailable");
  return {
    "content-type": "application/json",
    [AUTH_CONTEXT_HEADER]: signed.encoded,
    [AUTH_SIGNATURE_HEADER]: signed.signature,
  };
}

function stripeSignature(raw: string, secret: string, timestamp: number) {
  return `t=${timestamp},v1=${createHmac("sha256", secret)
    .update(`${timestamp}.${raw}`)
    .digest("hex")}`;
}

function queueMessage(body: JobMessage) {
  const ack = vi.fn();
  const retry = vi.fn();
  return {
    message: { body, ack, retry } as unknown as Message<JobMessage>,
    ack,
    retry,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Cloudflare Stripe billing", () => {
  it("verifies the raw webhook body with current or rotating secrets", async () => {
    const raw = new TextEncoder().encode('{"id":"evt_test"}');
    const now = 1_800_000_000;
    const valid = stripeSignature(
      new TextDecoder().decode(raw),
      "whsec_previous",
      now,
    );

    await expect(
      verifyStripeWebhookSignature(
        raw,
        valid,
        ["whsec_current", "whsec_previous"],
        now,
      ),
    ).resolves.toBe(true);
    await expect(
      verifyStripeWebhookSignature(
        new TextEncoder().encode('{"id": "evt_test"}'),
        valid,
        ["whsec_previous"],
        now,
      ),
    ).resolves.toBe(false);
    await expect(
      verifyStripeWebhookSignature(raw, valid, ["whsec_previous"], now + 301),
    ).resolves.toBe(false);
  });

  it("creates a hosted subscription Checkout Session from the D1 price authority", async () => {
    const state = testEnvironment();
    seedPrice(state.database);
    const requests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
        const normalized = new Request(request, init);
        requests.push(normalized);
        return Response.json({
          id: "cs_test_checkout123",
          url: "https://checkout.stripe.com/c/pay/cs_test_checkout123",
        });
      }),
    );
    try {
      const path = "/v1/payments/checkout-session";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "POST",
          headers: {
            ...(await billingHeaders("billing-user", path)),
            "idempotency-key": "checkout-attempt-1",
          },
          body: JSON.stringify({ price_id: "price_testOperator123" }),
        }),
        state.env,
      );

      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toEqual({
        url: "https://checkout.stripe.com/c/pay/cs_test_checkout123",
        session_id: "cs_test_checkout123",
      });
      expect(requests).toHaveLength(1);
      expect(requests[0].url).toBe(
        "https://api.stripe.com/v1/checkout/sessions",
      );
      expect(requests[0].headers.get("authorization")).toBe(
        `Basic ${btoa("sk_test_stripe_billing:")}`,
      );
      expect(requests[0].headers.get("idempotency-key")).toBe(
        "checkout-attempt-1",
      );
      const form = new URLSearchParams(await requests[0].text());
      expect(form.get("line_items[0][price]")).toBe("price_testOperator123");
      expect(form.get("metadata[uid]")).toBe("billing-user");
      expect(form.get("metadata[sub_type]")).toBe("operator");
      expect(form.get("success_url")).toBe(
        "https://edge.example.test/v1/payments/success?session_id={CHECKOUT_SESSION_ID}",
      );
    } finally {
      state.database.close();
    }
  });

  it("rejects an unconfigured price before any Stripe request", async () => {
    const state = testEnvironment();
    const stripeFetch = vi.fn();
    vi.stubGlobal("fetch", stripeFetch);
    try {
      const path = "/v1/payments/checkout-session";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "POST",
          headers: await billingHeaders("billing-user", path),
          body: JSON.stringify({ price_id: "price_unknown123" }),
        }),
        state.env,
      );

      expect(response.status).toBe(400);
      await expect(response.json()).resolves.toEqual({
        detail: "Unknown price_id",
      });
      expect(stripeFetch).not.toHaveBeenCalled();
    } finally {
      state.database.close();
    }
  });

  it("creates a short-lived customer portal session from D1 customer authority", async () => {
    const state = testEnvironment();
    state.database.database
      .prepare(
        "INSERT INTO cf_stripe_customers (uid, stripe_customer_id, updated_at) VALUES (?, ?, 1)",
      )
      .run("billing-user", "cus_testCustomer123");
    const requests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
        requests.push(new Request(request, init));
        return Response.json({
          id: "bps_test_portal123",
          url: "https://billing.stripe.com/p/session/test_portal123",
        });
      }),
    );
    try {
      const path = "/v1/payments/customer-portal";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "POST",
          headers: await billingHeaders("billing-user", path),
        }),
        state.env,
      );

      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toEqual({
        url: "https://billing.stripe.com/p/session/test_portal123",
      });
      const form = new URLSearchParams(await requests[0].text());
      expect(form.get("customer")).toBe("cus_testCustomer123");
      expect(form.get("return_url")).toBe(
        "https://edge.example.test/v1/payments/portal-return",
      );
    } finally {
      state.database.close();
    }
  });

  it("changes plans immediately with Stripe proration and projects the authoritative subscription", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedSubscription(state.database);
    seedPrice(state.database, "price_testUnlimitedV2123", "unlimited_v2");
    const periodEnd = Math.floor(Date.now() / 1_000) + 30 * 86_400;
    const requests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
        const normalized = new Request(request, init);
        requests.push(normalized);
        const url = normalized.url;
        if (url.includes("/v1/subscription_schedules?")) {
          return Response.json({ object: "list", data: [] });
        }
        if (url.endsWith("/v1/subscriptions/sub_testBilling123")) {
          const modified = normalized.method === "POST";
          return Response.json({
            id: "sub_testBilling123",
            status: "active",
            customer: "cus_testBilling123",
            metadata: {
              uid: "billing-user",
              sub_type: modified ? "unlimited_v2" : "plus",
            },
            items: {
              data: [
                {
                  id: "si_testBillingItem123",
                  price: {
                    id: modified
                      ? "price_testUnlimitedV2123"
                      : "price_testPlusMonthly123",
                  },
                },
              ],
            },
            current_period_start: periodEnd - 30 * 86_400,
            current_period_end: periodEnd,
            cancel_at_period_end: false,
          });
        }
        throw new Error(`unexpected Stripe request ${url}`);
      }),
    );
    try {
      const path = "/v1/payments/upgrade-subscription";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "POST",
          headers: {
            ...(await billingHeaders("billing-user", path)),
            "idempotency-key": "change-plan-1",
          },
          body: JSON.stringify({ price_id: "price_testUnlimitedV2123" }),
        }),
        state.env,
      );

      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toMatchObject({
        status: "success",
        days_remaining: 0,
        schedule_id: null,
        subscription: {
          plan: "unlimited_v2",
          current_price_id: "price_testUnlimitedV2123",
          stripe_subscription_id: "sub_testBilling123",
          limits: { chat_questions_per_month: 1_000 },
        },
      });
      expect(requests.map((request) => new URL(request.url).pathname)).toEqual([
        "/v1/subscriptions/sub_testBilling123",
        "/v1/subscription_schedules",
        "/v1/subscriptions/sub_testBilling123",
      ]);
      const modify = requests[2];
      expect(modify.headers.get("idempotency-key")).toBe(
        "change-plan-1-modify",
      );
      const form = new URLSearchParams(await modify.text());
      expect(form.get("items[0][id]")).toBe("si_testBillingItem123");
      expect(form.get("items[0][price]")).toBe("price_testUnlimitedV2123");
      expect(form.get("proration_behavior")).toBe("always_invoice");
      expect(
        state.database.row<{ plan: string; current_price_id: string }>(
          "SELECT plan, current_price_id FROM cf_user_subscriptions WHERE uid = ?",
          "billing-user",
        ),
      ).toEqual({
        plan: "unlimited_v2",
        current_price_id: "price_testUnlimitedV2123",
      });
    } finally {
      state.database.close();
    }
  });

  it("schedules a same-plan interval change with the required two Stripe schedule calls", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedSubscription(state.database);
    seedPrice(state.database, "price_testPlusMonthly123", "plus");
    seedPrice(state.database, "price_testPlusAnnual123", "plus", "year");
    const now = Math.floor(Date.now() / 1_000);
    const periodEnd = now + 10 * 86_400;
    const requests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
        const normalized = new Request(request, init);
        requests.push(normalized);
        const url = normalized.url;
        if (url.includes("/v1/subscription_schedules?")) {
          return Response.json({ object: "list", data: [] });
        }
        if (url.endsWith("/v1/subscriptions/sub_testBilling123")) {
          return Response.json({
            id: "sub_testBilling123",
            status: "active",
            customer: "cus_testBilling123",
            metadata: { uid: "billing-user", sub_type: "plus" },
            items: {
              data: [
                {
                  id: "si_testBillingItem123",
                  price: { id: "price_testPlusMonthly123" },
                },
              ],
            },
            current_period_start: now - 20 * 86_400,
            current_period_end: periodEnd,
            cancel_at_period_end: false,
          });
        }
        if (url.endsWith("/v1/subscription_schedules")) {
          return Response.json({ id: "sub_sched_testInterval123" });
        }
        if (
          url.endsWith("/v1/subscription_schedules/sub_sched_testInterval123")
        ) {
          return Response.json({
            id: "sub_sched_testInterval123",
            status: "active",
          });
        }
        throw new Error(`unexpected Stripe request ${url}`);
      }),
    );
    try {
      const path = "/v1/payments/upgrade-subscription";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "POST",
          headers: {
            ...(await billingHeaders("billing-user", path)),
            "idempotency-key": "change-interval-1",
          },
          body: JSON.stringify({ price_id: "price_testPlusAnnual123" }),
        }),
        state.env,
      );

      expect(response.status).toBe(200);
      const payload = (await response.json()) as Record<string, unknown>;
      expect(payload).toMatchObject({
        status: "success",
        schedule_id: "sub_sched_testInterval123",
        subscription: { plan: "plus" },
      });
      expect(Number(payload.days_remaining)).toBeGreaterThanOrEqual(9);
      const createForm = new URLSearchParams(await requests[2].text());
      expect(createForm.get("from_subscription")).toBe("sub_testBilling123");
      const updateForm = new URLSearchParams(await requests[3].text());
      expect(updateForm.get("phases[0][items][0][price]")).toBe(
        "price_testPlusMonthly123",
      );
      expect(updateForm.get("phases[1][items][0][price]")).toBe(
        "price_testPlusAnnual123",
      );
      expect(
        state.database.row<{
          stripe_schedule_id: string;
          scheduled_price_id: string;
          schedule_effective_at: number;
        }>(
          "SELECT stripe_schedule_id, scheduled_price_id, schedule_effective_at FROM cf_user_subscriptions WHERE uid = ?",
          "billing-user",
        ),
      ).toEqual({
        stripe_schedule_id: "sub_sched_testInterval123",
        scheduled_price_id: "price_testPlusAnnual123",
        schedule_effective_at: periodEnd,
      });

      const retry = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "POST",
          headers: {
            ...(await billingHeaders("billing-user", path)),
            "idempotency-key": "change-interval-1",
          },
          body: JSON.stringify({ price_id: "price_testPlusAnnual123" }),
        }),
        state.env,
      );
      expect(retry.status).toBe(200);
      await expect(retry.json()).resolves.toMatchObject({
        schedule_id: "sub_sched_testInterval123",
      });
      expect(requests).toHaveLength(5);
      expect(new URL(requests[4].url).pathname).toBe(
        "/v1/subscriptions/sub_testBilling123",
      );
    } finally {
      state.database.close();
    }
  });

  it("rejects desktop-to-consumer changes before contacting Stripe", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedSubscription(state.database, { plan: "architect" });
    seedPrice(state.database, "price_testPlusAnnual123", "plus", "year");
    const stripeFetch = vi.fn();
    vi.stubGlobal("fetch", stripeFetch);
    try {
      const path = "/v1/payments/upgrade-subscription";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "POST",
          headers: await billingHeaders("billing-user", path),
          body: JSON.stringify({ price_id: "price_testPlusAnnual123" }),
        }),
        state.env,
      );

      expect(response.status).toBe(400);
      await expect(response.json()).resolves.toEqual({
        detail:
          "This plan is managed from desktop. Switching to a mobile plan is not available here. Cancel at period end or contact support.",
      });
      expect(stripeFetch).not.toHaveBeenCalled();
    } finally {
      state.database.close();
    }
  });

  it("releases an attached schedule before canceling at period end and stores bounded feedback", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedSubscription(state.database);
    seedPrice(state.database, "price_testPlusMonthly123", "plus");
    const requests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
        const normalized = new Request(request, init);
        requests.push(normalized);
        const url = normalized.url;
        if (url.includes("/v1/subscription_schedules?")) {
          return Response.json({
            object: "list",
            data: [
              {
                id: "sub_sched_testCancel123",
                status: "active",
                subscription: "sub_testBilling123",
              },
            ],
          });
        }
        if (url.endsWith("/release")) {
          return Response.json({ id: "sub_sched_testCancel123" });
        }
        if (url.endsWith("/v1/subscriptions/sub_testBilling123")) {
          return Response.json({
            id: "sub_testBilling123",
            status: "active",
            customer: "cus_testBilling123",
            metadata: { uid: "billing-user", sub_type: "plus" },
            items: {
              data: [
                {
                  id: "si_testBillingItem123",
                  price: { id: "price_testPlusMonthly123" },
                },
              ],
            },
            current_period_start: 1_800_000_000,
            current_period_end: 1_802_592_000,
            cancel_at_period_end: normalized.method === "POST",
          });
        }
        throw new Error(`unexpected Stripe request ${url}`);
      }),
    );
    try {
      const path = "/v1/payments/subscription";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "DELETE",
          headers: {
            ...(await billingHeaders("billing-user", path, "DELETE")),
            "idempotency-key": "cancel-subscription-1",
          },
          body: JSON.stringify({
            reason: "too_expensive",
            reason_details: "Switching plans later",
          }),
        }),
        state.env,
      );

      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toEqual({
        status: "ok",
        message: "Subscription scheduled for cancellation.",
      });
      expect(requests.map((request) => new URL(request.url).pathname)).toEqual([
        "/v1/subscriptions/sub_testBilling123",
        "/v1/subscription_schedules",
        "/v1/subscription_schedules/sub_sched_testCancel123/release",
        "/v1/subscriptions/sub_testBilling123",
      ]);
      const cancelForm = new URLSearchParams(await requests[3].text());
      expect(cancelForm.get("cancel_at_period_end")).toBe("true");
      expect(
        state.database.row<{
          cancel_at_period_end: number;
          cancellation_reason: string;
          cancellation_reason_details: string;
        }>(
          "SELECT cancel_at_period_end, cancellation_reason, cancellation_reason_details FROM cf_user_subscriptions WHERE uid = ?",
          "billing-user",
        ),
      ).toEqual({
        cancel_at_period_end: 1,
        cancellation_reason: "too_expensive",
        cancellation_reason_details: "Switching plans later",
      });
    } finally {
      state.database.close();
    }
  });

  it("verifies, deduplicates, queues, and projects a Checkout webhook", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedPrice(state.database);
    const event = JSON.stringify({
      id: "evt_checkoutWebhook123",
      object: "event",
      type: "checkout.session.completed",
      data: { object: { id: "cs_test_webhook123" } },
    });
    const timestamp = Math.floor(Date.now() / 1_000);
    const response = await jobs.fetch(
      new Request("https://jobs.test/v1/stripe/webhook", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "stripe-signature": stripeSignature(
            event,
            "whsec_stripe_billing",
            timestamp,
          ),
        },
        body: event,
      }),
      state.env,
    );
    expect(response.status).toBe(200);
    expect(state.sent).toHaveLength(1);
    expect(
      state.database.row<{ status: string }>(
        "SELECT status FROM cf_stripe_webhook_events WHERE event_id = ?",
        "evt_checkoutWebhook123",
      )?.status,
    ).toBe("pending");

    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: RequestInfo | URL) => {
        const url = String(request instanceof Request ? request.url : request);
        if (url.endsWith("/v1/checkout/sessions/cs_test_webhook123")) {
          return Response.json({
            id: "cs_test_webhook123",
            mode: "subscription",
            client_reference_id: "billing-user",
            metadata: { uid: "billing-user" },
            customer: "cus_testWebhook123",
            subscription: "sub_testWebhook123",
          });
        }
        if (url.endsWith("/v1/subscriptions/sub_testWebhook123")) {
          return Response.json({
            id: "sub_testWebhook123",
            status: "active",
            customer: "cus_testWebhook123",
            metadata: { uid: "billing-user" },
            items: {
              data: [{ price: { id: "price_testOperator123" } }],
            },
            current_period_start: 1_800_000_000,
            current_period_end: 1_802_592_000,
            cancel_at_period_end: false,
          });
        }
        throw new Error(`unexpected Stripe request ${url}`);
      }),
    );
    const queued = queueMessage(state.sent[0]);
    try {
      await processStripeWebhookMessage(queued.message, state.env);
      expect(queued.ack).toHaveBeenCalledOnce();
      expect(
        state.database.row<{
          plan: string;
          stripe_subscription_id: string;
          stripe_event_id: string;
        }>(
          "SELECT plan, stripe_subscription_id, stripe_event_id FROM cf_user_subscriptions WHERE uid = ?",
          "billing-user",
        ),
      ).toEqual({
        plan: "operator",
        stripe_subscription_id: "sub_testWebhook123",
        stripe_event_id: "evt_checkoutWebhook123",
      });
      expect(
        state.database.row<{ stripe_customer_id: string }>(
          "SELECT stripe_customer_id FROM cf_stripe_customers WHERE uid = ?",
          "billing-user",
        )?.stripe_customer_id,
      ).toBe("cus_testWebhook123");

      const duplicate = await jobs.fetch(
        new Request("https://jobs.test/v1/stripe/webhook", {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "stripe-signature": stripeSignature(
              event,
              "whsec_stripe_billing",
              timestamp,
            ),
          },
          body: event,
        }),
        state.env,
      );
      expect(duplicate.status).toBe(200);
      expect(state.sent).toHaveLength(1);
    } finally {
      state.database.close();
    }
  });

  it("retrieves latest schedule and subscription state before completing a scheduled change", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedSubscription(state.database);
    seedPrice(state.database, "price_testPlusMonthly123", "plus");
    seedPrice(state.database, "price_testPlusAnnual123", "plus", "year");
    state.database.database
      .prepare(
        "UPDATE cf_user_subscriptions SET stripe_schedule_id = ?, scheduled_price_id = ?, " +
          "stripe_schedule_status = 'active', schedule_effective_at = 1802592000 WHERE uid = ?",
      )
      .run(
        "sub_sched_testCompleted123",
        "price_testPlusAnnual123",
        "billing-user",
      );
    const event = JSON.stringify({
      id: "evt_scheduleCompleted123",
      object: "event",
      type: "subscription_schedule.completed",
      data: { object: { id: "sub_sched_testCompleted123" } },
    });
    const timestamp = Math.floor(Date.now() / 1_000);
    const response = await jobs.fetch(
      new Request("https://jobs.test/v1/stripe/webhook", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "stripe-signature": stripeSignature(
            event,
            "whsec_stripe_billing",
            timestamp,
          ),
        },
        body: event,
      }),
      state.env,
    );
    expect(response.status).toBe(200);
    expect(state.sent).toHaveLength(1);

    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: RequestInfo | URL) => {
        const url = String(request instanceof Request ? request.url : request);
        if (
          url.endsWith("/v1/subscription_schedules/sub_sched_testCompleted123")
        ) {
          return Response.json({
            id: "sub_sched_testCompleted123",
            status: "completed",
            subscription: "sub_testBilling123",
            customer: "cus_testBilling123",
            metadata: { uid: "billing-user" },
            phases: [
              {
                start_date: 1_802_592_000,
                items: [{ price: "price_testPlusAnnual123" }],
              },
            ],
          });
        }
        if (
          url.endsWith(
            "/v1/subscription_schedules/sub_sched_testReleasedOld123",
          )
        ) {
          return Response.json({
            id: "sub_sched_testReleasedOld123",
            status: "released",
            released_subscription: "sub_testBilling123",
            customer: "cus_testBilling123",
            metadata: { uid: "billing-user" },
            phases: [],
          });
        }
        if (url.endsWith("/v1/subscriptions/sub_testBilling123")) {
          return Response.json({
            id: "sub_testBilling123",
            status: "active",
            customer: "cus_testBilling123",
            metadata: { uid: "billing-user", sub_type: "plus" },
            items: {
              data: [
                {
                  id: "si_testBillingItem123",
                  price: { id: "price_testPlusAnnual123" },
                },
              ],
            },
            current_period_start: 1_802_592_000,
            current_period_end: 1_834_128_000,
            cancel_at_period_end: false,
          });
        }
        throw new Error(`unexpected Stripe request ${url}`);
      }),
    );
    const queued = queueMessage(state.sent[0]);
    try {
      await processStripeWebhookMessage(queued.message, state.env);
      expect(queued.ack).toHaveBeenCalledOnce();
      expect(
        state.database.row<{
          current_price_id: string;
          stripe_schedule_id: string | null;
          stripe_schedule_status: string;
          stripe_event_id: string | null;
        }>(
          "SELECT current_price_id, stripe_schedule_id, stripe_schedule_status, stripe_event_id " +
            "FROM cf_user_subscriptions WHERE uid = ?",
          "billing-user",
        ),
      ).toEqual({
        current_price_id: "price_testPlusAnnual123",
        stripe_schedule_id: null,
        stripe_schedule_status: "completed",
        stripe_event_id: "evt_scheduleCompleted123",
      });
      expect(
        state.database.row<{ status: string; subscription_id: string }>(
          "SELECT status, subscription_id FROM cf_stripe_webhook_events WHERE event_id = ?",
          "evt_scheduleCompleted123",
        ),
      ).toEqual({
        status: "processed",
        subscription_id: "sub_testBilling123",
      });

      state.database.database
        .prepare(
          "UPDATE cf_user_subscriptions SET stripe_schedule_id = ?, scheduled_price_id = ?, " +
            "stripe_schedule_status = 'active', schedule_effective_at = 1900000000 WHERE uid = ?",
        )
        .run(
          "sub_sched_testNewerSchedule123",
          "price_testPlusAnnual123",
          "billing-user",
        );
      state.database.database
        .prepare(
          `INSERT INTO cf_stripe_webhook_events
             (event_id, event_type, object_id, payload_sha256, status,
              next_attempt_at, created_at, updated_at)
           VALUES (?, 'subscription_schedule.released', ?, ?, 'pending', 1, 1, 1)`,
        )
        .run(
          "evt_scheduleReleasedOld123",
          "sub_sched_testReleasedOld123",
          "0".repeat(64),
        );
      const oldRelease = queueMessage({
        jobId: "evt_scheduleReleasedOld123",
        uid: "stripe-webhook",
        kind: "stripe_webhook",
        payload: { eventId: "evt_scheduleReleasedOld123" },
      });
      await processStripeWebhookMessage(oldRelease.message, state.env);
      expect(oldRelease.ack).toHaveBeenCalledOnce();
      expect(
        state.database.row<{
          stripe_schedule_id: string;
          stripe_schedule_status: string;
        }>(
          "SELECT stripe_schedule_id, stripe_schedule_status FROM cf_user_subscriptions WHERE uid = ?",
          "billing-user",
        ),
      ).toEqual({
        stripe_schedule_id: "sub_sched_testNewerSchedule123",
        stripe_schedule_status: "active",
      });
    } finally {
      state.database.close();
    }
  });

  it("projects a paid-app Checkout entitlement and serves it from the authenticated Worker route", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedPaidApp(state.database);
    state.database.database
      .prepare(
        `INSERT INTO cf_stripe_webhook_events
           (event_id, event_type, object_id, payload_sha256, status,
            next_attempt_at, created_at, updated_at)
         VALUES ('evt_appCheckout123', 'checkout.session.completed',
                 'cs_test_appCheckout123', ?, 'pending', 1, 1, 1)`,
      )
      .run("0".repeat(64));
    const now = Math.floor(Date.now() / 1_000);
    const requests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = new Request(input, init);
        requests.push(request);
        if (
          request.url.endsWith("/v1/checkout/sessions/cs_test_appCheckout123")
        ) {
          return Response.json({
            id: "cs_test_appCheckout123",
            mode: "subscription",
            client_reference_id: "uid_billing-user",
            metadata: { app_id: "paid-app" },
            customer: "cus_testPaidApp123",
            subscription: "sub_testPaidApp123",
          });
        }
        if (request.url.endsWith("/v1/subscriptions/sub_testPaidApp123")) {
          return Response.json({
            id: "sub_testPaidApp123",
            status: "active",
            customer: "cus_testPaidApp123",
            metadata:
              request.method === "POST"
                ? { app_id: "paid-app", uid: "billing-user" }
                : { app_id: "paid-app" },
            items: {
              data: [{ price: { id: "price_testPaidApp123" } }],
            },
            current_period_start: now - 86_400,
            current_period_end: now + 30 * 86_400,
            cancel_at_period_end: false,
          });
        }
        throw new Error(`unexpected Stripe request ${request.url}`);
      }),
    );
    const queued = queueMessage({
      jobId: "evt_appCheckout123",
      uid: "stripe-webhook",
      kind: "stripe_webhook",
      payload: { eventId: "evt_appCheckout123" },
    });
    try {
      await processStripeWebhookMessage(queued.message, state.env);
      expect(queued.ack).toHaveBeenCalledOnce();
      expect(
        state.database.row<{
          uid: string;
          app_id: string;
          status: string;
          stripe_event_id: string;
        }>(
          "SELECT uid, app_id, status, stripe_event_id FROM cf_app_subscriptions WHERE uid = ? AND app_id = ?",
          "billing-user",
          "paid-app",
        ),
      ).toEqual({
        uid: "billing-user",
        app_id: "paid-app",
        status: "active",
        stripe_event_id: "evt_appCheckout123",
      });
      const metadataRequest = requests.find(
        (request) =>
          request.method === "POST" &&
          request.url.endsWith("/v1/subscriptions/sub_testPaidApp123"),
      );
      expect(metadataRequest?.headers.get("idempotency-key")).toBe(
        "stripe-webhook-evt_appCheckout123-app-metadata",
      );
      const metadataForm = new URLSearchParams(await metadataRequest?.text());
      expect(metadataForm.get("metadata[uid]")).toBe("billing-user");
      expect(metadataForm.get("metadata[app_id]")).toBe("paid-app");

      const path = "/v1/apps/paid-app/subscription";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          headers: await billingHeaders("billing-user", path, "GET"),
        }),
        state.env,
      );
      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toEqual({
        subscription: {
          id: "sub_testPaidApp123",
          status: "active",
          current_period_end: now + 30 * 86_400,
          cancel_at_period_end: false,
          price_id: "price_testPaidApp123",
          customer_id: "cus_testPaidApp123",
        },
      });
    } finally {
      state.database.close();
    }
  });

  it("cancels only the caller's projected app subscription and keeps access through period end", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedPaidApp(state.database);
    seedAppSubscription(state.database);
    const now = Math.floor(Date.now() / 1_000);
    const requests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = new Request(input, init);
        requests.push(request);
        return Response.json({
          id: "sub_testPaidApp123",
          status: "active",
          customer: "cus_testPaidApp123",
          metadata: { app_id: "paid-app", uid: "billing-user" },
          items: { data: [{ price: { id: "price_testPaidApp123" } }] },
          current_period_start: now - 86_400,
          current_period_end: now + 30 * 86_400,
          cancel_at_period_end: request.method === "POST",
        });
      }),
    );
    try {
      const path = "/v1/apps/paid-app/subscription";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "DELETE",
          headers: {
            ...(await billingHeaders("billing-user", path, "DELETE")),
            "idempotency-key": "cancel-paid-app-1",
          },
        }),
        state.env,
      );
      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toMatchObject({
        status: "success",
        cancel_at_period_end: true,
        current_period_end: now + 30 * 86_400,
      });
      expect(requests).toHaveLength(2);
      expect(requests[1].headers.get("idempotency-key")).toBe(
        "cancel-paid-app-1",
      );
      expect(
        new URLSearchParams(await requests[1].text()).get(
          "cancel_at_period_end",
        ),
      ).toBe("true");
      expect(
        state.database.row<{ cancel_at_period_end: number }>(
          "SELECT cancel_at_period_end FROM cf_app_subscriptions WHERE uid = ? AND app_id = ?",
          "billing-user",
          "paid-app",
        )?.cancel_at_period_end,
      ).toBe(1);

      const foreign = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "DELETE",
          headers: await billingHeaders("another-user", path, "DELETE"),
        }),
        state.env,
      );
      expect(foreign.status).toBe(404);
      expect(requests).toHaveLength(2);
    } finally {
      state.database.close();
    }
  });

  it("cancels a Checkout subscription that completes after the account-deletion fence", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedPaidApp(state.database);
    state.database.database
      .prepare(
        `INSERT INTO cf_account_deletion_intents
           (uid, job_id, status, phase, attempts, next_attempt_at,
            created_at, updated_at)
         VALUES ('billing-user', 'delete-job', 'pending', 'quiescing', 0, 1, 1, 1)`,
      )
      .run();
    state.database.database
      .prepare(
        `INSERT INTO cf_stripe_webhook_events
           (event_id, event_type, object_id, payload_sha256, status,
            next_attempt_at, created_at, updated_at)
         VALUES ('evt_appFenced123', 'checkout.session.completed',
                 'cs_test_appFenced123', ?, 'pending', 1, 1, 1)`,
      )
      .run("0".repeat(64));
    const requests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = new Request(input, init);
        requests.push(request);
        if (
          request.url.endsWith("/v1/checkout/sessions/cs_test_appFenced123")
        ) {
          return Response.json({
            id: "cs_test_appFenced123",
            mode: "subscription",
            client_reference_id: "uid_billing-user",
            metadata: { app_id: "paid-app" },
            customer: "cus_testFencedApp123",
            subscription: "sub_testFencedApp123",
          });
        }
        return Response.json({
          id: "sub_testFencedApp123",
          status: "active",
          customer: "cus_testFencedApp123",
          metadata: { app_id: "paid-app" },
          cancel_at_period_end: request.method === "POST",
        });
      }),
    );
    const queued = queueMessage({
      jobId: "evt_appFenced123",
      uid: "stripe-webhook",
      kind: "stripe_webhook",
      payload: { eventId: "evt_appFenced123" },
    });
    try {
      await processStripeWebhookMessage(queued.message, state.env);
      expect(queued.ack).toHaveBeenCalledOnce();
      expect(requests.map(({ method }) => method)).toEqual([
        "GET",
        "GET",
        "POST",
      ]);
      expect(requests[2].headers.get("idempotency-key")).toBe(
        "stripe-webhook-evt_appFenced123-fenced-cancel",
      );
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_app_subscriptions WHERE uid = ?",
          "billing-user",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ status: string }>(
          "SELECT status FROM cf_stripe_webhook_events WHERE event_id = ?",
          "evt_appFenced123",
        )?.status,
      ).toBe("ignored");
    } finally {
      state.database.close();
    }
  });

  it("cancels a Checkout subscription that completes after the paid-app owner deletion fence", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedCloudflareAccount(state.database, "creator-user");
    seedPaidApp(state.database);
    state.database.database
      .prepare("UPDATE cf_app_catalog SET owner_uid = ? WHERE id = ?")
      .run("creator-user", "paid-app");
    state.database.database
      .prepare(
        `INSERT INTO cf_account_deletion_intents
           (uid, job_id, status, phase, attempts, next_attempt_at,
            created_at, updated_at)
         VALUES ('creator-user', 'creator-delete-job', 'pending', 'quiescing', 0, 1, 1, 1)`,
      )
      .run();
    state.database.database
      .prepare(
        `INSERT INTO cf_stripe_webhook_events
           (event_id, event_type, object_id, payload_sha256, status,
            next_attempt_at, created_at, updated_at)
         VALUES ('evt_ownerFenced123', 'checkout.session.completed',
                 'cs_test_ownerFenced123', ?, 'pending', 1, 1, 1)`,
      )
      .run("0".repeat(64));
    const requests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = new Request(input, init);
        requests.push(request);
        if (
          request.url.endsWith("/v1/checkout/sessions/cs_test_ownerFenced123")
        ) {
          return Response.json({
            id: "cs_test_ownerFenced123",
            mode: "subscription",
            client_reference_id: "uid_billing-user",
            metadata: { app_id: "paid-app" },
            customer: "cus_testOwnerFenced123",
            subscription: "sub_testOwnerFenced123",
          });
        }
        return Response.json({
          id: "sub_testOwnerFenced123",
          status: "active",
          customer: "cus_testOwnerFenced123",
          metadata: { app_id: "paid-app" },
          cancel_at_period_end: request.method === "POST",
        });
      }),
    );
    const queued = queueMessage({
      jobId: "evt_ownerFenced123",
      uid: "stripe-webhook",
      kind: "stripe_webhook",
      payload: { eventId: "evt_ownerFenced123" },
    });
    try {
      await processStripeWebhookMessage(queued.message, state.env);
      expect(queued.ack).toHaveBeenCalledOnce();
      expect(requests.map(({ method }) => method)).toEqual([
        "GET",
        "GET",
        "POST",
      ]);
      expect(requests[2].headers.get("idempotency-key")).toBe(
        "stripe-webhook-evt_ownerFenced123-fenced-cancel",
      );
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_app_subscriptions WHERE app_id = ?",
          "paid-app",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ status: string }>(
          "SELECT status FROM cf_stripe_webhook_events WHERE event_id = ?",
          "evt_ownerFenced123",
        )?.status,
      ).toBe("ignored");
    } finally {
      state.database.close();
    }
  });

  it("cancels a delayed Checkout after the retired paid-app catalog row is gone", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedPaidApp(state.database);
    state.database.database
      .prepare(
        `INSERT INTO cf_retired_paid_apps
           (app_id, stripe_payment_link_id, retired_at)
         VALUES ('paid-app', 'plink_retiredPaidApp123', 1)`,
      )
      .run();
    state.database.database
      .prepare("DELETE FROM cf_app_catalog WHERE id = ?")
      .run("paid-app");
    state.database.database
      .prepare(
        `INSERT INTO cf_stripe_webhook_events
           (event_id, event_type, object_id, payload_sha256, status,
            next_attempt_at, created_at, updated_at)
         VALUES ('evt_retiredApp123', 'checkout.session.completed',
                 'cs_test_retiredApp123', ?, 'pending', 1, 1, 1)`,
      )
      .run("0".repeat(64));
    const requests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = new Request(input, init);
        requests.push(request);
        if (
          request.url.endsWith("/v1/checkout/sessions/cs_test_retiredApp123")
        ) {
          return Response.json({
            id: "cs_test_retiredApp123",
            mode: "subscription",
            client_reference_id: "uid_billing-user",
            metadata: { app_id: "paid-app" },
            customer: "cus_testRetiredApp123",
            subscription: "sub_testRetiredApp123",
          });
        }
        return Response.json({
          id: "sub_testRetiredApp123",
          status: "active",
          customer: "cus_testRetiredApp123",
          metadata: { app_id: "paid-app" },
          cancel_at_period_end: request.method === "POST",
        });
      }),
    );
    const queued = queueMessage({
      jobId: "evt_retiredApp123",
      uid: "stripe-webhook",
      kind: "stripe_webhook",
      payload: { eventId: "evt_retiredApp123" },
    });
    try {
      await processStripeWebhookMessage(queued.message, state.env);
      expect(queued.ack).toHaveBeenCalledOnce();
      expect(requests.map(({ method }) => method)).toEqual([
        "GET",
        "GET",
        "POST",
      ]);
      expect(requests[2].headers.get("idempotency-key")).toBe(
        "stripe-webhook-evt_retiredApp123-fenced-cancel",
      );
      expect(
        state.database.row<{ status: string }>(
          "SELECT status FROM cf_stripe_webhook_events WHERE event_id = ?",
          "evt_retiredApp123",
        )?.status,
      ).toBe("ignored");
    } finally {
      state.database.close();
    }
  });

  it("revokes an installed paid app when Stripe projects an inactive subscription", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedPaidApp(state.database);
    seedAppSubscription(state.database);
    state.database.database
      .prepare(
        "INSERT INTO cf_user_enabled_apps (uid, app_id, created_at) VALUES (?, ?, 1)",
      )
      .run("billing-user", "paid-app");
    state.database.database
      .prepare(
        `INSERT INTO cf_stripe_webhook_events
           (event_id, event_type, object_id, payload_sha256, status,
            next_attempt_at, created_at, updated_at)
         VALUES ('evt_appCanceled123', 'customer.subscription.deleted',
                 'sub_testPaidApp123', ?, 'pending', 1, 1, 1)`,
      )
      .run("0".repeat(64));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          id: "sub_testPaidApp123",
          status: "canceled",
          customer: "cus_testPaidApp123",
          metadata: { app_id: "paid-app", uid: "billing-user" },
          items: { data: [{ price: { id: "price_testPaidApp123" } }] },
          current_period_start: 1,
          current_period_end: 2,
          cancel_at_period_end: false,
        }),
      ),
    );
    const queued = queueMessage({
      jobId: "evt_appCanceled123",
      uid: "stripe-webhook",
      kind: "stripe_webhook",
      payload: { eventId: "evt_appCanceled123" },
    });
    try {
      await processStripeWebhookMessage(queued.message, state.env);
      expect(
        state.database.row<{ status: string }>(
          "SELECT status FROM cf_app_subscriptions WHERE uid = ? AND app_id = ?",
          "billing-user",
          "paid-app",
        )?.status,
      ).toBe("canceled");
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_user_enabled_apps WHERE uid = ? AND app_id = ?",
          "billing-user",
          "paid-app",
        )?.count,
      ).toBe(0);
    } finally {
      state.database.close();
    }
  });

  it("rejects an invalid webhook signature before durable ingestion", async () => {
    const state = testEnvironment();
    const event = JSON.stringify({
      id: "evt_invalidSignature123",
      object: "event",
      type: "customer.subscription.updated",
      data: { object: { id: "sub_invalidSignature123" } },
    });
    try {
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/stripe/webhook", {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "stripe-signature": stripeSignature(
              event,
              "whsec_wrong_environment",
              Math.floor(Date.now() / 1_000),
            ),
          },
          body: event,
        }),
        state.env,
      );

      expect(response.status).toBe(400);
      expect(state.sent).toEqual([]);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_stripe_webhook_events",
        )?.count,
      ).toBe(0);
    } finally {
      state.database.close();
    }
  });

  it("rejects an oversized streamed webhook before signature work", async () => {
    const state = testEnvironment();
    try {
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/stripe/webhook", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: new ReadableStream({
            start(controller) {
              controller.enqueue(new Uint8Array(129 * 1_024));
              controller.close();
            },
          }),
          duplex: "half",
        } as RequestInit & { duplex: "half" }),
        state.env,
      );

      expect(response.status).toBe(400);
      expect(state.sent).toEqual([]);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_stripe_webhook_events",
        )?.count,
      ).toBe(0);
    } finally {
      state.database.close();
    }
  });

  it("fails closed when Checkout and Subscription metadata identify different users", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    seedPrice(state.database);
    state.database.database
      .prepare(
        `INSERT INTO cf_stripe_webhook_events
           (event_id, event_type, object_id, payload_sha256, status,
            next_attempt_at, created_at, updated_at)
         VALUES ('evt_crossAccount123', 'checkout.session.completed',
                 'cs_test_crossAccount123', ?, 'pending', 1, 1, 1)`,
      )
      .run("0".repeat(64));
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: RequestInfo | URL) => {
        const url = String(request instanceof Request ? request.url : request);
        if (url.endsWith("/v1/checkout/sessions/cs_test_crossAccount123")) {
          return Response.json({
            id: "cs_test_crossAccount123",
            mode: "subscription",
            client_reference_id: "billing-user",
            metadata: { uid: "billing-user" },
            customer: "cus_testCrossAccount123",
            subscription: "sub_testCrossAccount123",
          });
        }
        return Response.json({
          id: "sub_testCrossAccount123",
          status: "active",
          customer: "cus_testCrossAccount123",
          metadata: { uid: "different-user" },
          items: { data: [{ price: { id: "price_testOperator123" } }] },
          current_period_end: 1_802_592_000,
        });
      }),
    );
    const queued = queueMessage({
      jobId: "evt_crossAccount123",
      uid: "stripe-webhook",
      kind: "stripe_webhook",
      payload: { eventId: "evt_crossAccount123" },
    });
    try {
      await expect(
        processStripeWebhookMessage(queued.message, state.env),
      ).rejects.toThrow("stripe webhook projection unavailable");
      expect(queued.ack).not.toHaveBeenCalled();
      expect(
        state.database.row<{ status: string }>(
          "SELECT status FROM cf_stripe_webhook_events WHERE event_id = ?",
          "evt_crossAccount123",
        )?.status,
      ).toBe("failed");
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_user_subscriptions",
        )?.count,
      ).toBe(0);
    } finally {
      state.database.close();
    }
  });

  it("does not let a canceled old subscription clobber a newer paid subscription", async () => {
    const state = testEnvironment();
    seedCloudflareAccount(state.database);
    state.database.database
      .prepare(
        `INSERT INTO cf_user_subscriptions
           (uid, plan, status, current_period_end, stripe_subscription_id,
            current_price_id, updated_at)
         VALUES (?, 'operator', 'active', ?, 'sub_newActive123',
                 'price_newActive123', 1)`,
      )
      .run("billing-user", Math.floor(Date.now() / 1_000) + 86_400);
    state.database.database
      .prepare(
        `INSERT INTO cf_stripe_customers (uid, stripe_customer_id, updated_at)
         VALUES ('billing-user', 'cus_newCustomer123', 1)`,
      )
      .run();
    state.database.database
      .prepare(
        `INSERT INTO cf_stripe_webhook_events
           (event_id, event_type, object_id, payload_sha256, status,
            next_attempt_at, created_at, updated_at)
         VALUES ('evt_oldCanceled123', 'customer.subscription.deleted',
                 'sub_oldCanceled123', ?, 'pending', 1, 1, 1)`,
      )
      .run("0".repeat(64));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          id: "sub_oldCanceled123",
          status: "canceled",
          customer: "cus_oldCustomer123",
          metadata: { uid: "billing-user" },
          items: { data: [] },
          current_period_end: 1,
          cancel_at_period_end: false,
        }),
      ),
    );
    const queued = queueMessage({
      jobId: "evt_oldCanceled123",
      uid: "stripe-webhook",
      kind: "stripe_webhook",
      payload: { eventId: "evt_oldCanceled123" },
    });
    try {
      await processStripeWebhookMessage(queued.message, state.env);
      expect(
        state.database.row<{
          plan: string;
          stripe_subscription_id: string;
        }>(
          "SELECT plan, stripe_subscription_id FROM cf_user_subscriptions WHERE uid = ?",
          "billing-user",
        ),
      ).toEqual({
        plan: "operator",
        stripe_subscription_id: "sub_newActive123",
      });
      expect(
        state.database.row<{ stripe_customer_id: string }>(
          "SELECT stripe_customer_id FROM cf_stripe_customers WHERE uid = ?",
          "billing-user",
        )?.stripe_customer_id,
      ).toBe("cus_newCustomer123");
    } finally {
      state.database.close();
    }
  });
});
