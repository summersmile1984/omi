import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Hono } from "hono";
import { registerChatCompatibilityRoutes } from "../workers/jobs/chat-compatibility";
import type { JobsEnv } from "../workers/jobs/env";

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
      run: async () => ({ meta: { changes: Number(this.database.prepare(sql).run(...(args as never[])).changes) } }),
    });
    return build();
  }

  async batch(statements: D1PreparedStatement[]) {
    return Promise.all(statements.map((statement) => statement.run()));
  }

  close() {
    this.database.close();
  }
}

const databases: SqliteD1[] = [];

function environment() {
  const database = new SqliteD1();
  databases.push(database);
  database.database.exec(`
    INSERT INTO cf_account_cutover
      (uid, schema_version, state, account_generation, ui_generation, api_generation,
       stranded_new_data, offline_queue_instruction, checkpoint_phase,
       destination_backend_bound, updated_at)
    VALUES ('chat-user', 1, 'new', 1, 1, 1, 0, 'none', 'completed', 1, 1);
    INSERT INTO cf_goals
      (uid, id, title, desired_outcome, status, source, created_at, updated_at)
    VALUES ('chat-user', 'goal-1', 'Ship Cloudflare', 'A durable edge deployment', 'focused', 'user', 1, 1);
    INSERT INTO cf_action_items
      (uid, id, description, status, completed, created_at, updated_at)
    VALUES ('chat-user', 'task-1', 'Deploy the Worker', 'active', 0, 1, 1);
  `);
  const env = {
    APP_DB: database,
    AI: { run: vi.fn(async () => ({ response: "Cloudflare says hello." })) },
    WORKERS_AI_CHAT_MODEL: "@cf/meta/test-chat",
    FREE_CHAT_QUESTIONS_PER_MONTH: "30",
    INTERNAL_ASSERTION_SECRET: "test-secret",
  } as never as JobsEnv;
  return { database, env };
}

function appFor(env: JobsEnv) {
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerChatCompatibilityRoutes(app, async () => ({ uid: "chat-user", authority: "better-auth" }));
  return app;
}

afterEach(() => {
  vi.restoreAllMocks();
  while (databases.length) databases.pop()?.close();
});

describe("Cloudflare exact Chat compatibility owner", () => {
  it("materializes a D1 daily opener and consumes a receipt exactly once", async () => {
    const fixture = environment();
    const app = appFor(fixture.env);
    const body = {
      source_surface: "main_chat",
      control_generation: 1,
      owner_fence: "chat-user",
      window_foreground: true,
      initial_page_loaded: true,
      receipts: [],
      cold_start_sequence_terminal_receipts: [],
    };
    const first = await app.request("https://jobs.test/v2/chat/materialize-prompts", { method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json" } }, fixture.env);
    expect(first.status).toBe(200);
    const payload = (await first.json()) as { intents: Array<Record<string, unknown>> };
    expect(payload.intents).toHaveLength(1);
    expect(payload.intents[0]).toMatchObject({ source: "daily_opener", delivery_state: "ready" });
    const intentId = payload.intents[0].intent_id as string;
    const receipt = await app.request("https://jobs.test/v1/chat/materialize-prompts", { method: "POST", body: JSON.stringify({ ...body, receipts: [{ intent_id: intentId, receipt_id: "receipt-1" }] }), headers: { "content-type": "application/json" } }, fixture.env);
    expect(receipt.status).toBe(200);
    const replay = await app.request("https://jobs.test/v1/chat/materialize-prompts", { method: "POST", body: JSON.stringify({ ...body, receipts: [{ intent_id: intentId, receipt_id: "receipt-1" }] }), headers: { "content-type": "application/json" } }, fixture.env);
    expect(replay.status).toBe(200);
    expect(fixture.database.database.prepare("SELECT delivery_state, materialization_receipt_id FROM cf_chat_first_intents WHERE uid = 'chat-user'").get()).toEqual({ delivery_state: "delivered", materialization_receipt_id: "receipt-1" });
  });

  it("runs Workers AI, persists both messages, emits OpenAI shape, and is idempotent", async () => {
    const fixture = environment();
    const app = appFor(fixture.env);
    const request = { model: "workers-ai", messages: [{ role: "user", content: "Hello" }] };
    const first = await app.request("https://jobs.test/v2/chat/completions", { method: "POST", headers: { "content-type": "application/json", "idempotency-key": "chat-idempotency-1" }, body: JSON.stringify(request) }, fixture.env);
    expect(first.status).toBe(200);
    await expect(first.json()).resolves.toMatchObject({ object: "chat.completion", model: "workers-ai", choices: [{ message: { content: "Cloudflare says hello." } }] });
    const second = await app.request("https://jobs.test/v2/chat/completions", { method: "POST", headers: { "content-type": "application/json", "idempotency-key": "chat-idempotency-1" }, body: JSON.stringify(request) }, fixture.env);
    expect(second.status).toBe(200);
    expect(fixture.env.AI.run).toHaveBeenCalledTimes(1);
    expect(fixture.database.database.prepare("SELECT COUNT(*) AS count FROM cf_chat_messages WHERE uid = 'chat-user'").get()).toEqual({ count: 2 });
  });

  it("rejects unsupported provider features before Workers AI and respects the account fence", async () => {
    const fixture = environment();
    const app = appFor(fixture.env);
    const unsupported = await app.request("https://jobs.test/v2/chat/completions", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ messages: [{ role: "user", content: "Hi" }], tools: [] }) }, fixture.env);
    expect(unsupported.status).toBe(409);
    expect(fixture.env.AI.run).not.toHaveBeenCalled();
    const stale = await app.request("https://jobs.test/v2/chat/materialize-prompts", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ source_surface: "main_chat", control_generation: 9, owner_fence: "chat-user", window_foreground: true, initial_page_loaded: true }) }, fixture.env);
    expect(stale.status).toBe(409);
  });

  it("fails closed when an OpenAI provider is requested without credentials", async () => {
    const fixture = environment();
    const app = appFor(fixture.env);
    const response = await app.request(
      "https://jobs.test/v2/chat/completions",
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          model: "gpt-4o-mini",
          messages: [{ role: "user", content: "No provider" }],
        }),
      },
      fixture.env,
    );
    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toMatchObject({
      error: "provider_not_configured",
    });
  });

  it("uses the validated OpenAI BYOK header without requiring a server key", async () => {
    const fixture = environment();
    const provider = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(new URL(String(input)).pathname).toBe("/v1/chat/completions");
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer caller-key");
      return Response.json({
        model: "gpt-4o-mini",
        choices: [{ message: { content: "BYOK response" } }],
        usage: { prompt_tokens: 2, completion_tokens: 3 },
      });
    });
    vi.stubGlobal("fetch", provider);
    const app = new Hono<{ Bindings: JobsEnv }>();
    registerChatCompatibilityRoutes(app, async () => ({
      uid: "chat-user",
      authority: "better-auth",
      byokActive: true,
    }));
    const response = await app.request(
      "https://jobs.test/v2/chat/completions",
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "idempotency-key": "byok-idempotency",
          "x-byok-openai": "caller-key",
        },
        body: JSON.stringify({
          model: "openai-byok",
          messages: [{ role: "user", content: "Use BYOK" }],
        }),
      },
      fixture.env,
    );
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({
      choices: [{ message: { content: "BYOK response" } }],
    });
    expect(provider).toHaveBeenCalledTimes(1);
  });
});
