import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import {
  processConversationFinalizationMessage,
  reconcileConversationFinalizations,
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

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
});

describe("conversation finalization jobs", () => {
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
