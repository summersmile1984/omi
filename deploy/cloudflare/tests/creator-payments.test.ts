import { createHmac } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import jobs from "../workers/jobs/index";
import type { JobsEnv } from "../workers/jobs/env";
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
      first: async <T>() =>
        (this.database.prepare(sql).get(...args.map(sqliteValue)) as
          T | undefined) ?? null,
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

function environment() {
  const database = new SqliteD1();
  const env = {
    APP_DB: database as unknown as D1Database,
    INTERNAL_ASSERTION_SECRET: "creator-payments-internal-secret",
    STRIPE_SECRET_KEY: "sk_test_creator_payments",
    STRIPE_CONNECT_WEBHOOK_SECRET: "whsec_creator_payments",
    STRIPE_CONNECT_REFRESH_SECRET: "creator-payments-refresh-secret-1234567890",
    PUBLIC_API_BASE_URL: "https://edge.example.test",
  } as JobsEnv;
  return { database, env };
}

function seedCloudflareAccount(database: SqliteD1, uid: string) {
  database.database
    .prepare(
      `INSERT INTO cf_account_cutover
         (uid, state, account_generation, ui_generation, api_generation,
          checkpoint_phase, manifest_id, destination_backend_bound, updated_at)
       VALUES (?, 'new', 1, 1, 1, 'completed', 'isolated-staging-v1', 1, 1)`,
    )
    .run(uid);
}

async function headers(uid: string, method: string, path: string) {
  const signed = await createSignedAuthContext(
    { uid, authority: "better-auth", requestId: "creator-payments-request" },
    "jobs",
    method,
    path,
    "creator-payments-internal-secret",
  );
  if (!signed) throw new Error("signed creator payment context unavailable");
  return {
    "content-type": "application/json",
    [AUTH_CONTEXT_HEADER]: signed.encoded,
    [AUTH_SIGNATURE_HEADER]: signed.signature,
  };
}

function stripeAccount(id: string, uid: string, complete = false) {
  return {
    id,
    object: "account",
    charges_enabled: complete,
    payouts_enabled: complete,
    details_submitted: complete,
    metadata: { uid },
  };
}

function stripeSignature(raw: string, secret: string, timestamp: number) {
  return `t=${timestamp},v1=${createHmac("sha256", secret)
    .update(`${timestamp}.${raw}`)
    .digest("hex")}`;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Cloudflare creator payments", () => {
  it("creates one owned Connect account and turns Stripe's GET refresh into a new onboarding redirect", async () => {
    const requests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = new Request(input, init);
        requests.push(request);
        if (request.url.endsWith("/v1/accounts")) {
          return Response.json(
            stripeAccount("acct_creatorOwned123", "creator-user"),
          );
        }
        return Response.json({
          object: "account_link",
          url: `https://connect.stripe.com/setup/c/acct_creatorOwned123/link-${requests.length}`,
        });
      }),
    );
    const state = environment();
    try {
      seedCloudflareAccount(state.database, "creator-user");
      const path = "/v1/stripe/connect-accounts";
      const first = await jobs.fetch(
        new Request(`https://jobs.test${path}?country=us`, {
          method: "POST",
          headers: await headers("creator-user", "POST", path),
        }),
        state.env,
      );
      expect(first.status).toBe(200);
      await expect(first.json()).resolves.toEqual({
        account_id: "acct_creatorOwned123",
        url: "https://connect.stripe.com/setup/c/acct_creatorOwned123/link-2",
      });

      const createRequest = requests[0];
      await expect(createRequest.text()).resolves.toContain("country=US");
      expect(createRequest.headers.get("idempotency-key")).toMatch(
        /^creator-connect-[a-f0-9]{64}$/,
      );
      const linkRequest = requests[1];
      const linkBody = new URLSearchParams(await linkRequest.text());
      expect(linkBody.get("account")).toBe("acct_creatorOwned123");
      expect(linkBody.get("type")).toBe("account_onboarding");
      expect(linkBody.get("return_url")).toBe(
        "https://edge.example.test/v1/stripe/return/acct_creatorOwned123",
      );
      const refreshUrl = linkBody.get("refresh_url");
      expect(refreshUrl).toMatch(
        /^https:\/\/edge\.example\.test\/v1\/stripe\/refresh\/acct_creatorOwned123\?token=/,
      );

      const browserRefresh = await jobs.fetch(
        new Request(refreshUrl || "https://invalid.test"),
        state.env,
      );
      expect(browserRefresh.status).toBe(303);
      expect(browserRefresh.headers.get("location")).toBe(
        "https://connect.stripe.com/setup/c/acct_creatorOwned123/link-3",
      );

      const duplicate = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "POST",
          headers: await headers("creator-user", "POST", path),
        }),
        state.env,
      );
      expect(duplicate.status).toBe(200);
      expect(
        requests.filter((request) => request.url.endsWith("/v1/accounts")),
      ).toHaveLength(1);
    } finally {
      state.database.close();
    }
  });

  it("rejects refreshing another user's connected account before calling Stripe", async () => {
    const stripeFetch = vi.fn();
    vi.stubGlobal("fetch", stripeFetch);
    const state = environment();
    try {
      state.database.database
        .prepare(
          `INSERT INTO cf_creator_payment_profiles
             (uid, stripe_account_id, updated_at) VALUES (?, ?, 1)`,
        )
        .run("owner-user", "acct_ownerOnly123");
      const path = "/v1/stripe/refresh/acct_ownerOnly123";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "POST",
          headers: await headers("attacker-user", "POST", path),
        }),
        state.env,
      );
      expect(response.status).toBe(403);
      expect(stripeFetch).not.toHaveBeenCalled();
    } finally {
      state.database.close();
    }
  });

  it("stores PayPal details and preserves explicit default-method selection", async () => {
    const state = environment();
    try {
      const savePath = "/v1/paypal/payment-details";
      const save = await jobs.fetch(
        new Request(`https://jobs.test${savePath}`, {
          method: "POST",
          headers: await headers("paypal-user", "POST", savePath),
          body: JSON.stringify({
            email: "CREATOR@EXAMPLE.COM",
            paypalme_url: "PayPal.Me/Creator",
          }),
        }),
        state.env,
      );
      expect(save.status).toBe(200);

      const defaultPath = "/v1/payment-methods/default";
      const setDefault = await jobs.fetch(
        new Request(`https://jobs.test${defaultPath}`, {
          method: "POST",
          headers: await headers("paypal-user", "POST", defaultPath),
          body: JSON.stringify({ method: "stripe" }),
        }),
        state.env,
      );
      expect(setDefault.status).toBe(200);

      const details = await jobs.fetch(
        new Request(`https://jobs.test${savePath}`, {
          headers: await headers("paypal-user", "GET", savePath),
        }),
        state.env,
      );
      await expect(details.json()).resolves.toEqual({
        email: "creator@example.com",
        paypalme_url: "paypal.me/creator",
      });

      const statusPath = "/v1/payment-methods/status";
      const status = await jobs.fetch(
        new Request(`https://jobs.test${statusPath}`, {
          headers: await headers("paypal-user", "GET", statusPath),
        }),
        state.env,
      );
      await expect(status.json()).resolves.toEqual({
        stripe: "not_connected",
        paypal: "connected",
        default: "stripe",
      });
    } finally {
      state.database.close();
    }
  });

  it("verifies a Connect webhook and projects onboarding state exactly once", async () => {
    const state = environment();
    try {
      seedCloudflareAccount(state.database, "webhook-creator");
      const now = Math.floor(Date.now() / 1_000);
      const raw = JSON.stringify({
        id: "evt_connectUpdated123",
        object: "event",
        type: "account.updated",
        data: {
          object: stripeAccount(
            "acct_webhookCreator123",
            "webhook-creator",
            true,
          ),
        },
      });
      const request = () =>
        new Request("https://jobs.test/v1/stripe/connect/webhook", {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "stripe-signature": stripeSignature(
              raw,
              "whsec_creator_payments",
              now,
            ),
          },
          body: raw,
        });
      const first = await jobs.fetch(request(), state.env);
      const duplicate = await jobs.fetch(request(), state.env);
      expect(first.status).toBe(200);
      expect(duplicate.status).toBe(200);
      expect(
        state.database.row<{
          stripe_onboarding_complete: number;
          default_payment_method: string;
          stripe_event_id: string;
        }>(
          `SELECT stripe_onboarding_complete, default_payment_method, stripe_event_id
             FROM cf_creator_payment_profiles WHERE uid = ?`,
          "webhook-creator",
        ),
      ).toEqual({
        stripe_onboarding_complete: 1,
        default_payment_method: "stripe",
        stripe_event_id: "evt_connectUpdated123",
      });
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_stripe_connect_events",
        )?.count,
      ).toBe(1);
    } finally {
      state.database.close();
    }
  });

  it("lists Stripe country specs while applying the existing GI exclusion and US floor", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          object: "list",
          data: [{ id: "CA" }, { id: "GI" }],
          has_more: false,
        }),
      ),
    );
    const state = environment();
    try {
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/stripe/supported-countries"),
        state.env,
      );
      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toEqual([
        { id: "CA", name: "Canada" },
        { id: "US", name: "United States" },
      ]);
    } finally {
      state.database.close();
    }
  });

  it("fails closed when the Connect webhook secret is absent", async () => {
    const state = environment();
    try {
      state.env.STRIPE_CONNECT_WEBHOOK_SECRET = undefined;
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/stripe/connect/webhook", {
          method: "POST",
          body: "{}",
        }),
        state.env,
      );
      expect(response.status).toBe(503);
      await expect(response.json()).resolves.toEqual({
        error: "stripe_connect_webhook_unavailable",
      });
    } finally {
      state.database.close();
    }
  });
});
