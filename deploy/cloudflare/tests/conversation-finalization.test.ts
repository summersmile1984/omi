import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import {
  processConversationFinalizationMessage,
  reconcileConversationFinalizations,
  registerConversationFinalizationRoutes,
} from "../workers/jobs/conversation-finalization";

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
      sql,
      args,
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...args.map(sqliteValue)) as T | undefined) ?? null,
      all: async <T>() => ({
        success: true as const,
        results: this.database.prepare(sql).all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0 },
      }),
      run: async () => build(args).execute(),
      execute: () => {
        const statement = this.database.prepare(sql);
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

  async batch(statements: Array<{ execute(): unknown }>) {
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

  close() {
    this.database.close();
  }
}

function sqliteValue(value: unknown) {
  return value as never;
}

const databases: SqliteD1[] = [];

function environment(processorStatus = 200) {
  const database = new SqliteD1();
  databases.push(database);
  const sent: JobMessage[] = [];
  const env = {
    APP_DB: database,
    JOBS: { send: vi.fn(async (message: JobMessage) => sent.push(message)) },
    API_CORE: {
      fetch: vi.fn(async () => new Response(null, { status: processorStatus })),
    },
    INTERNAL_ASSERTION_SECRET: "jobs-finalization-secret",
  } as unknown as JobsEnv;
  database.database
    .prepare(
      "INSERT INTO cf_conversations (uid, id, created_at, updated_at, status, transcript_segments_json) " +
        "VALUES ('job-user', 'conversation-1', 100, 100, 'processing', ?)",
    )
    .run(JSON.stringify([{ text: "hello", start: 0, end: 2 }]));
  database.database
    .prepare(
      "INSERT INTO cf_conversation_finalization_jobs " +
        "(uid, conversation_id, job_id, finalization_revision, status, attempts, next_attempt_at, created_at, updated_at) " +
        "VALUES ('job-user', 'conversation-1', 'job-1', 100, 'queued', 0, 0, 100, 100)",
    )
    .run();
  database.database
    .prepare(
      "UPDATE cf_conversations SET finalization_job_id = 'job-1', finalization_revision = 100, finalization_status = 'queued' " +
        "WHERE uid = 'job-user' AND id = 'conversation-1'",
    )
    .run();
  return { database, env, sent };
}

function message(env: JobsEnv): Message<JobMessage> {
  return {
    body: {
      jobId: "job-1",
      uid: "job-user",
      kind: "conversation_finalize",
      payload: { conversationId: "conversation-1", revision: 100 },
    },
    attempts: 0,
    ack: vi.fn(),
    retry: vi.fn(),
  } as unknown as Message<JobMessage>;
}

function finalizationRunApp(authority = "better-auth") {
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerConversationFinalizationRoutes(app, async () => ({
    uid: "job-user",
    authority,
  }));
  return app;
}

function runRequest(body: unknown): Request {
  return new Request("https://jobs.test/v1/conversation-finalization-jobs/run", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
});

describe("conversation finalization jobs", () => {
  it("requeues only the authenticated account's queued finalization job", async () => {
    const { env, sent } = environment();
    const response = await finalizationRunApp().fetch(
      runRequest({ job_id: "job-1" }),
      env,
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      status: "queued",
      job_id: "job-1",
      operation: "finalize",
    });
    expect(sent).toEqual([
      {
        jobId: "job-1",
        uid: "job-user",
        kind: "conversation_finalize",
        payload: { conversationId: "conversation-1", revision: 100 },
      },
    ]);
  });

  it("drops unknown or cross-account finalization job ids without disclosure", async () => {
    const { env, sent } = environment();
    const crossAccountApp = new Hono<{ Bindings: JobsEnv }>();
    registerConversationFinalizationRoutes(crossAccountApp, async () => ({
      uid: "other-user",
      authority: "better-auth",
    }));

    for (const jobId of ["job-1", "missing-job"]) {
      const response = await crossAccountApp.fetch(runRequest({ job_id: jobId }), env);
      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toEqual({
        status: "dropped",
        reason: "unknown_job",
      });
    }
    expect(sent).toHaveLength(0);
  });

  it("acks terminal jobs and locks active leases", async () => {
    const state = environment();
    const app = finalizationRunApp();
    state.database.database
      .prepare("UPDATE cf_conversation_finalization_jobs SET status = 'completed' WHERE job_id = 'job-1'")
      .run();
    const terminal = await app.fetch(runRequest({ job_id: "job-1" }), state.env);
    expect(terminal.status).toBe(200);
    await expect(terminal.json()).resolves.toEqual({
      status: "acked",
      job_status: "completed",
    });

    state.database.database
      .prepare(
        "UPDATE cf_conversation_finalization_jobs SET status = 'running', lease_until = ? WHERE job_id = 'job-1'",
      )
      .run(Math.floor(Date.now() / 1_000) + 300);
    const locked = await app.fetch(runRequest({ job_id: "job-1" }), state.env);
    expect(locked.status).toBe(409);
    expect(locked.headers.get("retry-after")).toBe("10");
    await expect(locked.json()).resolves.toEqual({ status: "locked" });
  });

  it("requeues expired reprocess jobs with their operation parameters", async () => {
    const { database, env, sent } = environment();
    database.database
      .prepare(
        "UPDATE cf_conversation_finalization_jobs SET status = 'running', lease_until = 0, operation = 'reprocess', language_code = 'fr', app_id = 'calendar-app' WHERE job_id = 'job-1'",
      )
      .run();
    const response = await finalizationRunApp().fetch(
      runRequest({ job_id: "job-1" }),
      env,
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      status: "queued",
      job_id: "job-1",
      operation: "reprocess",
    });
    expect(sent[0]).toMatchObject({
      jobId: "job-1",
      uid: "job-user",
      kind: "conversation_reprocess",
      payload: {
        conversationId: "conversation-1",
        revision: 100,
        languageCode: "fr",
        appId: "calendar-app",
      },
    });
  });

  it("returns 503 when the finalization queue is unavailable", async () => {
    const { env } = environment();
    env.JOBS.send = vi.fn(async () => {
      throw new Error("queue unavailable");
    });
    const response = await finalizationRunApp().fetch(
      runRequest({ job_id: "job-1" }),
      env,
    );
    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toEqual({
      status: "retry",
      reason: "queue_unavailable",
    });
  });

  it("requires a Better Auth principal and a single bounded job_id", async () => {
    const { env } = environment();
    const firebase = await finalizationRunApp("firebase").fetch(
      runRequest({ job_id: "job-1" }),
      env,
    );
    expect(firebase.status).toBe(401);

    const invalid = await finalizationRunApp().fetch(
      runRequest({ job_id: "job-1", uid: "job-user" }),
      env,
    );
    expect(invalid.status).toBe(400);
    await expect(invalid.json()).resolves.toMatchObject({ code: "invalid_request" });
  });

  it("claims a queued job and calls API Core with a signed assertion", async () => {
    const { database, env } = environment();
    const queued = message(env);
    await processConversationFinalizationMessage(queued, env);

    expect(queued.ack).toHaveBeenCalledOnce();
    expect(env.API_CORE?.fetch).toHaveBeenCalledOnce();
    const request = (env.API_CORE?.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as Request;
    expect(new URL(request.url).pathname).toBe("/internal/conversations/finalize");
    expect(request.headers.get("x-omi-auth-context")).toBeTruthy();
    expect(request.headers.get("x-omi-internal-signature")).toBeTruthy();
    expect(
      database.database
        .prepare("SELECT attempts, status FROM cf_conversation_finalization_jobs WHERE job_id = 'job-1'")
        .get(),
    ).toMatchObject({ attempts: 1, status: "running" });
  });

  it("returns transient processor failures to the queue with a durable retry state", async () => {
    const { database, env } = environment(503);
    const queued = message(env);
    await processConversationFinalizationMessage(queued, env);

    expect(queued.retry).toHaveBeenCalledOnce();
    expect(queued.ack).not.toHaveBeenCalled();
    expect(
      database.database
        .prepare("SELECT attempts, status, last_error FROM cf_conversation_finalization_jobs WHERE job_id = 'job-1'")
        .get(),
    ).toMatchObject({ attempts: 1, status: "queued", last_error: "conversation finalization processor unavailable" });
  });

  it("reconciles queued and expired jobs back onto the Jobs queue", async () => {
    const { env, sent } = environment();
    await reconcileConversationFinalizations(env, 100);

    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      jobId: "job-1",
      uid: "job-user",
      kind: "conversation_finalize",
    });
  });

  it("preserves reprocess operation parameters when dispatching to API Core", async () => {
    const { database, env } = environment();
    database.database
      .prepare(
        "UPDATE cf_conversation_finalization_jobs SET operation = 'reprocess', language_code = 'fr', app_id = 'calendar-app' " +
          "WHERE job_id = 'job-1'",
      )
      .run();
    const queued = {
      ...message(env),
      body: {
        ...message(env).body,
        kind: "conversation_reprocess" as const,
        payload: {
          conversationId: "conversation-1",
          revision: 100,
          languageCode: "fr",
          appId: "calendar-app",
        },
      },
    } as unknown as Message<JobMessage>;

    await processConversationFinalizationMessage(queued, env);

    expect(queued.ack).toHaveBeenCalledOnce();
    const request = (env.API_CORE?.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as Request;
    await expect(request.json()).resolves.toMatchObject({
      operation: "reprocess",
      language_code: "fr",
      app_id: "calendar-app",
    });
  });
});
