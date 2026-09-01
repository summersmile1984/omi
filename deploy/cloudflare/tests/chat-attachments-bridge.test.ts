import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobsEnv } from "../workers/jobs/env";
import { registerChatAssistantRoutes } from "../workers/jobs/chat-assistant-provider";

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
        (this.database.prepare(sql).get(...(args as never[])) as
          T | undefined) ?? null,
      all: async <T>() => ({
        results: this.database.prepare(sql).all(...(args as never[])) as T[],
      }),
      run: async () => {
        const result = this.database.prepare(sql).run(...(args as never[]));
        return { meta: { changes: Number(result.changes) } };
      },
    });
    return build();
  }

  async batch(statements: Array<{ run: () => Promise<unknown> }>) {
    const results = [];
    for (const statement of statements) results.push(await statement.run());
    return results;
  }

  close() {
    this.database.close();
  }
}

const databases: SqliteD1[] = [];

function environment(withSession = true) {
  const database = new SqliteD1();
  databases.push(database);
  database.database.exec(`
    ${withSession ? "INSERT INTO cf_chat_sessions (uid, id, title, created_at, updated_at) VALUES ('attachment-user', 'session-1', 'Test', 1, 1);" : ""}
    INSERT INTO cf_app_catalog
      (id, approved, status, disabled, is_popular, installs, rating_count, data_json, updated_at)
    VALUES
      ('app-1', 1, 'approved', 0, 0, 0, 0,
       '{"id":"app-1","name":"Reader","chat_prompt":"Use concise citations."}', 1);
    INSERT INTO cf_chat_files
      (uid, file_id, request_fingerprint, provider, provider_file_id, name,
       mime_type, size, checksum_sha256, storage_key, status,
       thumbnail_status, created_at, updated_at)
    VALUES
      ('attachment-user', 'file-1', '${"a".repeat(64)}', 'openai', 'file-1-provider',
       'notes.txt', 'text/plain', 4, '${"b".repeat(64)}',
       'attachment-user/file-1', 'ready', 'not_applicable', 1, 1);
    ${withSession ? "INSERT INTO cf_chat_session_files (uid, session_id, file_id, attached_at) VALUES ('attachment-user', 'session-1', 'file-1', 1);" : ""}
  `);
  const queue = { send: vi.fn(async () => undefined) };
  const env = {
    APP_DB: database,
    JOBS: queue,
    CHAT_ASSISTANT_PROVIDER_STAGING_ENABLED: "true",
    CHAT_FILES_WORKERS_AI_ENABLED: "false",
    OPENAI_API_KEY: "test-openai-key",
    OPENAI_ASSISTANT_ID: "asst-test-1",
  } as never as JobsEnv;
  return { database, env, queue };
}

function testApp(env: JobsEnv) {
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerChatAssistantRoutes(app, async () => ({
    uid: "attachment-user",
    authority: "better-auth",
  }));
  return app;
}

function provider(options: { completed?: boolean } = {}) {
  let messageNumber = 0;
  let runNumber = 0;
  return vi.fn(async (input: string | URL) => {
    const url = String(input);
    if (url.endsWith("/threads")) return Response.json({ id: "thread-1" });
    if (url.includes("/messages?limit=")) {
      return Response.json({
        data: [
          {
            role: "assistant",
            content: [{ type: "text", text: { value: "The file says hello." } }],
          },
        ],
      });
    }
    if (url.match(/\/runs\/run-[0-9]+$/)) {
      const id = url.slice(url.lastIndexOf("/") + 1);
      return Response.json({ id, status: options.completed ? "completed" : "queued" });
    }
    if (url.endsWith("/messages")) return Response.json({ id: `msg-${++messageNumber}` });
    return Response.json({ id: `run-${++runNumber}`, status: "queued" });
  });
}

function request(
  app: Hono<{ Bindings: JobsEnv }>,
  env: JobsEnv,
  payload: Record<string, unknown>,
  idempotencyKey = "attachment-request-1",
  query = "",
) {
  return app.request(
    `https://jobs.test/v2/cf/messages/attachments${query ? `?${query}` : ""}`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "idempotency-key": idempotencyKey,
      },
      body: JSON.stringify(payload),
    },
    env,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  while (databases.length) databases.pop()?.close();
});

describe("Cloudflare /v2/messages attachment bridge", () => {
  it("reuses the Assistant projection and returns an explicit 202 polling contract", async () => {
    const fixture = environment();
    const app = testApp(fixture.env);
    const calls = provider();
    vi.stubGlobal("fetch", calls);

    const response = await request(app, fixture.env, {
      text: "Summarize this file",
      file_ids: ["file-1"],
      session_id: "session-1",
    });
    expect(response.status).toBe(202);
    expect(response.headers.get("x-omi-chat-contract")).toBe(
      "cloudflare-assistants-v1",
    );
    expect(response.headers.get("x-omi-chat-stream")).toBe("poll");
    expect(response.headers.get("location")).toMatch(
      /\/v2\/cf\/chat-sessions\/session-1\/assistant-runs\/[^/]+$/,
    );
    await expect(response.json()).resolves.toMatchObject({
      created: true,
      session_id: "session-1",
      status: "queued",
      message_projection: {
        human_status: "pending",
        assistant_status: "pending",
      },
    });
    expect(calls).toHaveBeenCalledTimes(3);
    expect(fixture.queue.send).toHaveBeenCalledWith(
      expect.objectContaining({
        uid: "attachment-user",
        kind: "chat_assistant_poll",
        payload: expect.objectContaining({ sessionId: "session-1" }),
      }),
    );
    expect(
      fixture.database.database
        .prepare(
          "SELECT file_ids_json FROM cf_chat_assistant_message_projections",
        )
        .get(),
    ).toEqual({ file_ids_json: '["file-1"]' });
  });

  it("resolves a default user session, supports idempotent retry, and binds all state to uid", async () => {
    const fixture = environment();
    const app = testApp(fixture.env);
    const calls = provider();
    vi.stubGlobal("fetch", calls);

    const payload = { text: "Describe this", file_ids: ["file-1"] };
    const first = await request(app, fixture.env, payload, "same-request");
    expect(first.status).toBe(202);
    const firstPayload = (await first.json()) as {
      session_id: string;
      run_id: string;
    };
    expect(firstPayload.session_id).toBe("session-1");
    const second = await request(app, fixture.env, payload, "same-request");
    expect(second.status).toBe(200);
    await expect(second.json()).resolves.toMatchObject({
      created: false,
      run_id: firstPayload.run_id,
      session_id: firstPayload.session_id,
    });
    expect(calls).toHaveBeenCalledTimes(3);
    expect(fixture.queue.send).toHaveBeenCalledTimes(1);
  });

  it("keeps app-scoped attachments and page context in an app session", async () => {
    const fixture = environment();
    const app = testApp(fixture.env);
    const calls = provider();
    vi.stubGlobal("fetch", calls);

    const response = await request(
      app,
      fixture.env,
      {
        text: "Summarize this for the reader app",
        file_ids: ["file-1"],
        context: {
          type: "conversation",
          id: "conversation-1",
          title: "Meeting notes",
          summary: "A bounded page summary",
        },
      },
      "app-scoped-request",
      "app_id=app-1",
    );
    expect(response.status).toBe(202);
    const payload = (await response.json()) as { session_id: string };
    expect(payload.session_id).not.toBe("session-1");
    expect(
      fixture.database.database
        .prepare("SELECT app_id FROM cf_chat_sessions WHERE id = ?")
        .get(payload.session_id),
    ).toEqual({ app_id: "app-1" });
    expect(
      fixture.database.database
        .prepare("SELECT json_extract(message_json, '$.app_id') AS app_id FROM cf_chat_messages WHERE id LIKE 'chat-human-%'")
        .get(),
    ).toEqual({ app_id: "app-1" });
    expect(
      fixture.database.database
        .prepare("SELECT file_id FROM cf_chat_session_files WHERE session_id = ?")
        .get(payload.session_id),
    ).toEqual({ file_id: "file-1" });

    const missing = await request(
      app,
      fixture.env,
      { text: "Question", file_ids: ["file-1"] },
      "missing-app-request",
      "app_id=missing-app",
    );
    expect(missing.status).toBe(404);
    expect(calls).toHaveBeenCalledTimes(3);
  });

  it("fails closed for unbounded, duplicate, unsupported, and unavailable attachments", async () => {
    const fixture = environment();
    const app = testApp(fixture.env);
    const calls = provider();
    vi.stubGlobal("fetch", calls);

    const duplicate = await request(app, fixture.env, {
      text: "Question",
      file_ids: ["file-1", "file-1"],
    });
    expect(duplicate.status).toBe(400);
    await expect(duplicate.json()).resolves.toMatchObject({
      error: "provider_rejected",
    });

    const unsupported = await request(app, fixture.env, {
      text: "Question",
      file_ids: ["file-1"],
      context: { screen: "secret" },
    });
    expect(unsupported.status).toBe(400);

    const unready = await request(app, fixture.env, {
      text: "Question",
      file_ids: ["missing-file"],
    });
    expect(unready.status).toBe(400);

    const oversized = await request(app, fixture.env, {
      text: "x".repeat(140_000),
      file_ids: ["file-1"],
    });
    expect(oversized.status).toBe(413);
    expect(calls).not.toHaveBeenCalled();
    expect(fixture.queue.send).not.toHaveBeenCalled();
  });

  it("emits the guarded legacy messages SSE envelope only after projection completion", async () => {
    const fixture = environment();
    fixture.env.CHAT_ATTACHMENT_ENVELOPE_STAGING_ENABLED = "true";
    const app = testApp(fixture.env);
    vi.stubGlobal("fetch", provider({ completed: true }));

    const response = await request(
      app,
      fixture.env,
      { text: "Summarize this file", file_ids: ["file-1"], session_id: "session-1" },
      "messages-envelope",
      "envelope=messages",
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("text/event-stream");
    expect(response.headers.get("x-omi-chat-envelope")).toBe("messages-v1");
    const body = await response.text();
    expect(body).toContain("data: The file says hello.\n\n");
    const encoded = body.match(/done: ([A-Za-z0-9+/=]+)\n\n/)?.[1];
    expect(encoded).toBeTruthy();
    const message = JSON.parse(Buffer.from(encoded as string, "base64").toString("utf8"));
    expect(message).toMatchObject({
      sender: "ai",
      text: "The file says hello.",
      ask_for_nps: false,
      chat_session_id: "session-1",
    });
  });

  it("emits guarded OpenAI sync and SSE envelopes for simple textual messages", async () => {
    const fixture = environment();
    fixture.env.CHAT_ATTACHMENT_ENVELOPE_STAGING_ENABLED = "true";
    const app = testApp(fixture.env);
    vi.stubGlobal("fetch", provider({ completed: true }));

    const sync = await request(
      app,
      fixture.env,
      {
        model: "gpt-4o-mini",
        messages: [{ role: "user", content: "Describe this file" }],
        file_ids: ["file-1"],
        stream: false,
      },
      "openai-sync-envelope",
      "envelope=openai",
    );
    expect(sync.status).toBe(200);
    const syncPayload = await sync.json();
    expect(syncPayload).toMatchObject({
      object: "chat.completion",
      model: "gpt-4o-mini",
      choices: [
        {
          message: { role: "assistant", content: "The file says hello." },
          finish_reason: "stop",
        },
      ],
    });
    expect(syncPayload).not.toHaveProperty("usage");

    const stream = await request(
      app,
      fixture.env,
      {
        model: "gpt-4o-mini",
        messages: [{ role: "user", content: "Describe this file" }],
        file_ids: ["file-1"],
        stream: true,
      },
      "openai-stream-envelope",
      "envelope=openai",
    );
    expect(stream.status).toBe(200);
    expect(stream.headers.get("content-type")).toContain("text/event-stream");
    const streamBody = await stream.text();
    expect(streamBody).toContain('"object":"chat.completion.chunk"');
    expect(streamBody).toContain('"content":"The file says hello."');
    expect(streamBody).toContain("data: [DONE]\n\n");
  });
});
