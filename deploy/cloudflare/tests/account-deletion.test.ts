import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanupExpiredAccountDeletionTombstones,
  processAccountDeletionMessage,
  reconcileAccountDeletions,
} from "../workers/jobs/account-deletion";
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

function fakeBucket(initial: Record<string, Uint8Array> = {}) {
  const objects = new Map(Object.entries(initial));
  return {
    objects,
    binding: {
      list: vi.fn(
        async ({ prefix, limit }: { prefix?: string; limit?: number }) => {
          const keys = [...objects.keys()]
            .filter((key) => key.startsWith(prefix || ""))
            .slice(0, limit || 1_000);
          return {
            objects: keys.map((key) => ({
              key,
              version: "1",
              size: objects.get(key)?.byteLength || 0,
              etag: "etag",
              uploaded: new Date(0),
              httpEtag: '"etag"',
              checksums: {},
              storageClass: "Standard",
            })),
            truncated: false,
            delimitedPrefixes: [],
          };
        },
      ),
      delete: vi.fn(async (keys: string | string[]) => {
        for (const key of Array.isArray(keys) ? keys : [keys])
          objects.delete(key);
      }),
    } as unknown as R2Bucket,
  };
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

function fakeAuth(options: { failDelete?: boolean } = {}) {
  const requests: Array<{ method: string; path: string }> = [];
  return {
    requests,
    binding: {
      fetch: vi.fn(async (request: Request) => {
        const path = new URL(request.url).pathname;
        requests.push({ method: request.method, path });
        if (path === "/ready") return Response.json({ status: "ready" });
        if (request.method === "DELETE") {
          return options.failDelete
            ? Response.json({ error: "unavailable" }, { status: 503 })
            : Response.json({ status: "deleted", residual: {} });
        }
        return Response.json({ empty: true, residual: {} });
      }),
    } as unknown as Fetcher,
  };
}

function seedCloudflareAccount(database: SqliteD1, uid = "deletion-user") {
  database.database
    .prepare(
      `INSERT INTO cf_account_cutover
         (uid, state, account_generation, ui_generation, api_generation,
          checkpoint_phase, manifest_id, destination_backend_bound, updated_at)
       VALUES (?, 'new', 1, 1, 1, 'completed', 'isolated-staging-v1', 1, ?)`,
    )
    .run(uid, 1);
  database.database
    .prepare("INSERT INTO cf_worker_probe (uid, last_seen_at) VALUES (?, ?)")
    .run(uid, 1);
  database.database
    .prepare(
      "INSERT INTO cf_conversations (uid, id, created_at) VALUES (?, ?, ?)",
    )
    .run(uid, "deletion-conversation", 1);
  database.database
    .prepare(
      `INSERT INTO cf_task_shares
         (token, sender_uid, sender_name, expires_at, created_at)
       VALUES (?, ?, 'Deletion User', ?, ?)`,
    )
    .run("deletion-owned-share", uid, 10_000, 1);
  database.database
    .prepare(
      `INSERT INTO cf_task_share_items
         (token, ordinal, action_item_id) VALUES (?, 0, ?)`,
    )
    .run("deletion-owned-share", "deletion-action-item");
  database.database
    .prepare(
      `INSERT INTO cf_task_shares
         (token, sender_uid, sender_name, expires_at, created_at)
       VALUES (?, 'other-user', 'Other User', ?, ?)`,
    )
    .run("other-owned-share", 10_000, 1);
  database.database
    .prepare(
      `INSERT INTO cf_task_share_acceptances
         (token, recipient_uid, acceptance_nonce, accepted_at)
       VALUES (?, ?, ?, ?)`,
    )
    .run("other-owned-share", uid, "deletion-acceptance", 1);
}

function environment(
  options: {
    queueFail?: boolean;
    authFailDelete?: boolean;
    r2?: Record<string, Uint8Array>;
    stripeSecretKey?: string;
  } = {},
) {
  const database = new SqliteD1();
  const bucket = fakeBucket(options.r2);
  const queue = fakeQueue({ fail: options.queueFail });
  const auth = fakeAuth({ failDelete: options.authFailDelete });
  const env = {
    APP_DB: database as unknown as D1Database,
    ASSETS: bucket.binding,
    JOBS: queue.binding,
    AUTH: auth.binding,
    INTERNAL_ASSERTION_SECRET: "account-deletion-test-secret",
    STRIPE_SECRET_KEY: options.stripeSecretKey,
  } as JobsEnv;
  return { database, bucket, queue, auth, env };
}

async function deletionHeaders(uid: string, path: string) {
  const signed = await createSignedAuthContext(
    { uid, authority: "better-auth", requestId: "account-deletion-request" },
    "jobs",
    "DELETE",
    path,
    "account-deletion-test-secret",
  );
  if (!signed) throw new Error("account deletion test assertion unavailable");
  return {
    "content-type": "application/json",
    [AUTH_CONTEXT_HEADER]: signed.encoded,
    [AUTH_SIGNATURE_HEADER]: signed.signature,
  };
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

describe("Cloudflare account deletion workflow", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-29T00:00:00.000Z"));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("persists the intent before dispatch and fences every later D1 mutation", async () => {
    const state = environment();
    try {
      seedCloudflareAccount(state.database);
      const path = "/v1/users/delete-account";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "DELETE",
          headers: await deletionHeaders("deletion-user", path),
          body: JSON.stringify({ reason: "privacy_concerns" }),
        }),
        state.env,
      );
      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toEqual({
        status: "ok",
        message: "Account deletion started",
      });
      const intent = state.database.row<{
        job_id: string;
        status: string;
        phase: string;
      }>(
        "SELECT job_id, status, phase FROM cf_account_deletion_intents WHERE uid = ?",
        "deletion-user",
      );
      expect(intent).toMatchObject({ status: "pending", phase: "quiescing" });
      expect(state.queue.sent).toEqual([
        {
          body: {
            jobId: intent?.job_id,
            uid: "",
            kind: "account_delete",
            payload: {},
          },
          delaySeconds: 60,
        },
      ]);
      state.database.database
        .prepare("DELETE FROM cf_account_cutover WHERE uid = ?")
        .run("deletion-user");
      const repeated = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "DELETE",
          headers: await deletionHeaders("deletion-user", path),
        }),
        state.env,
      );
      expect(repeated.status).toBe(200);
      expect(state.queue.sent).toHaveLength(1);
      expect(() =>
        state.database.database
          .prepare(
            "INSERT INTO cf_worker_probe (uid, last_seen_at) VALUES (?, ?)",
          )
          .run("deletion-user", 2),
      ).toThrow(/account deletion fence/);
      expect(() =>
        state.database.database
          .prepare("UPDATE cf_worker_probe SET last_seen_at = ? WHERE uid = ?")
          .run(2, "deletion-user"),
      ).toThrow(/account deletion fence/);
      expect(() =>
        state.database.database
          .prepare("UPDATE cf_worker_probe SET uid = ? WHERE uid = ?")
          .run("other-user", "deletion-user"),
      ).toThrow(/account deletion fence/);
      expect(() =>
        state.database.database
          .prepare("UPDATE cf_task_share_items SET ordinal = 1 WHERE token = ?")
          .run("deletion-owned-share"),
      ).toThrow(/account deletion fence/);
    } finally {
      state.database.close();
    }
  });

  it("purges R2 and D1, performs two zero scans, then deletes Auth idempotently", async () => {
    const state = environment({
      r2: {
        "cf-assets/deletion-user/content": new Uint8Array([1, 2, 3]),
      },
    });
    try {
      seedCloudflareAccount(state.database);
      const path = "/v1/users/delete-account";
      await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "DELETE",
          headers: await deletionHeaders("deletion-user", path),
        }),
        state.env,
      );
      let processed = 0;
      while (state.auth.requests.length === 0 && processed < 10) {
        const dispatch = state.queue.sent.shift();
        if (!dispatch) throw new Error("missing account deletion dispatch");
        vi.advanceTimersByTime(dispatch.delaySeconds * 1_000);
        const queued = queueMessage(dispatch.body);
        await processAccountDeletionMessage(queued.message, state.env);
        expect(queued.ack).toHaveBeenCalledOnce();
        processed += 1;
      }
      expect(processed).toBeLessThan(10);
      expect(state.bucket.objects.size).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_worker_probe WHERE uid = ?",
          "deletion-user",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_conversations_fts WHERE uid = ?",
          "deletion-user",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_task_share_items WHERE token = ?",
          "deletion-owned-share",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_task_shares WHERE token = ?",
          "other-owned-share",
        )?.count,
      ).toBe(1);
      expect(state.auth.requests.map(({ method }) => method)).toEqual([
        "DELETE",
        "GET",
      ]);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_account_deletion_intents",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ expires_at: number; completed_at: number }>(
          "SELECT expires_at, completed_at FROM cf_account_deletion_tombstones WHERE uid = ?",
          "deletion-user",
        ),
      ).toMatchObject({
        expires_at: Math.floor(Date.now() / 1_000) + 25 * 60 * 60,
        completed_at: Math.floor(Date.now() / 1_000),
      });
      expect(() =>
        state.database.database
          .prepare(
            "INSERT INTO cf_worker_probe (uid, last_seen_at) VALUES (?, ?)",
          )
          .run("deletion-user", 3),
      ).toThrow(/account deletion fence/);

      vi.advanceTimersByTime(25 * 60 * 60 * 1_000);
      await cleanupExpiredAccountDeletionTombstones(
        state.env,
        Math.floor(Date.now() / 1_000),
      );
      expect(() =>
        state.database.database
          .prepare(
            "INSERT INTO cf_worker_probe (uid, last_seen_at) VALUES (?, ?)",
          )
          .run("deletion-user", 4),
      ).not.toThrow();
    } finally {
      state.database.close();
    }
  });

  it("keeps the identity fence recoverable when Auth deletion is unavailable", async () => {
    const state = environment({ authFailDelete: true });
    try {
      seedCloudflareAccount(state.database);
      const path = "/v1/users/delete-account";
      await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "DELETE",
          headers: await deletionHeaders("deletion-user", path),
        }),
        state.env,
      );

      let processed = 0;
      while (state.auth.requests.length === 0 && processed < 10) {
        const dispatch = state.queue.sent.shift();
        if (!dispatch) throw new Error("missing account deletion dispatch");
        vi.advanceTimersByTime(dispatch.delaySeconds * 1_000);
        await processAccountDeletionMessage(
          queueMessage(dispatch.body).message,
          state.env,
        );
        processed += 1;
      }
      expect(state.auth.requests).toEqual([
        { method: "DELETE", path: "/internal/users/deletion-user" },
      ]);
      expect(
        state.database.row<{ status: string; phase: string }>(
          "SELECT status, phase FROM cf_account_deletion_intents WHERE uid = ?",
          "deletion-user",
        ),
      ).toEqual({ status: "failed", phase: "identity" });
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_account_deletion_tombstones",
        )?.count,
      ).toBe(0);

      const recoveredAuth = fakeAuth();
      state.env.AUTH = recoveredAuth.binding;
      const retry = state.queue.sent.shift();
      if (!retry) throw new Error("missing Auth deletion retry");
      vi.advanceTimersByTime(retry.delaySeconds * 1_000);
      await processAccountDeletionMessage(
        queueMessage(retry.body).message,
        state.env,
      );
      expect(recoveredAuth.requests.map(({ method }) => method)).toEqual([
        "DELETE",
        "GET",
      ]);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_account_deletion_intents",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_account_deletion_tombstones",
        )?.count,
      ).toBe(1);
    } finally {
      state.database.close();
    }
  });

  it("keeps the durable intent recoverable when Queue dispatch is unavailable", async () => {
    const state = environment({ queueFail: true });
    try {
      seedCloudflareAccount(state.database);
      const path = "/v1/users/delete-account";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "DELETE",
          headers: await deletionHeaders("deletion-user", path),
        }),
        state.env,
      );
      expect(response.status).toBe(200);
      expect(
        state.database.row<{ status: string }>(
          "SELECT status FROM cf_account_deletion_intents WHERE uid = ?",
          "deletion-user",
        )?.status,
      ).toBe("pending");

      vi.advanceTimersByTime(60_000);
      const recoveryQueue = fakeQueue();
      state.env.JOBS = recoveryQueue.binding;
      await expect(
        reconcileAccountDeletions(state.env, Math.floor(Date.now() / 1_000)),
      ).resolves.toBe(1);
      expect(recoveryQueue.sent[0]?.body).toMatchObject({
        uid: "",
        kind: "account_delete",
      });
    } finally {
      state.database.close();
    }
  });

  it("fails closed before fencing external billing without its credential", async () => {
    const state = environment();
    try {
      seedCloudflareAccount(state.database);
      state.database.database
        .prepare(
          `INSERT INTO cf_user_subscriptions
             (uid, plan, status, stripe_subscription_id, updated_at)
           VALUES (?, 'plus', 'active', ?, ?)`,
        )
        .run("deletion-user", "sub_liveSubscription123", 1);
      const path = "/v1/users/delete-account";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "DELETE",
          headers: await deletionHeaders("deletion-user", path),
        }),
        state.env,
      );
      expect(response.status).toBe(503);
      await expect(response.json()).resolves.toEqual({
        error: "external_provider_cleanup_required",
      });
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_account_deletion_intents",
        )?.count,
      ).toBe(0);
    } finally {
      state.database.close();
    }
  });

  it("cancels Stripe at period end after the durable fence and before product purge", async () => {
    const stripeRequests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = new Request(input, init);
        stripeRequests.push(request);
        if (request.method === "GET") {
          return Response.json({
            id: "sub_liveSubscription123",
            status: "active",
            cancel_at_period_end: false,
          });
        }
        return Response.json({
          id: "sub_liveSubscription123",
          status: "active",
          cancel_at_period_end: true,
        });
      }),
    );
    const state = environment({ stripeSecretKey: "sk_test_account_deletion" });
    try {
      seedCloudflareAccount(state.database);
      state.database.database
        .prepare(
          `INSERT INTO cf_user_subscriptions
             (uid, plan, status, stripe_subscription_id, updated_at)
           VALUES (?, 'plus', 'active', ?, ?)`,
        )
        .run("deletion-user", "sub_liveSubscription123", 1);
      const path = "/v1/users/delete-account";
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "DELETE",
          headers: await deletionHeaders("deletion-user", path),
        }),
        state.env,
      );
      expect(response.status).toBe(200);
      expect(stripeRequests).toHaveLength(0);

      const dispatch = state.queue.sent.shift();
      if (!dispatch) throw new Error("missing account deletion dispatch");
      vi.advanceTimersByTime(dispatch.delaySeconds * 1_000);
      await processAccountDeletionMessage(
        queueMessage(dispatch.body).message,
        state.env,
      );

      expect(stripeRequests.map(({ method }) => method)).toEqual([
        "GET",
        "POST",
      ]);
      expect(stripeRequests[0]?.url).toBe(
        "https://api.stripe.com/v1/subscriptions/sub_liveSubscription123",
      );
      expect(stripeRequests[1]?.headers.get("authorization")).toBe(
        `Basic ${btoa("sk_test_account_deletion:")}`,
      );
      expect(stripeRequests[1]?.headers.get("idempotency-key")).toMatch(
        /^account-delete-[0-9a-f-]{36}$/,
      );
      await expect(stripeRequests[1]?.text()).resolves.toBe(
        "cancel_at_period_end=true",
      );
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_user_subscriptions WHERE uid = ?",
          "deletion-user",
        )?.count,
      ).toBe(0);
    } finally {
      state.database.close();
    }
  });

  it("retries provider cleanup without purging product data when Stripe fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ error: {} }, { status: 503 })),
    );
    const state = environment({ stripeSecretKey: "sk_test_account_deletion" });
    try {
      seedCloudflareAccount(state.database);
      state.database.database
        .prepare(
          `INSERT INTO cf_user_subscriptions
             (uid, plan, status, stripe_subscription_id, updated_at)
           VALUES (?, 'plus', 'active', ?, ?)`,
        )
        .run("deletion-user", "sub_liveSubscription123", 1);
      const path = "/v1/users/delete-account";
      await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "DELETE",
          headers: await deletionHeaders("deletion-user", path),
        }),
        state.env,
      );
      const dispatch = state.queue.sent.shift();
      if (!dispatch) throw new Error("missing account deletion dispatch");
      vi.advanceTimersByTime(dispatch.delaySeconds * 1_000);
      await processAccountDeletionMessage(
        queueMessage(dispatch.body).message,
        state.env,
      );

      expect(
        state.database.row<{
          status: string;
          phase: string;
          last_error: string;
        }>(
          `SELECT status, phase, last_error
           FROM cf_account_deletion_intents WHERE uid = ?`,
          "deletion-user",
        ),
      ).toEqual({
        status: "failed",
        phase: "quiescing",
        last_error: "account deletion dependency unavailable",
      });
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_worker_probe WHERE uid = ?",
          "deletion-user",
        )?.count,
      ).toBe(1);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_user_subscriptions WHERE uid = ?",
          "deletion-user",
        )?.count,
      ).toBe(1);
    } finally {
      state.database.close();
    }
  });

  it("accepts an already terminal Stripe subscription without mutating it", async () => {
    const stripeFetch = vi.fn(async () =>
      Response.json({
        id: "sub_liveSubscription123",
        status: "canceled",
        cancel_at_period_end: false,
      }),
    );
    vi.stubGlobal("fetch", stripeFetch);
    const state = environment({ stripeSecretKey: "sk_test_account_deletion" });
    try {
      seedCloudflareAccount(state.database);
      state.database.database
        .prepare(
          `INSERT INTO cf_user_subscriptions
             (uid, plan, status, stripe_subscription_id, updated_at)
           VALUES (?, 'plus', 'active', ?, ?)`,
        )
        .run("deletion-user", "sub_liveSubscription123", 1);
      const path = "/v1/users/delete-account";
      await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "DELETE",
          headers: await deletionHeaders("deletion-user", path),
        }),
        state.env,
      );
      const dispatch = state.queue.sent.shift();
      if (!dispatch) throw new Error("missing account deletion dispatch");
      vi.advanceTimersByTime(dispatch.delaySeconds * 1_000);
      await processAccountDeletionMessage(
        queueMessage(dispatch.body).message,
        state.env,
      );
      expect(stripeFetch).toHaveBeenCalledOnce();
    } finally {
      state.database.close();
    }
  });
});
