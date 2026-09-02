import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobsEnv } from "../workers/jobs/env";
import { drainIntegrationWebhooks } from "../workers/jobs/integration-webhooks";

type BoundStatement = {
  sql: string;
  args: unknown[];
  execute(): { success: true; results: unknown[]; meta: { changes: number } };
};

function sqliteValue(value: unknown) {
  return value as never;
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
    this.database
      .prepare(
        "INSERT INTO cf_app_catalog " +
          "(id, approved, status, disabled, data_json, updated_at, owner_uid) " +
          "VALUES ('webhook-app', 1, 'approved', 0, ?, 1, 'webhook-owner')",
      )
      .run(
        JSON.stringify({
          id: "webhook-app",
          name: "Webhook App",
          capabilities: ["external_integration"],
        }),
      );
  }

  prepare(sql: string) {
    const build = (args: unknown[] = []) => ({
      sql,
      args,
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...args.map(sqliteValue)) as
          | T
          | undefined) ?? null,
      all: async <T>() => ({
        success: true as const,
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
            success: true as const,
            results: statement.all(...args.map(sqliteValue)),
            meta: { changes: 0 },
          };
        }
        const result = statement.run(...args.map(sqliteValue));
        return {
          success: true as const,
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

  seed(deliveryId: string, webhookUrl: string) {
    this.database
      .prepare(
        "INSERT INTO cf_integration_webhook_outbox " +
          "(delivery_id, app_id, uid, conversation_id, webhook_url, payload_json, status, attempts, " +
          "not_before, created_at, updated_at) " +
          "VALUES (?, 'webhook-app', 'integration-user', 'conversation-1', ?, ?, 'pending', 0, 0, 999, 999)",
      )
      .run(
        deliveryId,
        webhookUrl,
        JSON.stringify({ id: "conversation-1", source: "external_integration" }),
      );
  }

  close() {
    this.database.close();
  }
}

const databases: SqliteD1[] = [];

function environment() {
  const database = new SqliteD1();
  databases.push(database);
  return {
    database,
    env: { APP_DB: database as unknown as D1Database } as unknown as JobsEnv,
  };
}

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
});

describe("integration webhook outbox", () => {
  it("leases, posts idempotently, and stores a successful app message", async () => {
    const { database, env } = environment();
    database.seed("delivery-success", "https://hooks.example.test/conversation?token=opaque");
    const fetcher = vi.fn(async (url: URL | RequestInfo, init?: RequestInit) => {
      const parsed = new URL(url instanceof URL ? url : String(url));
      expect(parsed.searchParams.get("uid")).toBe("integration-user");
      expect(parsed.searchParams.get("token")).toBe("opaque");
      expect(init?.method).toBe("POST");
      expect(new Headers(init?.headers).get("x-omi-idempotency-key")).toBe(
        "delivery-success",
      );
      expect(JSON.parse(String(init?.body))).toMatchObject({
        id: "conversation-1",
      });
      return Response.json({ message: "Webhook processing completed" });
    });

    await drainIntegrationWebhooks(env, 1_000, fetcher as typeof fetch);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(
      database.database
        .prepare(
          "SELECT status, attempts, lease_until, last_error FROM cf_integration_webhook_outbox",
        )
        .get(),
    ).toEqual({ status: "sent", attempts: 1, lease_until: null, last_error: null });
    expect(
      database.database
        .prepare("SELECT message_count, app_id FROM cf_chat_sessions")
        .get(),
    ).toEqual({ message_count: 1, app_id: "webhook-app" });
    const message = JSON.parse(
      String(
        (
          database.database
            .prepare("SELECT message_json FROM cf_chat_messages")
            .get() as { message_json: string }
        ).message_json,
      ),
    );
    expect(message).toMatchObject({
      text: "Webhook processing completed",
      app_id: "webhook-app",
      memories_id: ["conversation-1"],
    });
  });

  it("retries transient responses without logging or storing response bodies", async () => {
    const { database, env } = environment();
    database.seed("delivery-retry", "https://hooks.example.test/conversation");
    await drainIntegrationWebhooks(
      env,
      1_000,
      vi.fn(async () => new Response("sensitive upstream body", { status: 503 })) as typeof fetch,
    );
    expect(
      database.database
        .prepare(
          "SELECT status, attempts, not_before, last_error FROM cf_integration_webhook_outbox",
        )
        .get(),
    ).toEqual({
      status: "pending",
      attempts: 1,
      not_before: 1_060,
      last_error: "HTTP 503",
    });
  });

  it("fails an imported private-network destination without fetching it", async () => {
    const { database, env } = environment();
    database.seed("delivery-unsafe", "https://127.0.0.1/conversation");
    const fetcher = vi.fn();
    await drainIntegrationWebhooks(env, 1_000, fetcher as typeof fetch);
    expect(fetcher).not.toHaveBeenCalled();
    expect(
      database.database
        .prepare("SELECT status, last_error FROM cf_integration_webhook_outbox")
        .get(),
    ).toEqual({ status: "failed", last_error: "unsafe webhook URL" });
  });

  it("terminally fails an expired lease after the retry budget", async () => {
    const { database, env } = environment();
    database.seed("delivery-exhausted", "https://hooks.example.test/conversation");
    database.database
      .prepare(
        "UPDATE cf_integration_webhook_outbox SET status = 'sending', attempts = 10, lease_until = 999",
      )
      .run();
    const fetcher = vi.fn();
    await drainIntegrationWebhooks(env, 1_000, fetcher as typeof fetch);
    expect(fetcher).not.toHaveBeenCalled();
    expect(
      database.database
        .prepare(
          "SELECT status, attempts, lease_until, last_error FROM cf_integration_webhook_outbox",
        )
        .get(),
    ).toEqual({
      status: "failed",
      attempts: 10,
      lease_until: null,
      last_error: "retry limit exceeded",
    });
  });

  it("opens a failure window on failure and resets it on success", async () => {
    const { database, env } = environment();
    database.seed("delivery-h1", "https://hooks.example.test/conversation");
    await drainIntegrationWebhooks(
      env,
      1_000,
      vi.fn(async () => new Response(null, { status: 503 })) as typeof fetch,
    );
    expect(
      database.database
        .prepare(
          "SELECT first_failure_at, failure_count, last_status, disabled FROM cf_app_webhook_health",
        )
        .get(),
    ).toEqual({
      first_failure_at: 1_000,
      failure_count: 1,
      last_status: 503,
      disabled: 0,
    });

    // A later success stamps the window; the next failure restarts it.
    database.database
      .prepare("UPDATE cf_integration_webhook_outbox SET status = 'pending', not_before = 0")
      .run();
    await drainIntegrationWebhooks(
      env,
      2_000,
      vi.fn(async () => Response.json({})) as typeof fetch,
    );
    database.database.prepare("DELETE FROM cf_integration_webhook_outbox").run();
    database.seed("delivery-h2", "https://hooks.example.test/conversation");
    await drainIntegrationWebhooks(
      env,
      3_000,
      vi.fn(async () => new Response(null, { status: 500 })) as typeof fetch,
    );
    expect(
      database.database
        .prepare(
          "SELECT first_failure_at, failure_count, notified_day1 FROM cf_app_webhook_health",
        )
        .get(),
    ).toEqual({ first_failure_at: 3_000, failure_count: 1, notified_day1: 0 });
  });

  it("warns the owner after a day and auto-disables delivery after three", async () => {
    const { database, env } = environment();
    database.seed("delivery-warn", "https://hooks.example.test/conversation");
    database.database
      .prepare(
        "INSERT INTO cf_app_webhook_health (app_id, endpoint, first_failure_at, last_failure_at, " +
          "failure_count, last_status, last_error, updated_at) " +
          "VALUES ('webhook-app', 'integration', 100, 100, 1, 503, 'HTTP 503', 100)",
      )
      .run();
    const day1Now = 100 + 86_400;
    await drainIntegrationWebhooks(
      env,
      day1Now,
      vi.fn(async () => new Response(null, { status: 503 })) as typeof fetch,
    );
    expect(
      database.database
        .prepare("SELECT notified_day1, disabled FROM cf_app_webhook_health")
        .get(),
    ).toEqual({ notified_day1: 1, disabled: 0 });
    const warn = database.database
      .prepare(
        "SELECT uid, source_id FROM cf_notification_outbox WHERE source_id LIKE 'webhook-health:%'",
      )
      .get() as { uid: string; source_id: string };
    expect(warn.uid).toBe("webhook-owner");
    expect(warn.source_id).toBe("webhook-health:webhook-app:100:day1");

    // Past three days the next failure disables the app...
    database.database
      .prepare("UPDATE cf_integration_webhook_outbox SET status = 'pending', not_before = 0, attempts = 0")
      .run();
    const disableNow = 100 + 259_200;
    await drainIntegrationWebhooks(
      env,
      disableNow,
      vi.fn(async () => new Response(null, { status: 503 })) as typeof fetch,
    );
    expect(
      database.database
        .prepare("SELECT disabled FROM cf_app_webhook_health")
        .get(),
    ).toEqual({ disabled: 1 });
    expect(
      database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_notification_outbox WHERE source_id = 'webhook-health:webhook-app:100:disable'",
        )
        .get(),
    ).toEqual({ count: 1 });

    // ...and later deliveries are dropped without calling out.
    database.database.prepare("DELETE FROM cf_integration_webhook_outbox").run();
    database.seed("delivery-after-disable", "https://hooks.example.test/conversation");
    const fetcher = vi.fn();
    await drainIntegrationWebhooks(env, disableNow + 10, fetcher as typeof fetch);
    expect(fetcher).not.toHaveBeenCalled();
    expect(
      database.database
        .prepare(
          "SELECT status, last_error FROM cf_integration_webhook_outbox WHERE delivery_id = 'delivery-after-disable'",
        )
        .get(),
    ).toEqual({ status: "failed", last_error: "webhook auto-disabled" });

    // An owner webhook-config change re-enables delivery.
    database.database
      .prepare("DELETE FROM cf_app_webhook_health WHERE app_id = 'webhook-app'")
      .run();
    database.database.prepare("DELETE FROM cf_integration_webhook_outbox").run();
    database.seed("delivery-reenabled", "https://hooks.example.test/conversation");
    const revived = vi.fn(async () => Response.json({}));
    await drainIntegrationWebhooks(env, disableNow + 20, revived as typeof fetch);
    expect(revived).toHaveBeenCalledTimes(1);
  });
});
