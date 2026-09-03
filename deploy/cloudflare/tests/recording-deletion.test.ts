import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  processRecordingDeletionMessage,
  reconcileRecordingDeletions,
} from "../workers/jobs/recording-deletion";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import jobs from "../workers/jobs/index";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../workers/shared/auth-context";

type TestD1Result<T> = {
  success: true;
  results: T[];
  meta: { changes: number };
};

type BoundStatement = {
  sql: string;
  args: unknown[];
  execute(): TestD1Result<unknown>;
};

type PreparedStatement = BoundStatement & {
  bind(...values: unknown[]): PreparedStatement;
  first<T>(): Promise<T | null>;
  all<T>(): Promise<TestD1Result<T>>;
  run(): Promise<TestD1Result<unknown>>;
};

function sqliteValue(value: unknown) {
  return typeof value === "boolean" ? Number(value) : (value as never);
}

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

  prepare(sql: string): PreparedStatement {
    const build = (args: unknown[] = []): PreparedStatement => ({
      sql,
      args,
      bind: (...values: unknown[]) => build(values),
      first: async <T>() => {
        const row = this.database.prepare(sql).get(...args.map(sqliteValue)) as
          T | undefined;
        return row ?? null;
      },
      all: async <T>() => ({
        success: true,
        results: this.database
          .prepare(sql)
          .all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0 },
      }),
      run: async () => build(args).execute(),
      execute: () => {
        const statement = this.database.prepare(sql);
        if (/^SELECT\b/i.test(sql.trimStart())) {
          return {
            success: true,
            results: statement.all(...args.map(sqliteValue)),
            meta: { changes: 0 },
          };
        }
        const result = statement.run(...args.map(sqliteValue));
        return {
          success: true,
          results: [],
          meta: { changes: Number(result.changes) },
        };
      },
    });
    return build();
  }

  async batch(statements: BoundStatement[]) {
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

  row<T>(sql: string, ...args: unknown[]): T | null {
    return (
      (this.database.prepare(sql).get(...args.map(sqliteValue)) as
        T | undefined) ?? null
    );
  }

  close() {
    this.database.close();
  }
}

function fakeBucket(
  initial: Record<string, Uint8Array> = {},
  options: { failDeleteOnce?: boolean } = {},
) {
  const objects = new Map(Object.entries(initial));
  let failDelete = options.failDeleteOnce === true;
  return {
    objects,
    binding: {
      list: vi.fn(
        async ({ prefix, limit }: { prefix?: string; limit?: number }) => ({
          objects: [...objects.keys()]
            .filter((key) => key.startsWith(prefix || ""))
            .slice(0, limit || 1_000)
            .map((key) => ({
              key,
              version: "1",
              size: objects.get(key)?.byteLength || 0,
              etag: "etag",
              uploaded: new Date(0),
              httpEtag: '"etag"',
              checksums: {},
              storageClass: "Standard",
            })),
          truncated: false,
          delimitedPrefixes: [],
        }),
      ),
      head: vi.fn(async (key: string) =>
        objects.has(key)
          ? {
              key,
              version: "1",
              size: objects.get(key)?.byteLength || 0,
              etag: "etag",
              uploaded: new Date(0),
              httpEtag: '"etag"',
              checksums: {},
              storageClass: "Standard",
              httpMetadata: {
                contentType: key.includes("/audio/")
                  ? "audio/wav"
                  : "image/png",
              },
            }
          : null,
      ),
      delete: vi.fn(async (keys: string | string[]) => {
        if (failDelete) {
          failDelete = false;
          throw new Error("R2 unavailable");
        }
        for (const key of Array.isArray(keys) ? keys : [keys]) {
          objects.delete(key);
        }
      }),
    } as unknown as R2Bucket,
  };
}

function fakeQueue() {
  const sent: Array<{ body: JobMessage; delaySeconds: number }> = [];
  return {
    sent,
    binding: {
      send: vi.fn(
        async (body: JobMessage, options?: { delaySeconds?: number }) => {
          sent.push({ body, delaySeconds: options?.delaySeconds || 0 });
        },
      ),
    } as unknown as Queue<JobMessage>,
  };
}

function environment(options: { failAssetDeleteOnce?: boolean } = {}) {
  const database = new SqliteD1();
  const assets = fakeBucket(
    {
      "cf-sync/recording-user/job/0": new Uint8Array([1]),
      "sync-playback/recording-user/conversation/audio.wav": new Uint8Array([
        2,
      ]),
      "playback/recording-user/conversation/audio.mp3": new Uint8Array([3]),
      "merged/recording-user/conversation/audio.wav": new Uint8Array([4]),
      "chunks/recording-user/conversation/chunk.opus": new Uint8Array([5]),
      "cf-assets/recording-user/audio/current": new Uint8Array([6]),
      "cf-assets/recording-user/audio/superseded": new Uint8Array([9]),
      "cf-assets/recording-user/image/current": new Uint8Array([7]),
    },
    { failDeleteOnce: options.failAssetDeleteOnce },
  );
  const recordings = fakeBucket({
    "recording-user/conversation.wav": new Uint8Array([8]),
  });
  const queue = fakeQueue();
  const env = {
    APP_DB: database as unknown as D1Database,
    ASSETS: assets.binding,
    CONVERSATION_RECORDINGS: recordings.binding,
    JOBS: queue.binding,
    INTERNAL_ASSERTION_SECRET: "recording-deletion-secret",
  } as unknown as JobsEnv;
  return { database, assets, recordings, queue, env };
}

function seedRecordingState(database: SqliteD1) {
  const db = database.database;
  db.prepare(
    `INSERT INTO cf_user_privacy_settings
       (uid, store_recording_permission, private_cloud_sync_enabled,
        created_at, updated_at)
     VALUES ('recording-user', 1, 1, 1, 1)`,
  ).run();
  db.prepare(
    `INSERT INTO cf_conversations
       (uid, id, created_at, private_cloud_sync_enabled, audio_files_json,
        conversation_audio_json)
     VALUES ('recording-user', 'conversation', 1, 1, ?, ?)`,
  ).run(
    JSON.stringify([
      {
        id: "audio",
        storage_key: "sync-playback/recording-user/conversation/audio.wav",
      },
    ]),
    JSON.stringify({
      storage_key: "sync-playback/recording-user/conversation/conversation.wav",
    }),
  );
  db.prepare(
    `INSERT INTO cf_sync_jobs
       (job_id, uid, content_id, status, lane, capture_time_trust, source,
        total_files, created_at, updated_at, expires_at)
     VALUES ('sync-job', 'recording-user', 'content', 'completed', 'fresh',
             'device_bound', 'omi', 1, 1, 1, 1000)`,
  ).run();
  db.prepare(
    `INSERT INTO cf_sync_job_files
       (job_id, uid, ordinal, filename, object_key, sha256, size, capture_at,
        codec, sample_rate, channels, frame_size)
     VALUES ('sync-job', 'recording-user', 0, 'audio.opus',
             'cf-sync/recording-user/job/0', ?, 1, 1, 'opus', 16000, 1, 320)`,
  ).run("a".repeat(64));
  db.prepare(
    `INSERT INTO cf_sync_playback_objects
       (uid, storage_key, conversation_id, audio_file_id, job_id, state,
        created_at, updated_at)
     VALUES ('recording-user',
             'sync-playback/recording-user/conversation/audio.wav',
             'conversation', 'audio', 'sync-job', 'committed', 1, 1)`,
  ).run();
  for (const asset of [
    {
      key: "voice-note",
      storage: "cf-assets/recording-user/audio/current",
      contentType: "audio/wav",
    },
    {
      key: "avatar",
      storage: "cf-assets/recording-user/image/current",
      contentType: "image/png",
    },
  ]) {
    db.prepare(
      `INSERT INTO cf_asset_objects
         (uid, object_key, content_type, size, etag, checksum_sha256,
          storage_key, created_at, updated_at)
       VALUES ('recording-user', ?, ?, 1, 'etag', ?, ?, 1, 1)`,
    ).run(asset.key, asset.contentType, "b".repeat(64), asset.storage);
  }
  db.prepare(
    `INSERT INTO cf_asset_cleanup_tasks
       (storage_key, uid, logical_key, content_type, reason, not_before,
        attempts, created_at, updated_at)
     VALUES ('cf-assets/recording-user/audio/superseded', 'recording-user',
             'voice-note', NULL, 'superseded', 1, 0, 1, 1)`,
  ).run();
}

async function deletionHeaders(path: string) {
  const signed = await createSignedAuthContext(
    {
      uid: "recording-user",
      authority: "better-auth",
      requestId: "recording-deletion-request",
    },
    "jobs",
    "DELETE",
    path,
    "recording-deletion-secret",
  );
  if (!signed) throw new Error("recording deletion assertion unavailable");
  return {
    [AUTH_CONTEXT_HEADER]: signed.encoded,
    [AUTH_SIGNATURE_HEADER]: signed.signature,
  };
}

function queueMessage(body: JobMessage) {
  const ack = vi.fn();
  const retry = vi.fn();
  return {
    message: { body, ack, retry } as unknown as Message<JobMessage>,
    ack,
    retry,
  };
}

async function admit(state: ReturnType<typeof environment>) {
  const path = "/v1/users/store-recording-permission";
  return jobs.fetch(
    new Request(`https://jobs.test${path}`, {
      method: "DELETE",
      headers: await deletionHeaders(path),
    }),
    state.env,
  );
}

describe("Cloudflare recording deletion", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-30T00:00:00.000Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("invalidates access immediately and durably drains recording-only surfaces", async () => {
    const state = environment();
    try {
      seedRecordingState(state.database);
      const response = await admit(state);
      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toEqual({ status: "ok" });
      expect(
        state.database.row<{ store_recording_permission: number }>(
          "SELECT store_recording_permission FROM cf_user_privacy_settings WHERE uid = ?",
          "recording-user",
        )?.store_recording_permission,
      ).toBe(0);
      expect(() =>
        state.database.database
          .prepare(
            "UPDATE cf_user_privacy_settings SET store_recording_permission = 1 WHERE uid = ?",
          )
          .run("recording-user"),
      ).toThrow(/recording deletion fence/);

      let processed = 0;
      while (
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_recording_deletion_intents",
        )?.count !== 0 &&
        processed < 20
      ) {
        const dispatch = state.queue.sent.shift();
        if (!dispatch) throw new Error("missing recording deletion dispatch");
        vi.advanceTimersByTime(dispatch.delaySeconds * 1_000);
        const queued = queueMessage(dispatch.body);
        await processRecordingDeletionMessage(queued.message, state.env);
        expect(queued.ack).toHaveBeenCalledOnce();
        processed += 1;
      }

      expect(processed).toBeLessThan(20);
      expect(state.recordings.objects.size).toBe(0);
      expect([...state.assets.objects.keys()].sort()).toEqual([
        "cf-assets/recording-user/image/current",
      ]);
      const conversation = state.database.row<{
        audio_files_json: string;
        conversation_audio_json: string | null;
        private_cloud_sync_enabled: number;
      }>(
        `SELECT audio_files_json, conversation_audio_json,
                private_cloud_sync_enabled
         FROM cf_conversations WHERE uid = ? AND id = ?`,
        "recording-user",
        "conversation",
      );
      expect(conversation).toEqual({
        audio_files_json: "[]",
        conversation_audio_json: null,
        private_cloud_sync_enabled: 0,
      });
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_sync_job_files WHERE uid = ?",
          "recording-user",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_sync_playback_objects WHERE uid = ?",
          "recording-user",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_asset_objects WHERE uid = ? AND content_type LIKE 'audio/%'",
          "recording-user",
        )?.count,
      ).toBe(0);
    } finally {
      state.database.close();
    }
  });

  it("keeps a failed R2 cleanup durable and re-dispatches it after backoff", async () => {
    const state = environment({ failAssetDeleteOnce: true });
    try {
      seedRecordingState(state.database);
      await admit(state);
      const firstDispatch = state.queue.sent.shift();
      if (!firstDispatch) throw new Error("missing initial dispatch");
      const first = queueMessage(firstDispatch.body);
      await processRecordingDeletionMessage(first.message, state.env);
      expect(first.ack).toHaveBeenCalledOnce();

      const secondDispatch = state.queue.sent.shift();
      if (!secondDispatch) throw new Error("missing R2 retry dispatch");
      vi.advanceTimersByTime(secondDispatch.delaySeconds * 1_000);
      const second = queueMessage(secondDispatch.body);
      await processRecordingDeletionMessage(second.message, state.env);
      expect(second.ack).toHaveBeenCalledOnce();

      const failed = state.database.row<{
        status: string;
        next_attempt_at: number;
      }>(
        "SELECT status, next_attempt_at FROM cf_recording_deletion_intents WHERE uid = ?",
        "recording-user",
      );
      expect(failed?.status).toBe("failed");
      vi.setSystemTime(new Date(Number(failed?.next_attempt_at) * 1_000));
      await expect(
        reconcileRecordingDeletions(state.env, Number(failed?.next_attempt_at)),
      ).resolves.toBe(1);
      expect(state.queue.sent.at(-1)?.body.kind).toBe("recording_delete");
    } finally {
      state.database.close();
    }
  });
});
