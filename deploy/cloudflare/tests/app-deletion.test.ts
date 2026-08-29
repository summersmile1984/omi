import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import jobs from "../workers/jobs/index";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../workers/shared/auth-context";

type BoundStatement = {
  sql: string;
  args: unknown[];
  execute(): TestD1Result<unknown>;
};

type TestD1Result<T> = {
  success: true;
  results: T[];
  meta: { changes: number };
};

type TestD1PreparedStatement = BoundStatement & {
  bind(...values: unknown[]): TestD1PreparedStatement;
  first<T>(): Promise<T | null>;
  all<T>(): Promise<TestD1Result<T>>;
  run(): Promise<TestD1Result<unknown>>;
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

  prepare(sql: string): TestD1PreparedStatement {
    const build = (args: unknown[] = []): TestD1PreparedStatement => ({
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
      const results = statements.map((statement) => {
        const result = statement.execute();
        if (
          /^DELETE FROM cf_app_catalog\b/i.test(statement.sql.trimStart()) &&
          result.meta.changes === 1
        ) {
          // Remote D1 includes the app deletion fence's ON DELETE CASCADE in
          // this statement's metadata. Keep the harness faithful to that
          // successful multi-row result so it cannot be mistaken for failure.
          result.meta.changes = 2;
        }
        return result;
      });
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

function fakeQueue(options: { fail?: boolean } = {}) {
  const sent: Array<{ body: JobMessage; delaySeconds: number }> = [];
  return {
    sent,
    binding: {
      send: vi.fn(
        async (body: JobMessage, sendOptions?: { delaySeconds?: number }) => {
          if (options.fail) throw new Error("queue unavailable");
          sent.push({ body, delaySeconds: sendOptions?.delaySeconds || 0 });
        },
      ),
    } as unknown as Queue<JobMessage>,
  };
}

function fakeVectorize() {
  return {
    upsert: vi.fn(async () => undefined),
    deleteByIds: vi.fn(async () => undefined),
  };
}

function environment(options: { queueFail?: boolean } = {}) {
  const database = new SqliteD1();
  const queue = fakeQueue({ fail: options.queueFail });
  const assetDeletes = vi.fn(async () => undefined);
  return {
    database,
    queue,
    assetDeletes,
    env: {
      APP_DB: database as unknown as D1Database,
      JOBS: queue.binding,
      INTERNAL_ASSERTION_SECRET: "app-deletion-assertion-secret",
      STRIPE_SECRET_KEY: "sk_test_app_deletion",
      AUTH: { fetch: vi.fn() } as unknown as Fetcher,
      ASSETS: { delete: assetDeletes } as unknown as R2Bucket,
      AI: { run: vi.fn() },
      MEMORY_VECTORS: fakeVectorize(),
      ACTION_ITEM_VECTORS: fakeVectorize(),
      CONVERSATION_VECTORS: fakeVectorize(),
      TRANSCRIPT_CHUNK_VECTORS: fakeVectorize(),
      X_POST_VECTORS: fakeVectorize(),
      SYNC_FRESH: queue.binding,
      SYNC_BACKFILL: queue.binding,
    } satisfies JobsEnv,
  };
}

function seedApp(
  database: SqliteD1,
  options: { paid?: boolean; cloudflareLogo?: boolean } = {},
) {
  database.database
    .prepare(
      `INSERT INTO cf_app_catalog
         (id, approved, disabled, data_json, updated_at, owner_uid)
       VALUES ('owned-app', 1, 0, ?, 1, 'creator-user')`,
    )
    .run(
      JSON.stringify({
        id: "owned-app",
        name: "Owned app",
        is_paid: options.paid === true,
        image: options.cloudflareLogo
          ? "https://edge.test/v1/apps/owned-app/logo/00000000-0000-4000-8000-000000000000"
          : "https://storage.googleapis.com/legacy/logo.png",
      }),
    );
}

function seedPaymentLink(database: SqliteD1) {
  database.database
    .prepare(
      `INSERT INTO cf_app_payment_links
         (app_id, owner_uid, stripe_account_id, stripe_product_id,
          stripe_price_id, stripe_payment_link_id, payment_link_url,
          unit_amount, created_at, updated_at)
       VALUES ('owned-app', 'creator-user', 'acct_appDelete123',
               'prod_appDelete123', 'price_appDelete123',
               'plink_appDelete123',
               'https://buy.stripe.com/app-delete', 900, 1, 1)`,
    )
    .run();
}

function seedSubscriber(database: SqliteD1) {
  database.database
    .prepare(
      `INSERT INTO cf_app_subscriptions
         (uid, app_id, stripe_customer_id, stripe_subscription_id, status,
          current_period_start, current_period_end, cancel_at_period_end,
          price_id, created_at, updated_at)
       VALUES ('buyer-user', 'owned-app', 'cus_appDeleteBuyer123',
               'sub_appDeleteBuyer123', 'active', 1, 9999999999, 0,
               'price_appDelete123', 1, 1)`,
    )
    .run();
  database.database
    .prepare(
      `INSERT INTO cf_user_enabled_apps (uid, app_id, created_at)
       VALUES ('buyer-user', 'owned-app', 1)`,
    )
    .run();
}

async function deletionHeaders(uid: string, appId = "owned-app") {
  const path = `/v1/apps/${appId}`;
  const signed = await createSignedAuthContext(
    {
      uid,
      authority: "better-auth",
      requestId: "app-deletion-request",
    },
    "jobs",
    "DELETE",
    path,
    "app-deletion-assertion-secret",
  );
  if (!signed) throw new Error("signed app-deletion context unavailable");
  return {
    [AUTH_CONTEXT_HEADER]: signed.encoded,
    [AUTH_SIGNATURE_HEADER]: signed.signature,
  };
}

function queueMessage(body: JobMessage) {
  return {
    body,
    attempts: 1,
    ack: vi.fn(),
    retry: vi.fn(),
    id: "app-deletion-message",
    timestamp: new Date(0),
  } as unknown as Message<JobMessage>;
}

async function runQueuedDeletion(state: ReturnType<typeof environment>) {
  const dispatch = state.queue.sent.shift();
  if (!dispatch) throw new Error("missing app deletion dispatch");
  const message = queueMessage(dispatch.body);
  await jobs.queue(
    {
      queue: "omi-cf-jobs-staging",
      messages: [message],
      ackAll: vi.fn(),
      retryAll: vi.fn(),
    } as unknown as MessageBatch<JobMessage>,
    state.env,
  );
  return message;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Cloudflare app deletion", () => {
  it("removes the current R2 logo before completing a free app deletion", async () => {
    const state = environment();
    try {
      seedApp(state.database, { cloudflareLogo: true });
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps/owned-app", {
          method: "DELETE",
          headers: await deletionHeaders("creator-user"),
        }),
        state.env,
      );
      expect(response.status).toBe(200);
      const message = await runQueuedDeletion(state);
      expect(message.ack).toHaveBeenCalledOnce();
      expect(state.assetDeletes).toHaveBeenCalledWith(
        "cf-app-logos/creator-user/owned-app/00000000-0000-4000-8000-000000000000",
      );
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_app_catalog WHERE id = 'owned-app'",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ status: string }>(
          "SELECT status FROM cf_jobs WHERE kind = 'app_delete'",
        )?.status,
      ).toBe("completed");
    } finally {
      state.database.close();
    }
  });

  it("retires a paid app only after its Payment Link, Checkout, and subscriber renewal are stopped", async () => {
    const stripeRequests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = new Request(input, init);
        stripeRequests.push(request);
        if (request.url.includes("/v1/payment_links/")) {
          return Response.json({
            id: "plink_appDelete123",
            active: request.method === "GET",
            metadata: { app_id: "owned-app" },
            transfer_data: { destination: "acct_appDelete123" },
          });
        }
        if (request.url.includes("/v1/checkout/sessions?")) {
          return Response.json({
            object: "list",
            has_more: false,
            data: [
              {
                id: "cs_test_appDelete123",
                payment_link: "plink_appDelete123",
                status: "open",
              },
            ],
          });
        }
        if (request.url.endsWith("/cs_test_appDelete123/expire")) {
          return Response.json({
            id: "cs_test_appDelete123",
            status: "expired",
          });
        }
        if (request.url.includes("/v1/subscription_schedules?")) {
          return Response.json({ object: "list", has_more: false, data: [] });
        }
        if (request.url.endsWith("/v1/subscriptions/sub_appDeleteBuyer123")) {
          return Response.json({
            id: "sub_appDeleteBuyer123",
            status: "active",
            customer: "cus_appDeleteBuyer123",
            metadata: { app_id: "owned-app", uid: "buyer-user" },
            cancel_at_period_end: request.method === "POST",
          });
        }
        throw new Error(
          `unexpected Stripe request ${request.method} ${request.url}`,
        );
      }),
    );
    const state = environment();
    try {
      seedApp(state.database, { paid: true });
      seedPaymentLink(state.database);
      seedSubscriber(state.database);
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps/owned-app", {
          method: "DELETE",
          headers: await deletionHeaders("creator-user"),
        }),
        state.env,
      );
      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toEqual({ status: "ok" });
      expect(
        state.database.row<{ disabled: number }>(
          "SELECT disabled FROM cf_app_catalog WHERE id = 'owned-app'",
        )?.disabled,
      ).toBe(1);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_user_enabled_apps WHERE app_id = 'owned-app'",
        )?.count,
      ).toBe(0);
      expect(state.queue.sent).toHaveLength(1);

      const message = await runQueuedDeletion(state);
      expect(message.ack).toHaveBeenCalledOnce();
      expect(stripeRequests.map(({ method }) => method)).toEqual([
        "GET",
        "POST",
        "GET",
        "POST",
        "GET",
        "GET",
        "POST",
      ]);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_app_catalog WHERE id = 'owned-app'",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_user_enabled_apps WHERE app_id = 'owned-app'",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ stripe_payment_link_id: string }>(
          "SELECT stripe_payment_link_id FROM cf_retired_paid_apps WHERE app_id = 'owned-app'",
        )?.stripe_payment_link_id,
      ).toBe("plink_appDelete123");
      expect(
        state.database.row<{ status: string }>(
          "SELECT status FROM cf_jobs WHERE kind = 'app_delete'",
        )?.status,
      ).toBe("completed");
    } finally {
      state.database.close();
    }
  });

  it("keeps the hidden app and durable fence when Stripe ownership does not match", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          id: "plink_appDelete123",
          active: true,
          metadata: { app_id: "another-app" },
          transfer_data: { destination: "acct_appDelete123" },
        }),
      ),
    );
    const state = environment();
    try {
      seedApp(state.database, { paid: true });
      seedPaymentLink(state.database);
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps/owned-app", {
          method: "DELETE",
          headers: await deletionHeaders("creator-user"),
        }),
        state.env,
      );
      expect(response.status).toBe(200);
      await runQueuedDeletion(state);
      expect(
        state.database.row<{ disabled: number }>(
          "SELECT disabled FROM cf_app_catalog WHERE id = 'owned-app'",
        )?.disabled,
      ).toBe(1);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_app_deletion_fences WHERE app_id = 'owned-app'",
        )?.count,
      ).toBe(1);
      expect(
        state.database.row<{ status: string; last_error: string }>(
          "SELECT status, last_error FROM cf_jobs WHERE kind = 'app_delete'",
        ),
      ).toEqual({
        status: "failed",
        last_error: "app deletion dependency unavailable",
      });
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_retired_paid_apps WHERE app_id = 'owned-app'",
        )?.count,
      ).toBe(0);
      expect(() =>
        state.database.database
          .prepare(
            "UPDATE cf_app_catalog SET updated_at = 2 WHERE id = 'owned-app'",
          )
          .run(),
      ).toThrow("app deletion fence");
    } finally {
      state.database.close();
    }
  });

  it("fails closed before hiding a paid app whose provider mapping was not imported", async () => {
    const state = environment();
    try {
      seedApp(state.database, { paid: true });
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps/owned-app", {
          method: "DELETE",
          headers: await deletionHeaders("creator-user"),
        }),
        state.env,
      );
      expect(response.status).toBe(503);
      expect(
        state.database.row<{ disabled: number }>(
          "SELECT disabled FROM cf_app_catalog WHERE id = 'owned-app'",
        )?.disabled,
      ).toBe(0);
      expect(state.queue.sent).toHaveLength(0);
    } finally {
      state.database.close();
    }
  });

  it("fails closed before touching a paid mapping owned by another creator", async () => {
    const state = environment();
    try {
      seedApp(state.database, { paid: true });
      seedPaymentLink(state.database);
      state.database.database
        .prepare("UPDATE cf_app_catalog SET owner_uid = ? WHERE id = ?")
        .run("replacement-creator", "owned-app");
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps/owned-app", {
          method: "DELETE",
          headers: await deletionHeaders("replacement-creator"),
        }),
        state.env,
      );
      expect(response.status).toBe(503);
      expect(
        state.database.row<{ disabled: number }>(
          "SELECT disabled FROM cf_app_catalog WHERE id = 'owned-app'",
        )?.disabled,
      ).toBe(0);
      expect(state.queue.sent).toHaveLength(0);
    } finally {
      state.database.close();
    }
  });

  it("keeps a durable hidden deletion intent when the Queue is unavailable", async () => {
    const state = environment({ queueFail: true });
    try {
      seedApp(state.database);
      seedSubscriber(state.database);
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps/owned-app", {
          method: "DELETE",
          headers: await deletionHeaders("creator-user"),
        }),
        state.env,
      );
      expect(response.status).toBe(503);
      expect(
        state.database.row<{ disabled: number }>(
          "SELECT disabled FROM cf_app_catalog WHERE id = 'owned-app'",
        )?.disabled,
      ).toBe(1);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_user_enabled_apps WHERE app_id = 'owned-app'",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_app_deletion_fences WHERE app_id = 'owned-app'",
        )?.count,
      ).toBe(1);
      expect(
        state.database.row<{ status: string; last_error: string }>(
          "SELECT status, last_error FROM cf_jobs WHERE kind = 'app_delete'",
        ),
      ).toEqual({ status: "failed", last_error: "queue unavailable" });
    } finally {
      state.database.close();
    }
  });

  it("preserves the legacy not-found and ownership boundaries", async () => {
    const state = environment();
    try {
      seedApp(state.database);
      const unsigned = await jobs.fetch(
        new Request("https://jobs.test/v1/apps/owned-app", {
          method: "DELETE",
        }),
        state.env,
      );
      expect(unsigned.status).toBe(401);
      const forbidden = await jobs.fetch(
        new Request("https://jobs.test/v1/apps/owned-app", {
          method: "DELETE",
          headers: await deletionHeaders("another-user"),
        }),
        state.env,
      );
      expect(forbidden.status).toBe(403);
      const missing = await jobs.fetch(
        new Request("https://jobs.test/v1/apps/missing-app", {
          method: "DELETE",
          headers: await deletionHeaders("creator-user", "missing-app"),
        }),
        state.env,
      );
      expect(missing.status).toBe(404);
    } finally {
      state.database.close();
    }
  });
});
