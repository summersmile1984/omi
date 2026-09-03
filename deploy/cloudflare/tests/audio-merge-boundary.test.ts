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

  async get(
    key: string,
    options?: { range?: { offset: number; length: number } },
  ) {
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
    const bytes = new Uint8Array(await new Response(body).arrayBuffer());
    this.objects.set(key, bytes);
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
    INTERNAL_ASSERTION_SECRET: "audio-merge-secret",
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
    .run(
      JSON.stringify([
        { id: "audio-1", provider: "gcs", chunk_timestamps: [1000] },
      ]),
    );
  return { database, env, assets, sent };
}

async function signedHeaders(
  method: "GET" | "POST",
  pathname: string,
  uid = "audio-user",
) {
  const signed = await createSignedAuthContext(
    { uid, authority: "better-auth", requestId: "audio-merge-test" },
    "jobs",
    method,
    pathname,
    "audio-merge-secret",
  );
  return {
    "x-omi-auth-context": signed!.encoded,
    "x-omi-internal-signature": signed!.signature,
  };
}

function queueMessage(body: JobMessage, attempts = 1): Message<JobMessage> {
  return {
    body,
    attempts,
    ack: vi.fn(),
    retry: vi.fn(),
  } as unknown as Message<JobMessage>;
}

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
});

describe("Cloudflare audio merge staging boundary", () => {
  it("requires signed auth and rejects MP3 before queue admission", async () => {
    const { env, sent } = environment();
    const unauthorized = await jobs.fetch(
      new Request("https://jobs.test/v2/cf/audio-merge-jobs/run", {
        method: "POST",
        body: JSON.stringify({
          conversation_id: "conversation-1",
          output_format: "wav",
        }),
      }),
      env,
    );
    expect(unauthorized.status).toBe(401);

    const unsupported = await jobs.fetch(
      new Request("https://jobs.test/v2/cf/audio-merge-jobs/run", {
        method: "POST",
        headers: {
          ...(await signedHeaders("POST", "/v2/cf/audio-merge-jobs/run")),
          "content-type": "application/json",
        },
        body: JSON.stringify({
          conversation_id: "conversation-1",
          output_format: "mp3",
        }),
      }),
      env,
    );
    expect(unsupported.status).toBe(422);
    expect(await unsupported.json()).toMatchObject({
      error: "unsupported_output_format",
    });
    expect(sent).toHaveLength(0);
  });

  it("admits one idempotent uid-scoped job and processes an R2 PCM chunk", async () => {
    const { env, assets, sent } = environment();
    await assets.put(
      "chunks/audio-user/conversation-1/1000.bin",
      new Uint8Array(320),
    );
    const request = async () =>
      jobs.fetch(
        new Request("https://jobs.test/v2/cf/audio-merge-jobs/run", {
          method: "POST",
          headers: {
            ...(await signedHeaders("POST", "/v2/cf/audio-merge-jobs/run")),
            "content-type": "application/json",
          },
          body: JSON.stringify({
            conversation_id: "conversation-1",
            output_format: "wav",
          }),
        }),
        env,
      );
    const first = await request();
    expect(first.status).toBe(202);
    const admitted = (await first.json()) as { job_id: string; status: string };
    expect(admitted).toMatchObject({
      status: "queued",
      job_id: expect.stringMatching(/^audio-merge-[a-f0-9]{48}$/),
    });
    expect(sent).toHaveLength(1);

    const duplicate = await request();
    expect(duplicate.status).toBe(200);
    expect(await duplicate.json()).toMatchObject({
      job_id: admitted.job_id,
      status: "queued",
    });
    expect(sent).toHaveLength(1);

    const otherAccount = await jobs.fetch(
      new Request(
        `https://jobs.test/v2/cf/audio-merge-jobs/${admitted.job_id}`,
        {
          headers: await signedHeaders(
            "GET",
            `/v2/cf/audio-merge-jobs/${admitted.job_id}`,
            "other-audio-user",
          ),
        },
      ),
      env,
    );
    expect(otherAccount.status).toBe(404);

    const message = queueMessage(sent[0]);
    await jobs.queue({ messages: [message] } as never, env);
    expect(message.ack).toHaveBeenCalledOnce();
    expect(message.retry).not.toHaveBeenCalled();
    expect(
      env.APP_DB.prepare(
        "SELECT status, attempts, result_json FROM cf_audio_merge_jobs WHERE job_id = ?",
      )
        .bind(admitted.job_id)
        .first(),
    ).resolves.toMatchObject({ status: "completed", attempts: 1 });
    expect(
      await assets.head(
        "sync-playback/audio-user/conversation-1/conversation.wav",
      ),
    ).toBeTruthy();

    const status = await jobs.fetch(
      new Request(
        `https://jobs.test/v2/cf/audio-merge-jobs/${admitted.job_id}`,
        {
          headers: await signedHeaders(
            "GET",
            `/v2/cf/audio-merge-jobs/${admitted.job_id}`,
          ),
        },
      ),
      env,
    );
    expect(status.status).toBe(200);
    expect(await status.json()).toMatchObject({
      job_id: admitted.job_id,
      status: "completed",
      result: {
        dense_storage_key:
          "sync-playback/audio-user/conversation-1/conversation.wav",
      },
    });
  });

  it("fails closed for unknown conversations and missing R2 chunks", async () => {
    const { env, sent } = environment();
    const unknown = await jobs.fetch(
      new Request("https://jobs.test/v2/cf/audio-merge-jobs/run", {
        method: "POST",
        headers: {
          ...(await signedHeaders("POST", "/v2/cf/audio-merge-jobs/run")),
          "content-type": "application/json",
        },
        body: JSON.stringify({
          conversation_id: "other-conversation",
          output_format: "wav",
        }),
      }),
      env,
    );
    expect(unknown.status).toBe(404);

    const admitted = await jobs.fetch(
      new Request("https://jobs.test/v2/cf/audio-merge-jobs/run", {
        method: "POST",
        headers: {
          ...(await signedHeaders("POST", "/v2/cf/audio-merge-jobs/run")),
          "content-type": "application/json",
        },
        body: JSON.stringify({
          conversation_id: "conversation-1",
          output_format: "wav",
        }),
      }),
      env,
    );
    const job = (await admitted.json()) as { job_id: string };
    const message = queueMessage(sent[0]);
    await jobs.queue({ messages: [message] } as never, env);
    expect(message.ack).toHaveBeenCalledOnce();
    expect(message.retry).not.toHaveBeenCalled();
    expect(
      await env.APP_DB.prepare(
        "SELECT status, last_error FROM cf_audio_merge_jobs WHERE job_id = ?",
      )
        .bind(job.job_id)
        .first(),
    ).toMatchObject({
      status: "failed",
      last_error: "legacy audio chunks are not available in R2",
    });
  });
});
