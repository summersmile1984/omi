import { createHmac } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import jobs from "../workers/jobs/index";
import {
  cleanupExpiredHumeWebhookEvents,
  parseHumeWebhookPredictions,
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

  it("normalizes a bounded COMPLETED result without guessing task identity", async () => {
    const state = testEnvironment();
    try {
      const body = JSON.stringify({
        job_id: "batch_job_789",
        status: "COMPLETED",
        predictions: [
          {
            results: {
              predictions: [
                {
                  models: {
                    prosody: {
                      grouped_predictions: [
                        {
                          predictions: [
                            {
                              time: { begin: 1.25, end: 2.5 },
                              emotions: [
                                { name: "Joy", score: 0.91 },
                                { name: "invalid", score: 2 },
                                { name: "missing" },
                              ],
                            },
                          ],
                        },
                      ],
                    },
                  },
                },
              ],
            },
          },
        ],
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
        state.database.row<{
          callback_status: string;
          mapping_status: string;
          processing_status: string;
          prediction_count: number;
          predictions_json: string;
          result_json: string;
        }>(
          "SELECT callback_status, mapping_status, processing_status, prediction_count, predictions_json, result_json " +
            "FROM cf_hume_webhook_results WHERE event_id = ?",
          "hume:batch_job_789",
        ),
      ).toMatchObject({
        callback_status: "COMPLETED",
        mapping_status: "unmapped",
        processing_status: "completed",
        prediction_count: 1,
        predictions_json: JSON.stringify([
          {
            start: 1.25,
            end: 2.5,
            emotions: [{ name: "Joy", score: 0.91 }],
          },
        ]),
      });
      expect(
        state.database.row<{ status: string; last_error: string | null }>(
          "SELECT status, last_error FROM cf_hume_webhook_events WHERE event_id = ?",
          "hume:batch_job_789",
        ),
      ).toEqual({
        status: "queued",
        last_error: null,
      });
      const result = state.database.row<{ result_json: string }>(
        "SELECT result_json FROM cf_hume_webhook_results WHERE event_id = ?",
        "hume:batch_job_789",
      );
      expect(JSON.parse(result?.result_json ?? "{}")).toMatchObject({
        schema_version: 1,
        provider: "hume",
        job_id: "batch_job_789",
        status: "COMPLETED",
        mapping_status: "unmapped",
        prediction_count: 1,
      });

      // The old callback would only reach Firestore after resolving a task;
      // an un-attested Hume job must not create a synthetic conversation row.
      expect(
        state.database.row("SELECT COUNT(*) AS count FROM cf_conversations"),
      ).toEqual({ count: 0 });
    } finally {
      state.database.close();
    }
  });

  it("settles FAILED callbacks and duplicate queue deliveries exactly once", async () => {
    const state = testEnvironment();
    try {
      const body = JSON.stringify({
        job_id: "batch_job_failed",
        status: "FAILED",
        predictions: [{ ignored: true }],
      });
      await jobs.fetch(signedRequest(body), state.env);
      const first = {
        body: state.sent[0],
        ack: vi.fn(),
        retry: vi.fn(),
      } as unknown as Message<JobMessage>;
      await processHumeWebhookMessage(first, state.env);
      await processHumeWebhookMessage(
        {
          body: state.sent[0],
          ack: vi.fn(),
          retry: vi.fn(),
        } as unknown as Message<JobMessage>,
        state.env,
      );
      expect(
        state.database.row<{
          callback_status: string;
          processing_status: string;
          prediction_count: number;
          result_json: string;
        }>(
          "SELECT callback_status, processing_status, prediction_count, result_json " +
            "FROM cf_hume_webhook_results WHERE event_id = ?",
          "hume:batch_job_failed",
        ),
      ).toMatchObject({
        callback_status: "FAILED",
        processing_status: "completed",
        prediction_count: 0,
      });
      expect(
        JSON.parse(
          state.database.row<{ result_json: string }>(
            "SELECT result_json FROM cf_hume_webhook_results WHERE event_id = ?",
            "hume:batch_job_failed",
          )?.result_json ?? "{}",
        ),
      ).toMatchObject({ status: "FAILED", predictions: [] });
      expect(first.retry).not.toHaveBeenCalled();
    } finally {
      state.database.close();
    }
  });

  it("drops malformed nested predictions while retaining a valid interval", () => {
    expect(
      parseHumeWebhookPredictions({
        predictions: [
          {
            results: {
              predictions: [
                {
                  models: {
                    prosody: {
                      grouped_predictions: [
                        {
                          predictions: [
                            { time: { begin: -1, end: 2 }, emotions: [] },
                            { time: { begin: 2, end: 1 }, emotions: [] },
                            { time: { begin: 2, end: 3 }, emotions: [] },
                          ],
                        },
                      ],
                    },
                  },
                },
              ],
            },
          },
        ],
      }),
    ).toEqual([{ start: 2, end: 3, emotions: [] }]);
  });

  it("settles a queued pre-result receipt explicitly and never invents an owner", async () => {
    const state = testEnvironment();
    try {
      state.database.database
        .prepare(
          "INSERT INTO cf_hume_webhook_events " +
            "(event_id, job_id, callback_status, payload_sha256, status, created_at, updated_at) " +
            "VALUES (?, ?, 'COMPLETED', ?, 'queued', ?, ?)",
        )
        .run(
          "hume:legacy_missing_result",
          "legacy_missing_result",
          "a".repeat(64),
          1,
          1,
        );
      const ack = vi.fn();
      await processHumeWebhookMessage(
        {
          body: {
            jobId: "hume:legacy_missing_result",
            uid: "hume-webhook",
            kind: "hume_webhook",
            payload: { event_id: "hume:legacy_missing_result" },
          },
          ack,
          retry: vi.fn(),
        } as unknown as Message<JobMessage>,
        state.env,
      );
      expect(ack).toHaveBeenCalledOnce();
      expect(
        state.database.row<{ status: string; last_error: string }>(
          "SELECT status, last_error FROM cf_hume_webhook_events WHERE event_id = ?",
          "hume:legacy_missing_result",
        ),
      ).toEqual({ status: "failed", last_error: "hume result unavailable" });
      expect(
        state.database.row("SELECT COUNT(*) AS count FROM cf_conversations"),
      ).toEqual({ count: 0 });
    } finally {
      state.database.close();
    }
  });

  it("cleans results with their event receipt and leaves no orphan retention rows", async () => {
    const state = testEnvironment();
    try {
      const body = JSON.stringify({
        job_id: "batch_job_retention",
        status: "FAILED",
      });
      await jobs.fetch(signedRequest(body), state.env);
      state.database.database
        .prepare(
          "UPDATE cf_hume_webhook_events SET updated_at = 1 WHERE event_id = ?",
        )
        .run("hume:batch_job_retention");
      state.database.database
        .prepare(
          "UPDATE cf_hume_webhook_results SET updated_at = 1 WHERE event_id = ?",
        )
        .run("hume:batch_job_retention");
      await cleanupExpiredHumeWebhookEvents(state.env, 30 * 24 * 60 * 60 + 2);
      expect(
        state.database.row(
          "SELECT COUNT(*) AS count FROM cf_hume_webhook_events",
        ),
      ).toEqual({ count: 0 });
      expect(
        state.database.row(
          "SELECT COUNT(*) AS count FROM cf_hume_webhook_results",
        ),
      ).toEqual({ count: 0 });
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
