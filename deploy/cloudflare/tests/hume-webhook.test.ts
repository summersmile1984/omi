import { createHmac } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import jobs from "../workers/jobs/index";
import {
  processHumeWebhookMessage,
  verifyHumeWebhookSignature,
} from "../workers/jobs/hume-webhook";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";

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
    HUME_WEBHOOK_SIGNING_KEY: "hume-test-secret",
  } as JobsEnv;
  return { database, env, sent };
}

function signedRequest(
  body: string,
  timestamp = Math.floor(Date.now() / 1_000).toString(),
) {
  const signature = createHmac("sha256", "hume-test-secret")
    .update(`${body}.${timestamp}`)
    .digest("hex");
  return new Request("https://jobs.test/v1/agents/hume/callback", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-hume-ai-webhook-signature": signature,
      "x-hume-ai-webhook-timestamp": timestamp,
    },
    body,
  });
}

describe("Hume webhook boundary", () => {
  it("verifies the provider contract, receipts once, and admits one queue job", async () => {
    const state = testEnvironment();
    try {
      const body = JSON.stringify({
        job_id: "batch_job_123",
        status: "COMPLETED",
        predictions: [],
      });
      const response = await jobs.fetch(signedRequest(body), state.env);
      expect(response.status).toBe(202);
      expect(await response.json()).toEqual({
        status: "accepted",
        job_id: "batch_job_123",
      });
      expect(state.sent).toHaveLength(1);
      expect(state.sent[0]).toMatchObject({
        jobId: "hume:batch_job_123",
        uid: "hume-webhook",
        kind: "hume_webhook",
        payload: { event_id: "hume:batch_job_123" },
      });
      expect(
        state.database.row<{ status: string; attempts: number }>(
          "SELECT status, attempts FROM cf_hume_webhook_events WHERE event_id = ?",
          "hume:batch_job_123",
        ),
      ).toEqual({ status: "queued", attempts: 0 });

      const duplicate = await jobs.fetch(signedRequest(body), state.env);
      expect(duplicate.status).toBe(202);
      expect(state.sent).toHaveLength(1);

      const changed = JSON.stringify({
        job_id: "batch_job_123",
        status: "FAILED",
        predictions: [],
      });
      const mismatch = await jobs.fetch(signedRequest(changed), state.env);
      expect(mismatch.status).toBe(409);
      expect(state.sent).toHaveLength(1);
    } finally {
      state.database.close();
    }
  });

  it("rejects stale or invalid signatures before creating a receipt", async () => {
    const state = testEnvironment();
    try {
      const body = JSON.stringify({
        job_id: "batch_job_456",
        status: "FAILED",
      });
      const stale = await jobs.fetch(
        signedRequest(body, (Math.floor(Date.now() / 1_000) - 301).toString()),
        state.env,
      );
      expect(stale.status).toBe(400);
      const invalid = await jobs.fetch(
        new Request("https://jobs.test/v1/agents/hume/callback", {
          method: "POST",
          headers: {
            "x-hume-ai-webhook-signature": "0".repeat(64),
            "x-hume-ai-webhook-timestamp": Math.floor(
              Date.now() / 1_000,
            ).toString(),
          },
          body,
        }),
        state.env,
      );
      expect(invalid.status).toBe(400);
      expect(
        state.database.row(
          "SELECT COUNT(*) AS count FROM cf_hume_webhook_events",
        ),
      ).toEqual({ count: 0 });
    } finally {
      state.database.close();
    }
  });

  it("fails closed when the signing key is absent or body is too large", async () => {
    const state = testEnvironment();
    try {
      const missingSecretEnv = {
        ...state.env,
        HUME_WEBHOOK_SIGNING_KEY: undefined,
      } as JobsEnv;
      const unavailable = await jobs.fetch(
        new Request("https://jobs.test/v1/agents/hume/callback", {
          method: "POST",
          body: "{}",
        }),
        missingSecretEnv,
      );
      expect(unavailable.status).toBe(503);
      const body = "x".repeat(2 * 1024 * 1024 + 1);
      const oversized = await jobs.fetch(signedRequest(body), state.env);
      expect(oversized.status).toBe(413);
      expect(state.sent).toHaveLength(0);
    } finally {
      state.database.close();
    }
  });

  it("does not claim Hume processing parity in the queue consumer", async () => {
    const state = testEnvironment();
    try {
      const body = JSON.stringify({
        job_id: "batch_job_789",
        status: "COMPLETED",
      });
      await jobs.fetch(signedRequest(body), state.env);
      const ack = vi.fn();
      await processHumeWebhookMessage(
        {
          body: state.sent[0],
          ack,
          retry: vi.fn(),
        } as unknown as Message<JobMessage>,
        state.env,
      );
      expect(ack).toHaveBeenCalledOnce();
      expect(
        state.database.row<{ status: string; last_error: string }>(
          "SELECT status, last_error FROM cf_hume_webhook_events WHERE event_id = ?",
          "hume:batch_job_789",
        ),
      ).toEqual({
        status: "failed",
        last_error: "hume processing unavailable",
      });
    } finally {
      state.database.close();
    }
  });

  it("uses payload.timestamp and constant-time comparison", async () => {
    const raw = new TextEncoder().encode('{"job_id":"batch_job_sig"}');
    const timestamp = Math.floor(Date.now() / 1_000);
    const signature = createHmac("sha256", "hume-test-secret")
      .update(`{\"job_id\":\"batch_job_sig\"}.${timestamp}`)
      .digest("hex");
    await expect(
      verifyHumeWebhookSignature(
        raw,
        signature,
        timestamp.toString(),
        "hume-test-secret",
        timestamp,
      ),
    ).resolves.toBe(true);
    await expect(
      verifyHumeWebhookSignature(
        raw,
        signature,
        timestamp.toString(),
        "wrong-secret",
        timestamp,
      ),
    ).resolves.toBe(false);
  });
});
