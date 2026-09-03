import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Message } from "@cloudflare/workers-types";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import jobs from "../workers/jobs/index";
import { createSignedAuthContext } from "../workers/shared/auth-context";

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
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...(args as never[])) as T | undefined) ?? null,
      all: async <T>() => ({
        results: this.database.prepare(sql).all(...(args as never[])) as T[],
      }),
      run: async <T>() => {
        const statement = this.database.prepare(sql);
        if (/\bRETURNING\b/i.test(sql)) {
          const results = statement.all(...(args as never[])) as T[];
          return { meta: { changes: results.length }, results };
        }
        return {
          meta: { changes: Number(statement.run(...(args as never[])).changes) },
        };
      },
    });
    return build();
  }

  async batch(statements: Array<{ run(): Promise<unknown> }>) {
    return Promise.all(statements.map((statement) => statement.run()));
  }

  close() {
    this.database.close();
  }
}

function providerResult() {
  const event = {
    date: "March 15",
    title: "A memorable day",
    description: "A grounded description from the supplied conversation summaries.",
    story: "A grounded story from the supplied conversation summaries.",
    emoji: "🎉",
  };
  return {
    decision_style: { name: "Reflective Executor", description: "You reflect, then move decisively." },
    top_phrases: [{ phrase: "Let's do this", context: "When beginning important work." }],
    memorable_days: {
      most_fun_day: event,
      most_productive_day: event,
      most_stressful_day: event,
    },
    funniest_event: event,
    most_embarrassing_event: event,
    top_buddies: [{ name: "Alex", relationship: "Friend", context: "A recurring collaborator in the year.", emoji: "🤝" }],
    obsessions: { show: "Not mentioned", movie: "Not mentioned", book: "Not mentioned", celebrity: "Not mentioned", food: "Not mentioned" },
    movie_recommendations: ["Inception"],
    struggle: { title: "A hard season", description: "You kept moving through a difficult stretch." },
    personal_win: { title: "Steady progress", description: "You made meaningful progress through consistent effort." },
  };
}

const databases: SqliteD1[] = [];

function environment(ai = providerResult()) {
  const database = new SqliteD1();
  databases.push(database);
  const sent: JobMessage[] = [];
  const env = {
    APP_DB: database,
    INTERNAL_ASSERTION_SECRET: "wrapped-test-secret",
    JOBS: { send: vi.fn(async (message: JobMessage) => sent.push(message)) },
    AI: { run: vi.fn(async () => ({ response: ai })) },
  } as unknown as JobsEnv;
  database.database
    .prepare(
      "INSERT INTO cf_account_cutover (uid, state, account_generation, ui_generation, api_generation, checkpoint_phase, destination_backend_bound, updated_at) VALUES ('wrapped-user', 'new', 4, 4, 4, 'completed', 1, 1000)",
    )
    .run();
  database.database
    .prepare(
      "INSERT INTO cf_conversations (uid, id, created_at, updated_at, started_at, finished_at, status, structured_json, transcript_segments_json) VALUES ('wrapped-user', 'conversation-1', ?, ?, ?, ?, 'completed', ?, ?)",
    )
    .run(
      Math.floor(Date.UTC(2025, 2, 15) / 1_000),
      Math.floor(Date.UTC(2025, 2, 15) / 1_000),
      Math.floor(Date.UTC(2025, 2, 15) / 1_000),
      Math.floor(Date.UTC(2025, 2, 15, 0, 10) / 1_000),
      JSON.stringify({ title: "Planning", overview: "Planned a launch with Alex.", category: "work" }),
      JSON.stringify([{ text: "Let's do this", end: 120, is_user: true }]),
    );
  database.database
    .prepare(
      "INSERT INTO cf_action_items (uid, id, description, status, completed, created_at, updated_at) VALUES ('wrapped-user', 'action-1', 'Ship the launch', 'completed', 1, ?, ?)",
    )
    .run(Math.floor(Date.UTC(2025, 2, 15) / 1_000), Math.floor(Date.UTC(2025, 2, 15) / 1_000));
  return { database, env, sent };
}

async function signedHeaders(method: "GET" | "POST", pathname: string) {
  const signed = await createSignedAuthContext(
    { uid: "wrapped-user", authority: "better-auth", requestId: "wrapped-test" },
    "jobs",
    method,
    pathname,
    "wrapped-test-secret",
  );
  return {
    "x-omi-auth-context": signed!.encoded,
    "x-omi-internal-signature": signed!.signature,
  };
}

function queueMessage(body: JobMessage): Message<JobMessage> {
  return { body, attempts: 1, ack: vi.fn(), retry: vi.fn() } as unknown as Message<JobMessage>;
}

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
  vi.restoreAllMocks();
});

describe("Cloudflare Wrapped D1/Queue authority", () => {
  it("aggregates bounded D1 projections, generates a structured result, and notifies once", async () => {
    const { env, sent } = environment();
    const post = await jobs.fetch(
      new Request("https://jobs.test/v1/wrapped/2025/generate", {
        method: "POST",
        headers: { ...(await signedHeaders("POST", "/v1/wrapped/2025/generate")) },
      }),
      env,
    );
    expect(post.status).toBe(200);
    expect(await post.json()).toMatchObject({ status: "processing" });
    expect(sent).toHaveLength(1);

    const message = queueMessage(sent[0]);
    await jobs.queue({ messages: [message] } as never, env);
    expect(message.ack).toHaveBeenCalledOnce();
    const stored = await env.APP_DB.prepare(
      "SELECT status, result_json FROM cf_wrapped_jobs WHERE uid = 'wrapped-user' AND year = 2025",
    ).first<{ status: string; result_json: string }>();
    expect(stored?.status).toBe("completed");
    expect(JSON.parse(stored!.result_json)).toMatchObject({
      total_conversations: 1,
      days_active: 1,
      total_action_items: 1,
      completed_action_items: 1,
      decision_style: { name: "Reflective Executor" },
    });
    expect(await env.APP_DB.prepare(
      "SELECT source_kind, source_id FROM cf_notification_outbox WHERE uid = 'wrapped-user'",
    ).first()).toMatchObject({ source_kind: "integration", source_id: expect.stringMatching(/^wrapped:/) });

    const get = await jobs.fetch(
      new Request("https://jobs.test/v1/wrapped/2025", {
        headers: await signedHeaders("GET", "/v1/wrapped/2025"),
      }),
      env,
    );
    expect(get.status).toBe(200);
    expect(await get.json()).toMatchObject({ status: "completed", result: { total_conversations: 1 } });
  });

  it("retries a transient Workers AI failure without losing the D1 lease contract", async () => {
    const { env, sent } = environment();
    const ai = env.AI.run as unknown as ReturnType<typeof vi.fn>;
    ai.mockRejectedValueOnce(new Error("provider unavailable"));
    const post = await jobs.fetch(
      new Request("https://jobs.test/v1/wrapped/2025/generate", {
        method: "POST",
        headers: await signedHeaders("POST", "/v1/wrapped/2025/generate"),
      }),
      env,
    );
    expect(post.status).toBe(200);
    const first = queueMessage(sent[0]);
    await jobs.queue({ messages: [first] } as never, env);
    expect(first.retry).toHaveBeenCalledOnce();
    expect(await env.APP_DB.prepare("SELECT status, attempts FROM cf_wrapped_jobs WHERE uid = 'wrapped-user' AND year = 2025").first()).toMatchObject({ status: "queued", attempts: 1 });
    env.APP_DB.prepare("UPDATE cf_wrapped_jobs SET next_attempt_at = 0 WHERE uid = 'wrapped-user' AND year = 2025").run();
    const retry = queueMessage(sent[0]);
    await jobs.queue({ messages: [retry] } as never, env);
    expect(retry.ack).toHaveBeenCalledOnce();
    expect(await env.APP_DB.prepare("SELECT status, attempts FROM cf_wrapped_jobs WHERE uid = 'wrapped-user' AND year = 2025").first()).toMatchObject({ status: "completed", attempts: 2 });
  });

  it("rejects requests when the account deletion fence is active", async () => {
    const { database, env, sent } = environment();
    const now = Math.floor(Date.now() / 1_000);
    database.database
      .prepare("INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES ('wrapped-user', 'delete-1', 'pending', 'quiescing', ?, ?, ?)")
      .run(now, now, now);
    const response = await jobs.fetch(
      new Request("https://jobs.test/v1/wrapped/2025/generate", {
        method: "POST",
        headers: await signedHeaders("POST", "/v1/wrapped/2025/generate"),
      }),
      env,
    );
    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({ error: "account_deletion_in_progress" });
    expect(sent).toHaveLength(0);
  });
});
