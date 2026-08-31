import { describe, expect, it, vi } from "vitest";
import { createHash, createHmac } from "node:crypto";
import jobs from "../workers/jobs/index";
import { captureDlqMessage } from "../workers/jobs/dlq-replay";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";

const ADMIN_KEY = "dlq-admin-test-key";
const SIGNING_SECRET = "dlq-replay-signing-secret-with-at-least-32-bytes";
const DLQ = "omi-cf-jobs-dlq-staging";

type Stored = {
  replay?: Record<string, unknown>;
  message?: Record<string, unknown>;
  replayItems: Array<Record<string, unknown>>;
  messageUpdates: Array<Record<string, unknown>>;
};

function database(
  options: {
    existingReplay?: Record<string, unknown>;
    message?: Record<string, unknown>;
    claimChanges?: number;
    fenced?: boolean;
  } = {},
) {
  const state: Stored = {
    replay: options.existingReplay,
    message: options.message,
    replayItems: [],
    messageUpdates: [],
  };
  const queries: string[] = [];
  return {
    state,
    queries,
    prepare(sql: string) {
      queries.push(sql);
      return {
        bind(...args: unknown[]) {
          return {
            async first<T>() {
              if (sql.includes("cf_account_deletion_intents"))
                return options.fenced ? ({ fenced: 1 } as T) : undefined;
              if (sql.includes("FROM cf_queue_dlq_replay_requests"))
                return state.replay as T | undefined;
              if (sql.includes("FROM cf_queue_dlq_messages"))
                return state.message as T | undefined;
              return undefined;
            },
            async run() {
              if (sql.includes("INSERT INTO cf_queue_dlq_replay_requests")) {
                state.replay = {
                  replay_id: String(args[0]),
                  idempotency_key: String(args[1]),
                  request_fingerprint: String(args[2]),
                  requested_count: Number(args[3]),
                  queued_count: 0,
                  skipped_count: 0,
                  failed_count: 0,
                  status: "queued",
                };
              } else if (
                sql.includes(
                  "UPDATE cf_queue_dlq_messages SET status = 'replay_queued'",
                )
              ) {
                state.messageUpdates.push({ status: "replay_queued", args });
              } else if (
                sql.includes(
                  "UPDATE cf_queue_dlq_messages SET status = 'replayed'",
                )
              ) {
                state.messageUpdates.push({ status: "replayed", args });
              } else if (
                sql.includes(
                  "UPDATE cf_queue_dlq_messages SET status = 'replay_failed'",
                )
              ) {
                state.messageUpdates.push({ status: "replay_failed", args });
              } else if (
                sql.includes("INSERT INTO cf_queue_dlq_replay_items")
              ) {
                state.replayItems.push({ args });
              } else if (
                sql.includes("UPDATE cf_queue_dlq_replay_requests SET")
              ) {
                if (state.replay) {
                  state.replay.queued_count = Number(args[0]);
                  state.replay.skipped_count = Number(args[1]);
                  state.replay.failed_count = Number(args[2]);
                  state.replay.status = String(args[3]);
                }
              } else if (sql.includes("INSERT INTO cf_queue_dlq_messages")) {
                state.message = {
                  queue_name: String(args[0]),
                  message_id: String(args[1]),
                  body_sha256: String(args[2]),
                  job_id: args[3],
                  uid: args[4],
                  kind: args[5],
                  payload_json: args[6],
                  delivery_attempts: Number(args[7]),
                  status: String(args[8]),
                  invalid_reason: args[9],
                };
              }
              return {
                success: true,
                meta: {
                  changes: sql.includes("status = 'replay_queued'")
                    ? (options.claimChanges ?? 1)
                    : 1,
                },
              };
            },
          };
        },
      };
    },
  };
}

function queue() {
  return {
    send: vi.fn(async (_message: JobMessage) => undefined),
  } as unknown as Queue<JobMessage>;
}

function env(db: ReturnType<typeof database>, jobsQueue = queue()): JobsEnv {
  return {
    APP_DB: db as unknown as D1Database,
    ADMIN_KEY: ADMIN_KEY,
    DLQ_REPLAY_SIGNING_SECRET: SIGNING_SECRET,
    DLQ_REPLAY_STAGING_ENABLED: "true",
    JOBS: jobsQueue,
    SYNC_FRESH: queue(),
    SYNC_BACKFILL: queue(),
  } as unknown as JobsEnv;
}

function signature(
  timestamp: string,
  idempotencyKey: string,
  body: string,
): string {
  return createHmac("sha256", SIGNING_SECRET)
    .update(`${timestamp}\n${idempotencyKey}\n${body}`)
    .digest("base64url");
}

function request(
  body: string,
  idempotencyKey = "replay-1",
  secret = ADMIN_KEY,
): Request {
  const timestamp = String(Math.floor(Date.now() / 1_000));
  return new Request("https://jobs.test/internal/cf/jobs/dlq/replay", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "secret-key": secret,
      "idempotency-key": idempotencyKey,
      "x-dlq-replay-timestamp": timestamp,
      "x-dlq-replay-signature": signature(timestamp, idempotencyKey, body),
    },
    body,
  });
}

function capturedMessage(messageId = "message-1"): Record<string, unknown> {
  const envelope = JSON.stringify({
    jobId: "job-1",
    uid: "user-1",
    kind: "probe",
    payload: {},
  });
  return {
    queue_name: DLQ,
    message_id: messageId,
    body_sha256: createHash("sha256")
      .update(envelope)
      .digest("hex"),
    job_id: "job-1",
    uid: "user-1",
    kind: "probe",
    payload_json: "{}",
    delivery_attempts: 3,
    status: "captured",
    invalid_reason: null,
    replay_id: null,
    replay_count: 0,
  };
}

describe("Queue DLQ replay boundary", () => {
  it("indexes a DLQ delivery without reading the Queue", async () => {
    const db = database();
    const ack = vi.fn();
    await captureDlqMessage(
      {
        id: "message-1",
        timestamp: new Date(),
        attempts: 3,
        body: { jobId: "job-1", uid: "user-1", kind: "probe", payload: {} },
        ack,
        retry: vi.fn(),
      } as unknown as Message<JobMessage>,
      env(db),
      DLQ,
    );
    expect(ack).toHaveBeenCalledOnce();
    expect(db.state.message).toMatchObject({
      queue_name: DLQ,
      message_id: "message-1",
      job_id: "job-1",
      uid: "user-1",
      kind: "probe",
      status: "captured",
      payload_json: "{}",
    });
  });

  it("drops late deliveries and blocks replay after account deletion", async () => {
    const db = database({ fenced: true, message: capturedMessage() });
    const ack = vi.fn();
    await captureDlqMessage(
      {
        id: "message-1",
        timestamp: new Date(),
        attempts: 3,
        body: { jobId: "job-1", uid: "user-1", kind: "probe", payload: {} },
        ack,
        retry: vi.fn(),
      } as unknown as Message<JobMessage>,
      env(db),
      DLQ,
    );
    expect(ack).toHaveBeenCalledOnce();
    expect(
      db.queries.some((query) =>
        query.includes("INSERT INTO cf_queue_dlq_messages"),
      ),
    ).toBe(false);

    const replay = await jobs.fetch(
      request(JSON.stringify({ message_ids: ["message-1"] }), "replay-fenced"),
      env(db),
    );
    expect(replay.status).toBe(200);
    expect(await replay.json()).toMatchObject({
      status: "partial",
      skippedCount: 1,
    });
  });

  it("fails closed when the operator gate is disabled", async () => {
    const db = database();
    const response = await jobs.fetch(
      request(JSON.stringify({ message_ids: ["message-1"] })),
      { ...env(db), DLQ_REPLAY_STAGING_ENABLED: "false" },
    );
    expect(response.status).toBe(503);
    expect(db.queries).toHaveLength(0);
  });

  it("requires both the admin key and content-bound signature", async () => {
    const db = database();
    const body = JSON.stringify({ message_ids: ["message-1"] });
    const badSignature = new Request(request(body), {
      headers: { "x-dlq-replay-signature": "invalid" },
    });
    const response = await jobs.fetch(badSignature, env(db));
    expect(response.status).toBe(403);
    expect(db.queries).toHaveLength(0);
  });

  it("republishes a captured message once and returns an idempotent result", async () => {
    const db = database({ message: capturedMessage() });
    const jobsQueue = queue();
    const state = env(db, jobsQueue);
    const body = JSON.stringify({ message_ids: ["message-1"] });
    const first = await jobs.fetch(request(body), state);
    expect(first.status).toBe(202);
    expect(await first.json()).toMatchObject({
      status: "completed",
      queuedCount: 1,
      skippedCount: 0,
      failedCount: 0,
    });
    expect(jobsQueue.send).toHaveBeenCalledOnce();
    expect(db.state.messageUpdates.map((entry) => entry.status)).toEqual([
      "replay_queued",
      "replayed",
    ]);

    const second = await jobs.fetch(request(body), state);
    expect(second.status).toBe(200);
    expect(await second.json()).toMatchObject({
      status: "completed",
      queuedCount: 1,
    });
    expect(jobsQueue.send).toHaveBeenCalledOnce();
  });

  it("does not replay unknown or invalid ids and bounds the request", async () => {
    const db = database();
    const unknown = await jobs.fetch(
      request(JSON.stringify({ message_ids: ["missing-message"] })),
      env(db),
    );
    expect(unknown.status).toBe(200);
    expect(await unknown.json()).toMatchObject({
      status: "partial",
      skippedCount: 1,
    });
    const tooMany = Array.from(
      { length: 51 },
      (_, index) => `message-${index}`,
    );
    const response = await jobs.fetch(
      request(JSON.stringify({ message_ids: tooMany })),
      env(database()),
    );
    expect(response.status).toBe(400);
  });

  it("rejects a D1 envelope whose stored digest no longer matches", async () => {
    const db = database({
      message: { ...capturedMessage(), body_sha256: "b".repeat(64) },
    });
    const response = await jobs.fetch(
      request(JSON.stringify({ message_ids: ["message-1"] }), "replay-tampered"),
      env(db),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({
      status: "partial",
      queuedCount: 0,
      skippedCount: 1,
    });
    expect(db.state.messageUpdates).toHaveLength(0);
  });
});
