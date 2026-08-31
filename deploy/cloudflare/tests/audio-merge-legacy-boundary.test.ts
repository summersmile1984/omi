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
    const directory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/app",
    );
    for (const filename of readdirSync(directory)
      .filter((value) => value.endsWith(".sql"))
      .sort())
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
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
        const result = statement.run(...(args as never[]));
        return { meta: { changes: Number(result.changes) } };
      },
    });
    return build();
  }

  close() {
    this.database.close();
  }
}

class MemoryBucket {
  readonly objects = new Map<string, Uint8Array>();
  failPutCount = 0;

  async get(key: string, options?: { range?: { offset: number; length: number } }) {
    const stored = this.objects.get(key);
    if (!stored) return null;
    const range = options?.range;
    const bytes = range
      ? stored.slice(range.offset, range.offset + range.length)
      : stored.slice();
    return {
      body: new Response(bytes).body,
      arrayBuffer: async () => bytes.buffer,
    };
  }

  async head(key: string) {
    const stored = this.objects.get(key);
    return stored ? { key, size: stored.byteLength } : null;
  }

  async list(options?: { prefix?: string; cursor?: string; limit?: number }) {
    const prefix = options?.prefix || "";
    const objects = [...this.objects.entries()]
      .filter(([key]) => key.startsWith(prefix))
      .map(([key, bytes]) => ({ key, size: bytes.byteLength }));
    return { objects, truncated: false };
  }

  async put(key: string, body: BodyInit | ReadableStream<Uint8Array>) {
    if (this.failPutCount > 0) {
      this.failPutCount -= 1;
      throw new Error("transient R2 write failure");
    }
    this.objects.set(key, new Uint8Array(await new Response(body).arrayBuffer()));
  }

  async delete(key: string) {
    this.objects.delete(key);
  }
}

const databases: SqliteD1[] = [];

function environment() {
  const database = new SqliteD1();
  databases.push(database);
  const assets = new MemoryBucket();
  const sent: JobMessage[] = [];
  const env = {
    APP_DB: database,
    ASSETS: assets,
    JOBS: { send: vi.fn(async (message: JobMessage) => sent.push(message)) },
    INTERNAL_ASSERTION_SECRET: "audio-merge-legacy-secret",
  } as unknown as JobsEnv;
  database.database
    .prepare(
      "INSERT INTO cf_user_privacy_settings (uid, store_recording_permission, private_cloud_sync_enabled, created_at, updated_at) VALUES ('audio-user', 1, 1, 1000, 1000)",
    )
    .run();
  database.database
    .prepare(
      "INSERT INTO cf_conversations (uid, id, created_at, updated_at, audio_files_json) VALUES ('audio-user', 'conversation-1', 1000, 1000, ?)",
    )
    .run(JSON.stringify([{ id: "audio-1", provider: "gcs", chunk_timestamps: [1000] }]));
  return { database, env, assets, sent };
}

async function signedHeaders(method: "GET" | "POST", pathname: string) {
  const signed = await createSignedAuthContext(
    { uid: "audio-user", authority: "better-auth", requestId: "audio-merge-legacy-test" },
    "jobs",
    method,
    pathname,
    "audio-merge-legacy-secret",
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
});

describe("Cloudflare legacy MP3 audio merge adapter", () => {
  it("accepts schema-v1 payloads, emits one MP3 job, and is idempotent", async () => {
    const { env, assets, sent } = environment();
    await assets.put("chunks/audio-user/conversation-1/1000.bin", new Uint8Array(32_000));
    const request = async () =>
      jobs.fetch(
        new Request("https://jobs.test/v2/cf/audio-merge-jobs/legacy/run", {
          method: "POST",
          headers: {
            ...(await signedHeaders("POST", "/v2/cf/audio-merge-jobs/legacy/run")),
            "content-type": "application/json",
          },
          body: JSON.stringify({
            uid: "audio-user",
            conversation_id: "conversation-1",
            audio_file_id: "audio-1",
            timestamps: [1000],
          }),
        }),
        env,
      );
    const first = await request();
    expect(first.status).toBe(202);
    const admitted = (await first.json()) as { job_id: string; status: string };
    expect(admitted).toMatchObject({
      status: "queued",
      job_id: expect.stringMatching(/^audio-merge-legacy-[a-f0-9]{48}$/),
    });
    expect(sent).toHaveLength(1);
    const duplicate = await request();
    expect(duplicate.status).toBe(200);
    expect((await duplicate.json()) as { job_id: string }).toMatchObject({
      job_id: admitted.job_id,
      status: "queued",
    });
    expect(sent).toHaveLength(1);

    const message = queueMessage(sent[0]);
    await jobs.queue({ messages: [message] } as never, env);
    expect(message.ack).toHaveBeenCalledOnce();
    expect(message.retry).not.toHaveBeenCalled();
    expect(await assets.head("playback/audio-user/conversation-1/audio-1.mp3")).toBeTruthy();
    const stored = assets.objects.get("playback/audio-user/conversation-1/audio-1.mp3")!;
    expect(stored[0]).toBe(0xff);
    expect(stored[1] & 0xe0).toBe(0xe0);
    expect(
      await env.APP_DB.prepare(
        "SELECT status, attempts, result_json FROM cf_audio_merge_legacy_jobs WHERE job_id = ?",
      )
        .bind(admitted.job_id)
        .first(),
    ).toMatchObject({ status: "completed", attempts: 1 });
  });

  it("builds the schema-v2 dense MP3 and rejects unauthorized/MP3-incompatible output", async () => {
    const { env, assets, sent } = environment();
    await assets.put("chunks/audio-user/conversation-1/1000.bin", new Uint8Array(32_000));
    const headers = await signedHeaders("POST", "/v2/cf/audio-merge-jobs/legacy/run");
    const unauthorized = await jobs.fetch(
      new Request("https://jobs.test/v2/cf/audio-merge-jobs/legacy/run", {
        method: "POST",
        body: JSON.stringify({ schema_version: 2, conversation_id: "conversation-1" }),
      }),
      env,
    );
    expect(unauthorized.status).toBe(401);
    const unsupported = await jobs.fetch(
      new Request("https://jobs.test/v2/cf/audio-merge-jobs/legacy/run", {
        method: "POST",
        headers: { ...headers, "content-type": "application/json" },
        body: JSON.stringify({ schema_version: 2, conversation_id: "conversation-1", output_format: "wav" }),
      }),
      env,
    );
    expect(unsupported.status).toBe(422);
    expect(sent).toHaveLength(0);
    const admitted = await jobs.fetch(
      new Request("https://jobs.test/v2/cf/audio-merge-jobs/legacy/run", {
        method: "POST",
        headers: { ...headers, "content-type": "application/json" },
        body: JSON.stringify({ schema_version: 2, conversation_id: "conversation-1" }),
      }),
      env,
    );
    expect(admitted.status).toBe(202);
    const job = (await admitted.json()) as { job_id: string };
    const message = queueMessage(sent[0]);
    await jobs.queue({ messages: [message] } as never, env);
    expect(message.ack).toHaveBeenCalledOnce();
    expect(await assets.head("playback/audio-user/conversation-1/conversation.mp3")).toBeTruthy();
    expect(
      await env.APP_DB.prepare("SELECT status FROM cf_audio_merge_legacy_jobs WHERE job_id = ?")
        .bind(job.job_id)
        .first(),
    ).toMatchObject({ status: "completed" });
    const conversation = await env.APP_DB.prepare(
      "SELECT conversation_audio_json FROM cf_conversations WHERE uid = 'audio-user' AND id = 'conversation-1'",
    ).first<{ conversation_audio_json: string }>();
    expect(JSON.parse(conversation!.conversation_audio_json)).toMatchObject({
      content_type: "audio/mpeg",
      storage_key: "playback/audio-user/conversation-1/conversation.mp3",
    });
  });

  it("terminalizes missing chunks and never exposes another uid's job", async () => {
    const { env, sent } = environment();
    const admitted = await jobs.fetch(
      new Request("https://jobs.test/v2/cf/audio-merge-jobs/legacy/run", {
        method: "POST",
        headers: {
          ...(await signedHeaders("POST", "/v2/cf/audio-merge-jobs/legacy/run")),
          "content-type": "application/json",
        },
        body: JSON.stringify({ conversation_id: "conversation-1", audio_file_id: "audio-1", timestamps: [1000] }),
      }),
      env,
    );
    const job = (await admitted.json()) as { job_id: string };
    const message = queueMessage(sent[0]);
    await jobs.queue({ messages: [message] } as never, env);
    expect(message.ack).toHaveBeenCalledOnce();
    expect(message.retry).not.toHaveBeenCalled();
    expect(await env.APP_DB.prepare("SELECT status, last_error FROM cf_audio_merge_legacy_jobs WHERE job_id = ?")
      .bind(job.job_id).first()).toMatchObject({ status: "failed", last_error: "legacy audio chunks are not available in R2" });
  });

  it("resets a transient artifact failure for Queue retry and keeps deletion fenced", async () => {
    const { env, assets, sent, database } = environment();
    await assets.put("chunks/audio-user/conversation-1/1000.bin", new Uint8Array(32_000));
    const admitted = await jobs.fetch(
      new Request("https://jobs.test/v2/cf/audio-merge-jobs/legacy/run", {
        method: "POST",
        headers: {
          ...(await signedHeaders("POST", "/v2/cf/audio-merge-jobs/legacy/run")),
          "content-type": "application/json",
        },
        body: JSON.stringify({ conversation_id: "conversation-1", audio_file_id: "audio-1", timestamps: [1000] }),
      }),
      env,
    );
    const job = (await admitted.json()) as { job_id: string };
    assets.failPutCount = 1;
    const first = queueMessage(sent[0]);
    await jobs.queue({ messages: [first] } as never, env);
    expect(first.retry).toHaveBeenCalledOnce();
    expect(first.ack).not.toHaveBeenCalled();
    expect(await env.APP_DB.prepare("SELECT status, last_error FROM cf_audio_merge_legacy_jobs WHERE job_id = ?")
      .bind(job.job_id).first()).toMatchObject({ status: "queued", last_error: "audio merge processor unavailable" });
    const deletionNow = Math.floor(Date.now() / 1000);
    database.database.prepare(
      "INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES (?, ?, 'pending', 'quiescing', ?, ?, ?)",
    ).run("audio-user", "delete-audio-user", deletionNow, deletionNow, deletionNow);
    const fenced = await jobs.fetch(
      new Request("https://jobs.test/v2/cf/audio-merge-jobs/legacy/run", {
        method: "POST",
        headers: {
          ...(await signedHeaders("POST", "/v2/cf/audio-merge-jobs/legacy/run")),
          "content-type": "application/json",
        },
        body: JSON.stringify({ conversation_id: "conversation-1", audio_file_id: "audio-1", timestamps: [1000, 1001] }),
      }),
      env,
    );
    expect(fenced.status).toBe(503);
    expect(await env.APP_DB.prepare("SELECT COUNT(*) AS count FROM cf_audio_merge_legacy_jobs WHERE uid = 'audio-user'").first())
      .toMatchObject({ count: 1 });
  });
});
