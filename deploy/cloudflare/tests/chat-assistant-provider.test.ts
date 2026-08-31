import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ChatAssistantProviderError,
  createAssistantRun,
  deleteAssistantSession,
  pollAssistantRun,
  processChatAssistantRunMessage,
} from "../workers/jobs/chat-assistant-provider";

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    const directory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/app",
    );
    this.database.exec("PRAGMA foreign_keys = ON");
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
          | T
          | undefined) ?? null,
      all: async <T>() => ({
        results: this.database.prepare(sql).all(...(args as never[])) as T[],
      }),
      run: async () => ({
        meta: {
          changes: Number(
            this.database.prepare(sql).run(...(args as never[])).changes,
          ),
        },
      }),
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

function environment() {
  const database = new SqliteD1();
  databases.push(database);
  database.database.exec(`
    INSERT INTO cf_chat_sessions (uid, id, title, created_at, updated_at)
    VALUES ('assistant-user', 'session-1', 'Test', 1, 1);
    INSERT INTO cf_chat_files
      (uid, file_id, request_fingerprint, provider, provider_file_id, name,
       mime_type, size, checksum_sha256, storage_key, status,
       thumbnail_status, created_at, updated_at)
    VALUES
      ('assistant-user', 'file-text', '${"a".repeat(64)}', 'openai', 'file-text-1',
       'notes.txt', 'text/plain', 4, '${"b".repeat(64)}',
       'assistant-user/file-text', 'ready', 'not_applicable', 1, 1),
      ('assistant-user', 'file-image', '${"c".repeat(64)}', 'openai', 'file-image-1',
       'photo.png', 'image/png', 4, '${"d".repeat(64)}',
       'assistant-user/file-image', 'ready', 'ready', 1, 1);
    INSERT INTO cf_chat_session_files (uid, session_id, file_id, attached_at)
    VALUES
      ('assistant-user', 'session-1', 'file-text', 1),
      ('assistant-user', 'session-1', 'file-image', 1);
  `);
  return {
    database,
    env: {
      APP_DB: database,
      OPENAI_API_KEY: "test-openai-key",
      OPENAI_ASSISTANT_ID: "asst-test-1",
    } as never,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  while (databases.length) databases.pop()!.close();
});

describe("Cloudflare OpenAI Assistants continuity adapter", () => {
  it("creates an uid-scoped thread/message/run and maps attachments by MIME", async () => {
    const { database, env } = environment();
    const calls: Array<{ url: string; body: Record<string, unknown> | null }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL, init?: RequestInit) => {
        const url = String(input);
        const body = init?.body ? JSON.parse(String(init.body)) : null;
        calls.push({ url, body });
        if (url.endsWith("/threads")) return Response.json({ id: "thread-1" });
        if (url.endsWith("/messages")) return Response.json({ id: "msg-1" });
        if (url.endsWith("/runs"))
          return Response.json({ id: "run-1", status: "queued" });
        throw new Error(`unexpected provider call: ${url}`);
      }),
    );

    const result = await createAssistantRun(
      env,
      "assistant-user",
      "session-1",
      "request-1",
      "Summarize these files",
      ["file-text", "file-image"],
      100,
    );
    expect(result).toMatchObject({ created: true, status: "queued" });
    expect(calls).toHaveLength(3);
    expect(calls[1].body).toMatchObject({
      role: "user",
      content: [
        { type: "text", text: "Summarize these files" },
        { type: "image_file", image_file: { file_id: "file-image-1", detail: "auto" } },
      ],
      attachments: [
        { file_id: "file-text-1", tools: [{ type: "file_search" }] },
      ],
    });
    expect(
      database.database
        .prepare(
          "SELECT thread_id, provider_message_id, provider_run_id, attempts FROM cf_chat_assistant_sessions s JOIN cf_chat_assistant_runs r ON r.uid = s.uid AND r.session_id = s.session_id",
        )
        .get(),
    ).toMatchObject({
      thread_id: "thread-1",
      provider_message_id: "msg-1",
      provider_run_id: "run-1",
      attempts: 1,
    });
    expect(
      database.database
        .prepare(
          "SELECT human_status, assistant_status, request_text, file_ids_json FROM cf_chat_assistant_message_projections",
        )
        .get(),
    ).toMatchObject({
      human_status: "ready",
      assistant_status: "pending",
      request_text: "Summarize these files",
      file_ids_json: '["file-text","file-image"]',
    });
    const projectedHuman = database.database
      .prepare("SELECT message_json FROM cf_chat_messages WHERE uid = 'assistant-user'")
      .get() as { message_json: string };
    expect(JSON.parse(projectedHuman.message_json)).toMatchObject({
      sender: "human",
      text: "Summarize these files",
      files_id: ["file-text", "file-image"],
      files: [
        { id: "file-text", openai_file_id: "file-text-1" },
        { id: "file-image", openai_file_id: "file-image-1" },
      ],
      chat_session_id: "session-1",
      message_source: "cloudflare_assistants",
    });
  });

  it("returns exact idempotent retries without re-calling the provider and rejects payload reuse", async () => {
    const { env } = environment();
    const provider = vi.fn(async (input: string | URL) => {
      const url = String(input);
      if (url.endsWith("/threads")) return Response.json({ id: "thread-1" });
      if (url.endsWith("/messages")) return Response.json({ id: "msg-1" });
      return Response.json({ id: "run-1", status: "queued" });
    });
    vi.stubGlobal("fetch", provider);

    const first = await createAssistantRun(
      env,
      "assistant-user",
      "session-1",
      "request-1",
      "Summarize",
      ["file-text"],
      100,
    );
    const second = await createAssistantRun(
      env,
      "assistant-user",
      "session-1",
      "request-1",
      "Summarize",
      ["file-text"],
      101,
    );
    expect(first).toMatchObject({ created: true });
    expect(second).toMatchObject({ created: false, run_id: first.run_id });
    expect(provider).toHaveBeenCalledTimes(3);
    await expect(
      createAssistantRun(
        env,
        "assistant-user",
        "session-1",
        "request-1",
        "Different question",
        ["file-text"],
        102,
      ),
    ).rejects.toMatchObject({ code: "provider_rejected" });
    expect(provider).toHaveBeenCalledTimes(3);
  });

  it("admits a text-only run without constructing an empty attachment query", async () => {
    const { env } = environment();
    const calls: Array<{ url: string; body: Record<string, unknown> | null }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL, init?: RequestInit) => {
        const url = String(input);
        const body = init?.body ? JSON.parse(String(init.body)) : null;
        calls.push({ url, body });
        if (url.endsWith("/threads")) return Response.json({ id: "thread-1" });
        if (url.endsWith("/messages")) return Response.json({ id: "msg-1" });
        return Response.json({ id: "run-1", status: "queued" });
      }),
    );

    const result = await createAssistantRun(
      env,
      "assistant-user",
      "session-1",
      "request-text-only",
      "Just answer this question",
      [],
      100,
    );
    expect(result).toMatchObject({ created: true, status: "queued" });
    expect(calls).toHaveLength(3);
    expect(calls[1].body).toEqual({
      role: "user",
      content: [{ type: "text", text: "Just answer this question" }],
    });
  });

  it("polls a completed run, keeps session ids isolated, and deletes provider/D1 state", async () => {
    const { database, env } = environment();
    const provider = vi.fn(async (input: string | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "DELETE") return Response.json({ deleted: true });
      if (url.endsWith("/threads")) return Response.json({ id: "thread-1" });
      if (url.endsWith("/messages")) return Response.json({ id: "msg-1" });
      if (url.includes("/runs/run-1"))
        return Response.json({ id: "run-1", status: "completed" });
      if (url.includes("/messages?limit=20"))
        return Response.json({
          data: [{ role: "assistant", content: [{ type: "text", text: { value: "answer" } }] }],
        });
      return Response.json({ id: "run-1", status: "queued" });
    });
    vi.stubGlobal("fetch", provider);
    const created = await createAssistantRun(
      env,
      "assistant-user",
      "session-1",
      "request-1",
      "Question",
      ["file-text"],
      100,
    );
    await expect(
      pollAssistantRun(env, "assistant-user", "other-session", String(created.run_id), 101),
    ).rejects.toMatchObject({ code: "provider_rejected" });
    let acknowledged = false;
    await processChatAssistantRunMessage(
      {
        body: {
          jobId: String(created.run_id),
          uid: "assistant-user",
          kind: "chat_assistant_poll",
          payload: { sessionId: "session-1", runId: String(created.run_id) },
        },
        attempts: 1,
        ack: () => {
          acknowledged = true;
        },
        retry: () => {
          throw new Error("completed provider run must be acknowledged");
        },
      } as never,
      env,
    );
    expect(acknowledged).toBe(true);
    const completed = await pollAssistantRun(
      env,
      "assistant-user",
      "session-1",
      String(created.run_id),
      101,
    );
    expect(completed).toMatchObject({ status: "completed", result: { text: "answer" } });
    expect(
      database.database
        .prepare(
          "SELECT human_status, assistant_status FROM cf_chat_assistant_message_projections WHERE uid = 'assistant-user' AND run_id = ?",
        )
        .get(String(created.run_id)),
    ).toEqual({ human_status: "ready", assistant_status: "ready" });
    const projectedMessages = database.database
      .prepare(
        "SELECT message_json FROM cf_chat_messages WHERE uid = 'assistant-user' ORDER BY created_at, id",
      )
      .all() as Array<{ message_json: string }>;
    expect(projectedMessages).toHaveLength(2);
    expect(JSON.parse(projectedMessages[1].message_json)).toMatchObject({
      sender: "ai",
      text: "answer",
      files_id: [],
      chat_session_id: "session-1",
      message_source: "cloudflare_assistants",
    });

    await deleteAssistantSession(env, "assistant-user", "session-1");
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_chat_assistant_sessions WHERE uid = 'assistant-user'")
        .get(),
    ).toMatchObject({ count: 0 });
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_chat_assistant_runs WHERE uid = 'assistant-user'")
        .get(),
    ).toMatchObject({ count: 0 });
    expect(provider).toHaveBeenCalledWith(
      "https://api.openai.com/v1/threads/thread-1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("retries transient provider failures but fails closed without provider or ready attachment state", async () => {
    const { env, database } = environment();
    let threadAttempts = 0;
    const provider = vi.fn(async (input: string | URL) => {
      const url = String(input);
      if (url.endsWith("/threads")) {
        threadAttempts += 1;
        if (threadAttempts === 1) return new Response("busy", { status: 503 });
        return Response.json({ id: "thread-1" });
      }
      if (url.endsWith("/messages")) return Response.json({ id: "msg-1" });
      return Response.json({ id: "run-1", status: "queued" });
    });
    vi.stubGlobal("fetch", provider);
    await expect(
      createAssistantRun(
        env,
        "assistant-user",
        "session-1",
        "request-1",
        "Question",
        ["file-text"],
        100,
      ),
    ).resolves.toMatchObject({ status: "queued" });
    expect(threadAttempts).toBe(2);

    const callsBeforeInvalid = provider.mock.calls.length;
    await expect(
      createAssistantRun(
        env,
        "assistant-user",
        "session-1",
        "request-2",
        "Question",
        ["not-linked"],
        100,
      ),
    ).rejects.toMatchObject({ code: "provider_rejected" });
    expect(provider).toHaveBeenCalledTimes(callsBeforeInvalid);

    database.database.exec(`
      INSERT INTO cf_account_deletion_intents
        (uid, job_id, status, phase, next_attempt_at, created_at, updated_at)
      VALUES ('fenced-user', 'delete-1', 'pending', 'quiescing', 1, 1, 1);
    `);
    await expect(
      createAssistantRun(
        env,
        "fenced-user",
        "session-1",
        "request-fenced",
        "Question",
        [],
        100,
      ),
    ).rejects.toMatchObject({ code: "provider_rejected" });
    expect(provider).toHaveBeenCalledTimes(callsBeforeInvalid);
  });
});
