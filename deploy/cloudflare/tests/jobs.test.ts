import { describe, expect, it } from "vitest";
import { encodeAuthContext, signAuthContext } from "../workers/shared/auth-context";
import jobs from "../workers/jobs/index";

type StoredJob = {
  job_id: string;
  uid: string;
  kind: string;
  status: string;
  attempts: number;
  last_error: string | null;
  created_at: number;
  updated_at: number;
} | null;

function fakeDatabase() {
  let stored: StoredJob = null;
  return {
    prepare: (sql: string) => ({
      bind: (...args: unknown[]) => ({
        first: async () => {
          if (!stored) return null;
          if (sql.includes("SELECT job_id")) return args[1] === stored.uid ? stored : null;
          if (sql.includes("SELECT status")) return args[1] === stored.uid ? { status: stored.status } : null;
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

async function signedHeaders(secret: string, uid = "user-1"): Promise<Headers> {
  const encoded = encodeAuthContext({ uid, authority: "better-auth", requestId: "req-1" });
  const signature = await signAuthContext(encoded, secret);
  return new Headers({ "x-omi-auth-context": encoded, "x-omi-internal-signature": signature || "" });
}

describe("jobs ingress", () => {
  it("rejects forged context before touching the queue", async () => {
    const response = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/jobs", {
        method: "POST",
        headers: { "x-omi-auth-context": "forged", "x-omi-internal-signature": "forged" },
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
    const requestBody = JSON.stringify({ jobId: "job-1", kind: "probe", payload: { source: "test" } });
    const first = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/jobs", { method: "POST", headers, body: requestBody }),
      env as never,
    );
    const second = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/jobs", { method: "POST", headers: await signedHeaders(env.INTERNAL_ASSERTION_SECRET), body: requestBody }),
      env as never,
    );
    expect(first.status).toBe(202);
    expect(second.status).toBe(200);
    expect((await second.json() as { status: string }).status).toBe("already_queued");
    expect(sent).toHaveLength(1);

    const statusResponse = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/jobs/job-1", { method: "GET", headers: await signedHeaders(env.INTERNAL_ASSERTION_SECRET) }),
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
        headers: await signedHeaders(env.INTERNAL_ASSERTION_SECRET, "user-2"),
      }),
      env as never,
    );
    expect(otherUserResponse.status).toBe(404);
  });
});
