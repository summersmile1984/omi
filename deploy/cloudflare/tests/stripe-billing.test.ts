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
          T | undefined;
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
        T | undefined) ?? null
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
) {
  database.database
    .prepare(
      `INSERT INTO cf_subscription_prices
         (id, plan_id, title, price_string, interval, unit_amount, active, updated_at)
       VALUES (?, ?, 'Monthly', '$20/month', 'month', 2000, 1, 1)`,
    )
    .run(priceId, plan);
}

async function billingHeaders(uid: string, path: string) {
  const signed = await createSignedAuthContext(
    { uid, authority: "better-auth", requestId: "stripe-billing-request" },
    "jobs",
    "POST",
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
