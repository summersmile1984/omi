import { describe, expect, it } from "vitest";
import { createSignedAuthContext } from "../workers/shared/auth-context";
import jobs from "../workers/jobs/index";
import type { JobMessage } from "../workers/jobs/env";

type StoredJob = {
  job_id: string;
  uid: string;
  kind: string;
  status: string;
  attempts: number;
  last_error: string | null;
  created_at: number;
  updated_at: number;
  idempotency_key?: string | null;
  result_json?: string | null;
} | null;

function fakeDatabase() {
  let stored: StoredJob = null;
  return {
    prepare: (sql: string) => ({
      bind: (...args: unknown[]) => ({
        first: async () => {
          if (!stored) return null;
          if (sql.includes("SELECT job_id"))
            return args[1] === stored.uid ? stored : null;
          if (sql.includes("SELECT status"))
            return args[1] === stored.uid ? { status: stored.status } : null;
          return null;
        },
        run: async () => {
          if (sql.includes("INSERT INTO cf_jobs")) {
            if (stored) return { success: true, meta: { changes: 0 } };
            stored = {
              job_id: String(args[0]),
              uid: String(args[1]),
              kind: String(args[2]),
              status: "queued",
              attempts: 0,
              last_error: null,
              created_at: Number(args[4]),
              updated_at: Number(args[5]),
            };
            return { success: true, meta: { changes: 1 } };
          }
          return { success: true, meta: { changes: 1 } };
        },
      }),
    }),
  };
}

function fakeTranscriptionDatabase() {
  let stored: StoredJob = null;
  return {
    get: () => stored,
    prepare: (sql: string) => ({
      bind: (...args: unknown[]) => ({
        first: async () => {
          if (sql.includes("WHERE uid = ? AND kind = 'transcribe'")) {
            if (
              stored &&
              stored.uid === args[0] &&
              stored.kind === "transcribe" &&
              stored.idempotency_key === args[1]
            ) {
              return { job_id: stored.job_id, status: stored.status };
            }
            return null;
          }
          if (sql.includes("SELECT status FROM cf_jobs")) {
            return stored && stored.job_id === args[0] && stored.uid === args[1]
              ? { status: stored.status }
              : null;
          }
          if (sql.includes("SELECT status, kind FROM cf_jobs")) {
            return stored && stored.job_id === args[0]
              ? { status: stored.status, kind: stored.kind }
              : null;
          }
          if (sql.includes("SELECT job_id, kind, status")) {
            return stored && stored.job_id === args[0] && stored.uid === args[1]
              ? stored
              : null;
          }
          return null;
        },
        run: async () => {
          if (sql.includes("INSERT INTO cf_jobs")) {
            if (stored) return { success: true, meta: { changes: 0 } };
            stored = {
              job_id: String(args[0]),
              uid: String(args[1]),
              kind: String(args[2]),
              status: "queued",
              attempts: 0,
              last_error: null,
              created_at: Number(args[4]),
              updated_at: Number(args[5]),
              idempotency_key: (args[6] as string | null) || null,
              result_json: null,
            };
            return { success: true, meta: { changes: 1 } };
          }
          if (!stored) return { success: true, meta: { changes: 0 } };
          if (sql.includes("status = 'running'")) {
            stored.status = "running";
            stored.attempts += 1;
            stored.updated_at = Number(args[0]);
          } else if (sql.includes("status = 'completed'")) {
            if (sql.includes("result_json"))
              stored.result_json = String(args[0]);
            stored.status = "completed";
            stored.updated_at = Number(
              sql.includes("result_json") ? args[1] : args[0],
            );
          } else if (sql.includes("status = 'failed'")) {
            stored.status = "failed";
            stored.last_error = String(args[0]);
            stored.updated_at = Number(args[1]);
          } else if (sql.includes("status = 'queued'")) {
            stored.status = "queued";
            stored.last_error = String(args[0]);
            stored.updated_at = Number(args[1]);
          }
          return { success: true, meta: { changes: 1 } };
        },
      }),
    }),
  };
}

function fakeAssets() {
  const blobs = new Map<string, Uint8Array>();
  return {
    blobs,
    put: async (key: string, body: ArrayBuffer) =>
      blobs.set(key, new Uint8Array(body)),
    get: async (key: string) => {
      const value = blobs.get(key);
      return value ? { arrayBuffer: async () => value.slice().buffer } : null;
    },
    delete: async (key: string) => void blobs.delete(key),
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

  it("deduplicates a stable job id", async () => {
    const database = fakeDatabase();
    const sent: unknown[] = [];
    const env = {
      APP_DB: database,
      JOBS: { send: async (message: unknown) => sent.push(message) },
      INTERNAL_ASSERTION_SECRET: "test-secret",
    };
    const headers = await signedHeaders(env.INTERNAL_ASSERTION_SECRET);
    const requestBody = JSON.stringify({
      jobId: "job-1",
      kind: "probe",
      payload: { source: "test" },
    });
    const first = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/jobs", {
        method: "POST",
        headers,
        body: requestBody,
      }),
      env as never,
    );
    const second = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/jobs", {
        method: "POST",
        headers: await signedHeaders(env.INTERNAL_ASSERTION_SECRET),
        body: requestBody,
      }),
      env as never,
    );
    expect(first.status).toBe(202);
    expect(second.status).toBe(200);
    expect(((await second.json()) as { status: string }).status).toBe(
      "already_queued",
    );
    expect(sent).toHaveLength(1);

    const statusResponse = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/jobs/job-1", {
        method: "GET",
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
        method: "GET",
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

  it("stages an audio body once and completes a native Workers AI transcription job", async () => {
    const database = fakeTranscriptionDatabase();
    const assets = fakeAssets();
    const sent: JobMessage[] = [];
    const env = {
      APP_DB: database,
      ASSETS: assets,
      AI: {
        run: async (model: string, input: Record<string, unknown>) => {
          expect(model).toContain("whisper");
          expect(typeof input.audio).toBe("string");
          return {
            text: "hello from workers",
            segments: [{ start: 0, end: 1, text: "hello from workers" }],
          };
        },
      },
      JOBS: { send: async (message: JobMessage) => void sent.push(message) },
      INTERNAL_ASSERTION_SECRET: "test-secret",
    };
    const headers = await signedHeaders(
      env.INTERNAL_ASSERTION_SECRET,
      "user-1",
      "POST",
      "/v1/cf/transcription-jobs",
    );
    headers.set("content-type", "audio/wav");
    headers.set("idempotency-key", "capture-1");
    const first = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/transcription-jobs", {
        method: "POST",
        headers,
        body: new Uint8Array([1, 2, 3, 4]),
      }),
      env as never,
    );
    const firstBody = (await first.json()) as { jobId: string; status: string };
    expect(first.status).toBe(202);
    expect(firstBody.status).toBe("queued");
    expect(sent).toHaveLength(1);
    expect(assets.blobs.size).toBe(1);

    const secondHeaders = await signedHeaders(
      env.INTERNAL_ASSERTION_SECRET,
      "user-1",
      "POST",
      "/v1/cf/transcription-jobs",
    );
    secondHeaders.set("content-type", "audio/wav");
    secondHeaders.set("idempotency-key", "capture-1");
    const second = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/transcription-jobs", {
        method: "POST",
        headers: secondHeaders,
      }),
      env as never,
    );
    expect(second.status).toBe(200);
    expect(await second.json()).toMatchObject({
      status: "already_queued",
      jobId: firstBody.jobId,
    });
    expect(sent).toHaveLength(1);

    const message = {
      id: "message-1",
      timestamp: new Date(),
      attempts: 1,
      body: sent[0],
      ack: () => undefined,
      retry: () => undefined,
    };
    await jobs.queue({ messages: [message] } as never, env as never);
    expect(database.get()?.status).toBe("completed");
    expect(assets.blobs.size).toBe(0);

    const status = await jobs.fetch(
      new Request(
        `https://jobs.test/v1/cf/transcription-jobs/${firstBody.jobId}`,
        {
          method: "GET",
          headers: await signedHeaders(
            env.INTERNAL_ASSERTION_SECRET,
            "user-1",
            "GET",
            `/v1/cf/transcription-jobs/${firstBody.jobId}`,
          ),
        },
      ),
      env as never,
    );
    expect(status.status).toBe(200);
    expect(await status.json()).toMatchObject({
      jobId: firstBody.jobId,
      status: "completed",
      result: { text: "hello from workers", provider: "workers-ai" },
    });
  });
});
