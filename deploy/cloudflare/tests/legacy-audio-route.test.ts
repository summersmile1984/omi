import { describe, expect, it } from "vitest";
import type { JobMessage } from "../workers/jobs/env";
import jobs from "../workers/jobs/index";
import { createSignedAuthContext } from "../workers/shared/auth-context";

type StoredJob = {
  job_id: string;
  uid: string;
  kind: string;
  payload_json: string;
  status: string;
  attempts: number;
  last_error: string | null;
  idempotency_key: string | null;
  request_fingerprint: string;
  result_json: string | null;
};

function fakeDatabase(isLocked = 0) {
  const stored = new Map<string, StoredJob>();
  const conversation = {
    created_at: 1_000,
    updated_at: null as number | null,
    started_at: 1_000,
    is_locked: isLocked,
    audio_files_json: JSON.stringify([
      {
        id: "audio-1",
        provider: "gcs",
        chunk_timestamps: [1_000],
      },
    ]),
    conversation_audio_json: null,
  };
  const byIdempotency = (uid: string, kind: string, key: string) =>
    [...stored.values()].find(
      (job) =>
        job.uid === uid && job.kind === kind && job.idempotency_key === key,
    ) || null;
  return {
    stored,
    fail(jobId: string) {
      const job = stored.get(jobId)!;
      job.status = "failed";
      job.last_error = "legacy audio chunks are not available in R2";
    },
    prepare: (sql: string) => ({
      bind: (...args: unknown[]) => ({
        first: async () => {
          if (sql.includes("FROM cf_conversations")) {
            return args[0] === "user-1" && args[1] === "conversation-1"
              ? conversation
              : null;
          }
          if (sql.includes("idempotency_key = ?")) {
            return byIdempotency(
              String(args[0]),
              String(args[1]),
              String(args[2]),
            );
          }
          const job = stored.get(String(args[0]));
          return job?.uid === args[1] ? job : null;
        },
        run: async () => {
          if (sql.includes("INSERT INTO cf_jobs")) {
            const jobId = String(args[0]);
            const uid = String(args[1]);
            const kind = String(args[2]);
            const idempotencyKey = String(args[6]);
            if (stored.has(jobId) || byIdempotency(uid, kind, idempotencyKey)) {
              return { success: true, meta: { changes: 0 } };
            }
            stored.set(jobId, {
              job_id: jobId,
              uid,
              kind,
              payload_json: String(args[3]),
              status: "queued",
              attempts: 0,
              last_error: null,
              idempotency_key: idempotencyKey,
              request_fingerprint: String(args[7]),
              result_json: null,
            });
            return { success: true, meta: { changes: 1 } };
          }
          if (sql.includes("SET payload_json")) {
            const job = stored.get(String(args[2]));
            if (
              !job ||
              job.uid !== args[3] ||
              job.status !== args[4] ||
              job.request_fingerprint !== args[5]
            ) {
              return { success: true, meta: { changes: 0 } };
            }
            job.payload_json = String(args[0]);
            job.status = "queued";
            job.attempts = 0;
            job.last_error = null;
            job.result_json = null;
            return { success: true, meta: { changes: 1 } };
          }
          if (sql.includes("status = 'running'")) {
            const job = stored.get(String(args[1]));
            if (!job || job.uid !== args[2] || job.status !== "queued") {
              return { success: true, meta: { changes: 0 } };
            }
            job.status = "running";
            job.attempts += 1;
            return { success: true, meta: { changes: 1 } };
          }
          if (sql.includes("status = 'failed'")) {
            const job = stored.get(String(args[2]));
            if (!job || job.uid !== args[3]) {
              return { success: true, meta: { changes: 0 } };
            }
            job.status = "failed";
            job.last_error = String(args[0]);
            return { success: true, meta: { changes: 1 } };
          }
          throw new Error(`unhandled SQL: ${sql}`);
        },
      }),
    }),
  };
}

async function headers(secret: string) {
  const signed = await createSignedAuthContext(
    { uid: "user-1", authority: "better-auth", requestId: "request-1" },
    "jobs",
    "POST",
    "/v1/sync/audio/conversation-1/precache",
    secret,
  );
  return {
    "x-omi-auth-context": signed!.encoded,
    "x-omi-internal-signature": signed!.signature,
  };
}

describe("legacy audio precache route", () => {
  it("queues one deterministic rebuild and reopens a failed import after R2 copy", async () => {
    const secret = "test-internal-secret";
    const database = fakeDatabase();
    const queued: JobMessage[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: secret,
      APP_DB: database,
      ASSETS: { head: async () => null },
      JOBS: { send: async (message: JobMessage) => queued.push(message) },
    };
    const request = async () =>
      jobs.fetch(
        new Request("https://jobs.test/v1/sync/audio/conversation-1/precache", {
          method: "POST",
          headers: await headers(secret),
        }),
        env as never,
      );

    const first = await request();
    expect(first.status).toBe(202);
    const firstBody = (await first.json()) as {
      status: string;
      audio_file_count: number;
      job_id: string;
    };
    expect(firstBody).toMatchObject({
      status: "started",
      audio_file_count: 1,
    });
    expect(queued).toHaveLength(1);
    expect(queued[0]).toMatchObject({
      jobId: firstBody.job_id,
      uid: "user-1",
      kind: "legacy_audio_rebuild",
      payload: { conversationId: "conversation-1" },
    });

    const duplicate = await request();
    expect(duplicate.status).toBe(200);
    expect(await duplicate.json()).toMatchObject({
      status: "started",
      job_id: firstBody.job_id,
      job_state: "queued",
    });
    expect(queued).toHaveLength(1);

    database.fail(firstBody.job_id);
    const reopened = await request();
    expect(reopened.status).toBe(202);
    expect(queued).toHaveLength(2);
    expect(database.stored.get(firstBody.job_id)).toMatchObject({
      status: "queued",
      attempts: 0,
      last_error: null,
    });
  });

  it("keeps locked conversations fail-closed", async () => {
    const secret = "test-internal-secret";
    const response = await jobs.fetch(
      new Request("https://jobs.test/v1/sync/audio/conversation-1/precache", {
        method: "POST",
        headers: await headers(secret),
      }),
      {
        INTERNAL_ASSERTION_SECRET: secret,
        APP_DB: fakeDatabase(1),
      } as never,
    );
    expect(response.status).toBe(402);
  });

  it("claims the queued rebuild and records a deterministic missing-copy failure", async () => {
    const secret = "test-internal-secret";
    const database = fakeDatabase();
    const queued: JobMessage[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: secret,
      APP_DB: database,
      ASSETS: {
        head: async () => null,
        list: async () => ({ objects: [], truncated: false }),
      },
      JOBS: { send: async (message: JobMessage) => queued.push(message) },
    };
    const response = await jobs.fetch(
      new Request("https://jobs.test/v1/sync/audio/conversation-1/precache", {
        method: "POST",
        headers: await headers(secret),
      }),
      env as never,
    );
    expect(response.status).toBe(202);

    let acknowledged = false;
    const message = {
      id: "queue-message-1",
      timestamp: new Date(),
      attempts: 1,
      body: queued[0],
      ack: () => {
        acknowledged = true;
      },
      retry: () => {
        throw new Error("deterministic source errors must not retry");
      },
    };
    await jobs.queue({ messages: [message] } as never, env as never);

    expect(acknowledged).toBe(true);
    expect(database.stored.get(queued[0].jobId)).toMatchObject({
      status: "failed",
      attempts: 1,
      last_error: "legacy audio chunks are not available in R2",
    });
  });
});
