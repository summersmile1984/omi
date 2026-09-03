import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Message } from "@cloudflare/workers-types";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import {
  processConversationMergeMessage,
  reconcileConversationMerges,
} from "../workers/jobs/conversation-merge";

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    this.database.exec("PRAGMA foreign_keys = ON");
    const directory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../migrations/app");
    for (const filename of readdirSync(directory).filter((value) => value.endsWith(".sql")).sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string) {
    const build = (args: unknown[] = []) => ({
      sql,
      args,
      bind: (...values: unknown[]) => build(values),
      first: async <T>() => (this.database.prepare(sql).get(...args.map((value) => value as never)) as T | undefined) ?? null,
      all: async <T>() => ({ results: this.database.prepare(sql).all(...args.map((value) => value as never)) as T[] }),
      run: async () => build(args).execute(),
      execute: () => ({
        success: true as const,
        results: [],
        meta: { changes: Number(this.database.prepare(sql).run(...args.map((value) => value as never)).changes) },
      }),
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

const databases: SqliteD1[] = [];

function environment(processorStatus = 200) {
  const database = new SqliteD1();
  databases.push(database);
  const sent: JobMessage[] = [];
  const env = {
    APP_DB: database,
    JOBS: { send: vi.fn(async (message: JobMessage) => sent.push(message)) },
    API_CORE: { fetch: vi.fn(async () => new Response(null, { status: processorStatus })) },
    INTERNAL_ASSERTION_SECRET: "jobs-merge-secret",
  } as unknown as JobsEnv;
  database.database
    .prepare(
      "INSERT INTO cf_conversations (uid, id, created_at, updated_at, status, transcript_segments_json, merge_job_id, merge_revision) " +
        "VALUES ('merge-user', 'conversation-a', 100, 100, 'merging', ?, 'job-merge-1', 100), " +
        "('merge-user', 'conversation-b', 101, 101, 'merging', ?, 'job-merge-1', 100)",
    )
    .run(JSON.stringify([{ text: "a", start: 0, end: 1 }]), JSON.stringify([{ text: "b", start: 0, end: 1 }]));
  database.database
    .prepare(
      "INSERT INTO cf_conversation_merge_jobs " +
        "(uid, job_id, source_conversation_ids_json, result_conversation_id, merge_revision, reprocess, status, attempts, next_attempt_at, request_fingerprint, created_at, updated_at) " +
        "VALUES ('merge-user', 'job-merge-1', ?, 'result-1', 100, 0, 'queued', 0, 0, 'fingerprint', 100, 100)",
    )
    .run(JSON.stringify(["conversation-a", "conversation-b"]));
  return { database, env, sent };
}

function message(kind: JobMessage["kind"] = "conversation_merge"): Message<JobMessage> {
  return {
    body: {
      jobId: "job-merge-1",
      uid: "merge-user",
      kind,
      payload: { conversationIds: ["conversation-a", "conversation-b"], revision: 100, reprocess: false },
    },
    attempts: 0,
    ack: vi.fn(),
    retry: vi.fn(),
  } as unknown as Message<JobMessage>;
}

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
});

describe("conversation merge jobs", () => {
  it("claims a queued merge and calls the API Core processor", async () => {
    const { database, env } = environment();
    const queued = message();
    await processConversationMergeMessage(queued, env);

    expect(queued.ack).toHaveBeenCalledOnce();
    expect(env.API_CORE?.fetch).toHaveBeenCalledOnce();
    const request = (env.API_CORE?.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as Request;
    expect(new URL(request.url).pathname).toBe("/internal/conversations/merge");
    await expect(request.json()).resolves.toMatchObject({ job_id: "job-merge-1", reprocess: false });
    expect(database.database.prepare("SELECT attempts, status FROM cf_conversation_merge_jobs").get()).toMatchObject({
      attempts: 1,
      status: "running",
    });
  });

  it("restores source ownership after terminal processor failure", async () => {
    const { database, env } = environment(400);
    const queued = message();
    Object.defineProperty(queued, "attempts", { value: 2 });
    await processConversationMergeMessage(queued, env);

    expect(queued.ack).toHaveBeenCalledOnce();
    expect(database.database.prepare("SELECT status FROM cf_conversation_merge_jobs").get()).toMatchObject({ status: "failed" });
    expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_conversations WHERE status = 'completed'").get()).toMatchObject({ count: 2 });
  });

  it("reconciles an expired merge back onto the Jobs queue", async () => {
    const { database, env, sent } = environment();
    database.database.prepare("UPDATE cf_conversation_merge_jobs SET status = 'running', lease_until = 99").run();
    await reconcileConversationMerges(env, 100);

    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ jobId: "job-merge-1", uid: "merge-user", kind: "conversation_merge" });
  });
});
