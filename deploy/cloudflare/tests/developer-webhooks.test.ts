import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { drainDeveloperWebhooks } from "../workers/jobs/developer-webhooks";
import type { JobsEnv } from "../workers/jobs/env";

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
  }

  prepare(sql: string) {
    const build = (args: unknown[] = []) => ({
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...args.map(sqliteValue)) as
          T | undefined) ?? null,
      all: async <T>() => ({
        success: true as const,
        results: this.database
          .prepare(sql)
          .all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0 },
      }),
      run: async () => {
        const result = this.database.prepare(sql).run(...args.map(sqliteValue));
        return {
          success: true as const,
          results: [],
          meta: { changes: Number(result.changes) },
        };
      },
    });
    return build();
  }

  seed(deliveryId: string, webhookUrl: string) {
    this.database
      .prepare(
        "INSERT INTO cf_developer_webhook_outbox " +
          "(delivery_id, uid, webhook_type, conversation_id, webhook_url, payload_json, status, attempts, " +
          "not_before, created_at, updated_at) " +
          "VALUES (?, 'developer-user', 'memory_created', 'conversation-1', ?, ?, 'pending', 0, 0, 999, 999)",
      )
      .run(
        deliveryId,
        webhookUrl,
        JSON.stringify({ id: "conversation-1", status: "completed" }),
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

describe("developer webhook outbox", () => {
  it("leases and posts a memory_created payload with stable identity", async () => {
    const { database, env } = environment();
    database.seed(
      "delivery-success",
      "https://developer.example.test/conversation?token=opaque",
    );
    const fetcher = vi.fn(
      async (url: URL | RequestInfo, init?: RequestInit) => {
        const parsed = new URL(url instanceof URL ? url : String(url));
        expect(parsed.searchParams.get("uid")).toBe("developer-user");
        expect(parsed.searchParams.get("token")).toBe("opaque");
        const headers = new Headers(init?.headers);
        expect(headers.get("x-omi-idempotency-key")).toBe("delivery-success");
        expect(headers.get("x-omi-webhook-type")).toBe("memory_created");
        expect(JSON.parse(String(init?.body))).toMatchObject({
          id: "conversation-1",
        });
        return new Response(null, { status: 204 });
      },
    );

    await drainDeveloperWebhooks(env, 1_000, fetcher as typeof fetch);

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(
      database.database
        .prepare(
          "SELECT status, attempts, lease_until, last_error FROM cf_developer_webhook_outbox",
        )
        .get(),
    ).toEqual({
      status: "sent",
      attempts: 1,
      lease_until: null,
      last_error: null,
    });
  });

  it("retries transient responses and permanently rejects private destinations", async () => {
    const retry = environment();
    retry.database.seed(
      "delivery-retry",
      "https://developer.example.test/conversation",
    );
    await drainDeveloperWebhooks(
      retry.env,
      1_000,
      vi.fn(
        async () => new Response("sensitive", { status: 503 }),
      ) as typeof fetch,
    );
    expect(
      retry.database.database
        .prepare(
          "SELECT status, attempts, not_before, last_error FROM cf_developer_webhook_outbox",
        )
        .get(),
    ).toEqual({
      status: "pending",
      attempts: 1,
      not_before: 1_060,
      last_error: "HTTP 503",
    });

    const unsafe = environment();
    unsafe.database.seed("delivery-unsafe", "https://127.0.0.1/conversation");
    const fetcher = vi.fn();
    await drainDeveloperWebhooks(unsafe.env, 1_000, fetcher as typeof fetch);
    expect(fetcher).not.toHaveBeenCalled();
    expect(
      unsafe.database.database
        .prepare("SELECT status, last_error FROM cf_developer_webhook_outbox")
        .get(),
    ).toEqual({ status: "failed", last_error: "unsafe webhook URL" });
  });
});
