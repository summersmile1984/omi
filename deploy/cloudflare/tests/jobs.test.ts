import { describe, expect, it } from "vitest";
import { createSignedAuthContext } from "../workers/shared/auth-context";
import jobs from "../workers/jobs/index";
import type { JobMessage } from "../workers/jobs/env";

type StoredJob = {
  job_id: string;
  uid: string;
  kind: string;
  payload_json: string;
  status: string;
  attempts: number;
  last_error: string | null;
  created_at: number;
  updated_at: number;
  idempotency_key: string | null;
  request_fingerprint: string;
  result_json: string | null;
};

function fakeDatabase() {
  const stored = new Map<string, StoredJob>();
  let hideNextIdempotencyLookup = false;
  const throwNextSelectForJob = new Set<string>();

  const byIdempotency = (uid: string, kind: string, key: string) =>
    [...stored.values()].find(
      (job) =>
        job.uid === uid && job.kind === kind && job.idempotency_key === key,
    ) || null;

  return {
    get: (jobId: string) => stored.get(jobId) || null,
    hideNextIdempotencyLookup: () => {
      hideNextIdempotencyLookup = true;
    },
    throwNextSelectForJob: (jobId: string) => {
      throwNextSelectForJob.add(jobId);
    },
    prepare: (sql: string) => ({
      bind: (...args: unknown[]) => ({
        first: async () => {
          if (sql.includes("idempotency_key = ?")) {
            if (hideNextIdempotencyLookup) {
              hideNextIdempotencyLookup = false;
              return null;
            }
            return byIdempotency(
              String(args[0]),
              String(args[1]),
              String(args[2]),
            );
          }
          const jobId = String(args[0]);
          if (throwNextSelectForJob.delete(jobId)) {
            throw new Error("simulated D1 read failure");
          }
          const row = stored.get(jobId);
          return row && row.uid === args[1] ? row : null;
        },
        run: async () => {
          if (sql.includes("INSERT INTO cf_jobs")) {
            const jobId = String(args[0]);
            const uid = String(args[1]);
            const kind = String(args[2]);
            const idempotencyKey = (args[6] as string | null) || null;
            const conflict =
              stored.has(jobId) ||
              (idempotencyKey !== null &&
                byIdempotency(uid, kind, idempotencyKey) !== null);
            if (conflict) return { success: true, meta: { changes: 0 } };
            stored.set(jobId, {
              job_id: jobId,
              uid,
              kind,
              payload_json: String(args[3]),
              status: "queued",
              attempts: 0,
              last_error: null,
              created_at: Number(args[4]),
              updated_at: Number(args[5]),
              idempotency_key: idempotencyKey,
              request_fingerprint: String(args[7]),
              result_json: null,
            });
            return { success: true, meta: { changes: 1 } };
          }

          let row: StoredJob | undefined;
          if (sql.includes("SET payload_json")) {
            row = stored.get(String(args[2]));
            if (
              !row ||
              row.uid !== args[3] ||
              row.status !== "failed" ||
              row.last_error !== "queue unavailable" ||
              row.request_fingerprint !== args[4]
            ) {
              return { success: true, meta: { changes: 0 } };
            }
            row.payload_json = String(args[0]);
            row.status = "queued";
            row.last_error = null;
            row.updated_at = Number(args[1]);
          } else if (sql.includes("status = 'running'")) {
            row = stored.get(String(args[1]));
            if (
              !row ||
              row.uid !== args[2] ||
              !(
                row.status === "queued" ||
                (row.status === "running" && row.updated_at <= Number(args[3]))
              )
            ) {
              return { success: true, meta: { changes: 0 } };
            }
            row.status = "running";
            row.attempts += 1;
            row.updated_at = Number(args[0]);
          } else if (
            sql.includes("status = 'completed'") &&
            sql.includes("result_json")
          ) {
            row = stored.get(String(args[2]));
            if (!row || row.uid !== args[3])
              return { success: true, meta: { changes: 0 } };
            row.result_json = String(args[0]);
            row.status = "completed";
            row.last_error = null;
            row.updated_at = Number(args[1]);
          } else if (sql.includes("status = 'completed'")) {
            row = stored.get(String(args[1]));
            if (!row || row.uid !== args[2])
              return { success: true, meta: { changes: 0 } };
            row.status = "completed";
            row.updated_at = Number(args[0]);
          } else if (sql.includes("status = 'failed'")) {
            row = stored.get(String(args[2]));
            if (sql.includes("AND uid = ?") && (!row || row.uid !== args[3]))
              return { success: true, meta: { changes: 0 } };
            if (!row) return { success: true, meta: { changes: 0 } };
            row.status = "failed";
            row.last_error = String(args[0]);
            row.updated_at = Number(args[1]);
          } else if (sql.includes("status = 'queued'")) {
            row = stored.get(String(args[2]));
            if (!row || row.uid !== args[3])
              return { success: true, meta: { changes: 0 } };
            row.status = "queued";
            row.last_error = String(args[0]);
            row.updated_at = Number(args[1]);
          }
          return { success: true, meta: { changes: row ? 1 : 0 } };
        },
      }),
    }),
  };
}

function fakeAssets() {
  const blobs = new Map<string, Uint8Array>();
  return {
    blobs,
    put: async (key: string, body: ArrayBuffer) => {
      blobs.set(key, new Uint8Array(body));
    },
    get: async (key: string) => {
      const value = blobs.get(key);
      return value ? { arrayBuffer: async () => value.slice().buffer } : null;
    },
    delete: async (key: string) => {
      blobs.delete(key);
    },
  };
}

async function signedHeaders(
  secret: string,
  uid = "user-1",
  method = "POST",
  path = "/v1/cf/jobs",
): Promise<Headers> {
  const signed = await createSignedAuthContext(
    { uid, authority: "better-auth", requestId: "req-1" },
    "jobs",
    method,
    path,
    secret,
  );
  return new Headers({
    "x-omi-auth-context": signed?.encoded || "",
    "x-omi-internal-signature": signed?.signature || "",
  });
}

function queueMessage(body: JobMessage, attempts = 1) {
  let acknowledged = false;
  const retries: unknown[] = [];
  return {
    message: {
      id: `message-${body.jobId}`,
      timestamp: new Date(),
      attempts,
      body,
      ack: () => {
        acknowledged = true;
      },
      retry: (options?: unknown) => {
        retries.push(options);
      },
    },
    acknowledged: () => acknowledged,
    retries,
  };
}

function jobsEnvironment(
  database: ReturnType<typeof fakeDatabase>,
  assets = fakeAssets(),
  aiRun: (
    model: string,
    input: Record<string, unknown>,
  ) => Promise<unknown> = async () => ({
    text: "hello from workers",
    segments: [],
  }),
) {
  const sent: JobMessage[] = [];
  let queueSendFailures = 0;
  return {
    assets,
    sent,
    failNextQueueSend: () => {
      queueSendFailures += 1;
    },
    env: {
      APP_DB: database,
      ASSETS: assets,
      AI: { run: aiRun },
      JOBS: {
        send: async (message: JobMessage) => {
          if (queueSendFailures > 0) {
            queueSendFailures -= 1;
            throw new Error("simulated queue failure");
          }
          sent.push(message);
        },
      },
      INTERNAL_ASSERTION_SECRET: "test-secret",
    },
  };
}

async function enqueueProbe(
  env: ReturnType<typeof jobsEnvironment>["env"],
  jobId: string,
  payload: Record<string, unknown> = { source: "test" },
) {
  return jobs.fetch(
    new Request("https://jobs.test/v1/cf/jobs", {
      method: "POST",
      headers: await signedHeaders(env.INTERNAL_ASSERTION_SECRET),
      body: JSON.stringify({ jobId, kind: "probe", payload }),
    }),
    env as never,
  );
}

async function enqueueTranscription(
  env: ReturnType<typeof jobsEnvironment>["env"],
  bytes: Uint8Array,
  idempotencyKey: string,
) {
  const headers = await signedHeaders(
    env.INTERNAL_ASSERTION_SECRET,
    "user-1",
    "POST",
    "/v1/cf/transcription-jobs",
  );
  headers.set("content-type", "audio/wav");
  headers.set("idempotency-key", idempotencyKey);
  return jobs.fetch(
    new Request("https://jobs.test/v1/cf/transcription-jobs", {
      method: "POST",
      headers,
      body: bytes.buffer as ArrayBuffer,
    }),
    env as never,
  );
}

describe("jobs ingress", () => {
  it("rejects forged context before touching the queue", async () => {
    const response = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/jobs", {
        method: "POST",
        headers: {
          "x-omi-auth-context": "forged",
          "x-omi-internal-signature": "forged",
        },
        body: "{}",
      }),
      { INTERNAL_ASSERTION_SECRET: "test-secret" } as never,
    );
    expect(response.status).toBe(401);
  });

  it("deduplicates a stable job id and rejects identity reuse with a different payload", async () => {
    const database = fakeDatabase();
    const { env, sent } = jobsEnvironment(database);
    const first = await enqueueProbe(env, "job-1");
    const duplicate = await enqueueProbe(env, "job-1");
    const conflict = await enqueueProbe(env, "job-1", { source: "other" });

    expect(first.status).toBe(202);
    expect(duplicate.status).toBe(200);
    expect(await duplicate.json()).toMatchObject({
      status: "already_queued",
      jobId: "job-1",
    });
    expect(conflict.status).toBe(409);
    expect(sent).toHaveLength(1);

    const statusResponse = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/jobs/job-1", {
        headers: await signedHeaders(
          env.INTERNAL_ASSERTION_SECRET,
          "user-1",
          "GET",
          "/v1/cf/jobs/job-1",
        ),
      }),
      env as never,
    );
    expect(statusResponse.status).toBe(200);
    expect(await statusResponse.json()).toMatchObject({
      jobId: "job-1",
      kind: "probe",
      status: "queued",
      attempts: 0,
      lastError: null,
    });

    const otherUserResponse = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/jobs/job-1", {
        headers: await signedHeaders(
          env.INTERNAL_ASSERTION_SECRET,
          "user-2",
          "GET",
          "/v1/cf/jobs/job-1",
        ),
      }),
      env as never,
    );
    expect(otherUserResponse.status).toBe(404);
  });

  it("resolves a raced idempotent insert and removes the losing staged object", async () => {
    const database = fakeDatabase();
    const { env, sent, assets } = jobsEnvironment(database);
    const audio = new Uint8Array([1, 2, 3, 4]);
    const first = await enqueueTranscription(env, audio, "capture-1");
    const firstBody = (await first.json()) as { jobId: string };
    database.hideNextIdempotencyLookup();
    const duplicate = await enqueueTranscription(env, audio, "capture-1");

    expect(first.status).toBe(202);
    expect(duplicate.status).toBe(200);
    expect(await duplicate.json()).toMatchObject({
      status: "already_queued",
      jobId: firstBody.jobId,
    });
    expect(sent).toHaveLength(1);
    expect(assets.blobs.size).toBe(1);
  });

  it("rejects an idempotency key reused with different audio without leaking the staged object", async () => {
    const database = fakeDatabase();
    const { env, sent, assets } = jobsEnvironment(database);
    const first = await enqueueTranscription(
      env,
      new Uint8Array([1, 2, 3]),
      "capture-1",
    );
    const conflict = await enqueueTranscription(
      env,
      new Uint8Array([9, 8, 7]),
      "capture-1",
    );

    expect(first.status).toBe(202);
    expect(conflict.status).toBe(409);
    expect(await conflict.json()).toMatchObject({
      error: "idempotency key reused with different payload",
    });
    expect(sent).toHaveLength(1);
    expect(assets.blobs.size).toBe(1);
  });

  it("repairs an exact retry after queue publication failed", async () => {
    const database = fakeDatabase();
    const { env, sent, assets, failNextQueueSend } = jobsEnvironment(database);
    const audio = new Uint8Array([1, 2, 3]);
    failNextQueueSend();
    const failed = await enqueueTranscription(env, audio, "capture-1");
    const repaired = await enqueueTranscription(env, audio, "capture-1");

    expect(failed.status).toBe(503);
    expect(repaired.status).toBe(202);
    const repairedBody = (await repaired.json()) as { jobId: string };
    expect(database.get(repairedBody.jobId)?.status).toBe("queued");
    expect(database.get(repairedBody.jobId)?.last_error).toBeNull();
    expect(sent).toHaveLength(1);
    expect(assets.blobs.size).toBe(1);
  });

  it("completes native Workers AI transcription and removes staged audio", async () => {
    const database = fakeDatabase();
    const { env, sent, assets } = jobsEnvironment(
      database,
      fakeAssets(),
      async (model, input) => {
        expect(model).toContain("whisper");
        expect(typeof input.audio).toBe("string");
        return {
          text: "hello from workers",
          segments: [{ start: 0, end: 1, text: "hello from workers" }],
        };
      },
    );
    const response = await enqueueTranscription(
      env,
      new Uint8Array([1, 2, 3, 4]),
      "capture-1",
    );
    const body = (await response.json()) as { jobId: string };
    const delivery = queueMessage(sent[0]);
    await jobs.queue({ messages: [delivery.message] } as never, env as never);

    expect(database.get(body.jobId)?.status).toBe("completed");
    expect(delivery.acknowledged()).toBe(true);
    expect(delivery.retries).toHaveLength(0);
    expect(assets.blobs.size).toBe(0);

    const status = await jobs.fetch(
      new Request(`https://jobs.test/v1/cf/transcription-jobs/${body.jobId}`, {
        headers: await signedHeaders(
          env.INTERNAL_ASSERTION_SECRET,
          "user-1",
          "GET",
          `/v1/cf/transcription-jobs/${body.jobId}`,
        ),
      }),
      env as never,
    );
    expect(status.status).toBe(200);
    expect(await status.json()).toMatchObject({
      jobId: body.jobId,
      status: "completed",
      result: { text: "hello from workers", provider: "workers-ai" },
    });
  });
});

describe("jobs consumer", () => {
  it("isolates a failed message so later messages in the batch still complete", async () => {
    const database = fakeDatabase();
    const { env, sent } = jobsEnvironment(database);
    await enqueueProbe(env, "job-fails");
    await enqueueProbe(env, "job-completes");
    database.throwNextSelectForJob("job-fails");
    const failed = queueMessage(sent[0]);
    const completed = queueMessage(sent[1]);

    await jobs.queue(
      { messages: [failed.message, completed.message] } as never,
      env as never,
    );

    expect(failed.retries).toHaveLength(1);
    expect(failed.acknowledged()).toBe(false);
    expect(completed.acknowledged()).toBe(true);
    expect(database.get("job-completes")?.status).toBe("completed");
  });

  it("does not claim a job owned by another uid", async () => {
    const database = fakeDatabase();
    const { env, sent, assets } = jobsEnvironment(database);
    await enqueueProbe(env, "job-1");
    const body: JobMessage = {
      ...sent[0],
      uid: "user-2",
      kind: "transcribe",
      payload: {
        objectKey: "cf-transcriptions/user-2/stale",
        contentType: "audio/wav",
      },
    };
    assets.blobs.set(
      "cf-transcriptions/user-2/stale",
      new Uint8Array([1, 2, 3]),
    );
    const delivery = queueMessage(body);

    await jobs.queue({ messages: [delivery.message] } as never, env as never);

    expect(delivery.acknowledged()).toBe(true);
    expect(database.get("job-1")?.status).toBe("queued");
    expect(assets.blobs.size).toBe(0);
  });

  it("marks terminal provider failure, cleans audio, and retries for the DLQ", async () => {
    const database = fakeDatabase();
    const { env, sent, assets } = jobsEnvironment(
      database,
      fakeAssets(),
      async () => {
        throw new Error("provider unavailable");
      },
    );
    const response = await enqueueTranscription(
      env,
      new Uint8Array([1, 2, 3]),
      "capture-1",
    );
    const body = (await response.json()) as { jobId: string };
    const delivery = queueMessage(sent[0], 3);

    await jobs.queue({ messages: [delivery.message] } as never, env as never);

    expect(database.get(body.jobId)?.status).toBe("failed");
    expect(database.get(body.jobId)?.last_error).toContain("unavailable");
    expect(assets.blobs.size).toBe(0);
    expect(delivery.acknowledged()).toBe(false);
    expect(delivery.retries).toEqual([{ delaySeconds: 10 }]);
  });
});

describe("jobs scheduled cleanup", () => {
  it("preserves active assets and isolates a failed orphan deletion", async () => {
    const tasks = new Map([
      ["cf-assets/user-1/active", { uid: "user-1", attempts: 0 }],
      ["cf-assets/user-1/orphan-ok", { uid: "user-1", attempts: 0 }],
      ["cf-assets/user-1/orphan-retry", { uid: "user-1", attempts: 0 }],
    ]);
    const active = new Set(["cf-assets/user-1/active"]);
    const deleted: string[] = [];
    let failOrphanOnce = true;
    const env = {
      APP_DB: {
        prepare: (sql: string) => ({
          bind: (...args: unknown[]) => ({
            all: async () => ({
              results: [...tasks].map(([storage_key, task]) => ({
                storage_key,
                uid: task.uid,
              })),
            }),
            first: async () =>
              active.has(String(args[1])) ? { active: 1 } : null,
            run: async () => {
              const storageKey = String(args[sql.startsWith("DELETE") ? 0 : 2]);
              if (sql.startsWith("DELETE")) tasks.delete(storageKey);
              else {
                const task = tasks.get(storageKey);
                if (task) task.attempts += 1;
              }
              return { success: true, meta: { changes: 1 } };
            },
          }),
        }),
      },
      ASSETS: {
        delete: async (storageKey: string) => {
          if (
            storageKey === "cf-assets/user-1/orphan-retry" &&
            failOrphanOnce
          ) {
            failOrphanOnce = false;
            throw new Error("simulated R2 delete failure");
          }
          deleted.push(storageKey);
        },
      },
    };

    await jobs.scheduled({} as never, env as never);
    expect(deleted).toEqual(["cf-assets/user-1/orphan-ok"]);
    expect(tasks.has("cf-assets/user-1/active")).toBe(false);
    expect(tasks.has("cf-assets/user-1/orphan-ok")).toBe(false);
    expect(tasks.get("cf-assets/user-1/orphan-retry")?.attempts).toBe(1);

    await jobs.scheduled({} as never, env as never);
    expect(deleted).toEqual([
      "cf-assets/user-1/orphan-ok",
      "cf-assets/user-1/orphan-retry",
    ]);
    expect(tasks.size).toBe(0);
  });
});
