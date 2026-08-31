import { describe, expect, it, vi } from "vitest";
import type { Message } from "@cloudflare/workers-types";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import { processTaskIntelligenceMessage } from "../workers/jobs/task-intelligence";

function row(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    uid: "task-user",
    job_id: "task-job-1",
    account_generation: 1,
    device_id: "account",
    status: "queued",
    attempts: 1,
    lease_until: null,
    next_attempt_at: 0,
    ...overrides,
  };
}

function environment(job = row(), processorStatus = 503) {
  const fetch = vi.fn(async () => new Response(null, { status: processorStatus }));
  const env = {
    APP_DB: {
      prepare: vi.fn((sql: string) => ({
        bind: (..._args: unknown[]) => ({
          first: async () => (sql.includes("FROM cf_task_intelligence_jobs") ? job : null),
          all: async () => ({ results: [] }),
          run: async () => ({ meta: { changes: 1 } }),
        }),
      })),
    },
    API_CORE: { fetch },
    INTERNAL_ASSERTION_SECRET: "task-intelligence-test-secret",
  } as unknown as JobsEnv;
  return { env, fetch };
}

function message(): Message<JobMessage> {
  return {
    body: {
      jobId: "task-job-1",
      uid: "task-user",
      kind: "task_intelligence_evaluate",
      payload: {},
    },
    attempts: 0,
    ack: vi.fn(),
    retry: vi.fn(),
  } as unknown as Message<JobMessage>;
}

describe("task-intelligence Queue consumer", () => {
  it("calls the signed API Core processor and retries a transient provider failure", async () => {
    const { env, fetch } = environment();
    const queued = message();

    await processTaskIntelligenceMessage(queued, env);

    expect(fetch).toHaveBeenCalledOnce();
    const request = (fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls[0][0] as Request;
    expect(new URL(request.url).pathname).toBe("/internal/task-intelligence/evaluate");
    expect(request.headers.get("x-omi-auth-context")).toBeTruthy();
    expect(request.headers.get("x-omi-internal-signature")).toBeTruthy();
    await expect(request.json()).resolves.toEqual({
      job_id: "task-job-1",
      account_generation: 1,
      device_id: "account",
    });
    expect(queued.retry).toHaveBeenCalledOnce();
    expect(queued.ack).not.toHaveBeenCalled();
  });

  it("does not call the provider while the D1 lease is delayed", async () => {
    const { env, fetch } = environment(row({ next_attempt_at: Math.floor(Date.now() / 1_000) + 30 }), 200);
    const queued = message();

    await processTaskIntelligenceMessage(queued, env);

    expect(fetch).not.toHaveBeenCalled();
    expect(queued.retry).toHaveBeenCalledOnce();
    expect(queued.retry).toHaveBeenCalledWith({ delaySeconds: expect.any(Number) });
  });

  it("acks a completed or terminal D1 job without invoking API Core", async () => {
    const { env, fetch } = environment(row({ status: "failed" }), 200);
    const queued = message();

    await processTaskIntelligenceMessage(queued, env);

    expect(fetch).not.toHaveBeenCalled();
    expect(queued.ack).toHaveBeenCalledOnce();
    expect(queued.retry).not.toHaveBeenCalled();
  });
});
