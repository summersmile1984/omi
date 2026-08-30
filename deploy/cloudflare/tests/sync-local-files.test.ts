import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { describe, expect, it } from "vitest";
import type { JobMessage } from "../workers/jobs/env";
import {
  cleanupOrphanPlaybackObjects,
  processSyncJobMessage,
} from "../workers/jobs/sync-local-files";

function wal(frame: Uint8Array): ArrayBuffer {
  const output = new Uint8Array(frame.byteLength + 4);
  new DataView(output.buffer).setUint32(0, frame.byteLength, true);
  output.set(frame, 4);
  return output.buffer;
}

function processingHarness(
  options: {
    failAsr?: boolean;
    existingConversation?: boolean;
    privateCloudSyncEnabled?: boolean;
  } = {},
) {
  const now = Math.floor(Date.now() / 1_000);
  const audio = wal(new Uint8Array(new Int16Array(160).buffer));
  const job = {
    job_id: "sync-job-1",
    uid: "user-1",
    content_id: "a".repeat(64),
    status: "queued",
    lane: "fresh",
    capture_time_trust: "device_bound",
    conversation_id: options.existingConversation ? "conversation-1" : null,
    source: "omi",
    client_device_id: "ios_1234abcd",
    client_platform: "ios",
    recording_age_seconds: 60,
    total_files: 1,
    total_segments: 0,
    processed_segments: 0,
    successful_segments: 0,
    failed_segments: 0,
    attempts: 0,
    result_json: null as string | null,
    last_error: null as string | null,
    reason_code: null as string | null,
    created_at: now,
    updated_at: now,
    lease_until: null as number | null,
  };
  const file = {
    job_id: job.job_id,
    uid: job.uid,
    ordinal: 0,
    filename: `audio_omibatch_pcm16_16000_1_fs160_${now - 60}.bin`,
    object_key: `cf-sync/${job.uid}/${job.job_id}/0`,
    sha256: bytesToHex(sha256(new Uint8Array(audio))),
    size: audio.byteLength,
    capture_at: now - 60,
    codec: "pcm16",
    sample_rate: 16_000,
    channels: 1,
    frame_size: 160,
    status: "staged",
    transcription_json: null as string | null,
    speech_ms: 0,
    duration_ms: 0,
    detected_language: null as string | null,
    last_error: null as string | null,
  };
  const existingConversation = options.existingConversation
    ? {
        id: "conversation-1",
        created_at: now - 60,
        updated_at: now - 60,
        started_at: now - 60,
        finished_at: now,
        source: "omi",
        language: null,
        status: "processing",
        visibility: "private",
        starred: 0,
        discarded: 0,
        is_locked: 0,
        deferred: 0,
        private_cloud_sync_enabled: 0,
        folder_id: null,
        client_device_id: "ios_1234abcd",
        client_platform: "ios",
        structured_json: "{}",
        transcript_segments_json: "[]",
        photos_json: "[]",
        audio_files_json: "[]",
        conversation_audio_json: null,
        apps_results_json: "[]",
        suggested_apps_json: "[]",
        geolocation_json: null,
        external_data_json: null,
        calendar_event_json: null,
      }
    : null;
  const conversations = new Map<string, Record<string, unknown>>(
    existingConversation
      ? [[existingConversation.id, { ...existingConversation }]]
      : [],
  );
  const playbackLedger = new Map<string, Record<string, unknown>>();
  const usage = new Map<string, Record<string, unknown>>();
  let ledgerStatus = "processing";

  const bound = (sql: string, args: unknown[]) => ({
    first: async () => {
      if (sql.startsWith("SELECT status, updated_at FROM cf_sync_jobs")) {
        return { status: job.status, updated_at: job.updated_at };
      }
      if (sql.startsWith("SELECT job_id, uid, content_id, status"))
        return { ...job };
      if (sql.startsWith("SELECT COALESCE(state.stage")) {
        return {
          stage: "none",
          restrict_until: null,
          daily_ms: 0,
          three_day_ms: 0,
          weekly_ms: 0,
        };
      }
      if (
        sql.startsWith("SELECT EXISTS(") &&
        sql.includes("FROM cf_user_privacy_settings")
      ) {
        return { enabled: options.privateCloudSyncEnabled === false ? 0 : 1 };
      }
      if (
        sql.startsWith(
          "SELECT created_at, updated_at, started_at, audio_files_json FROM cf_conversations",
        )
      ) {
        const row = conversations.get(String(args[1]));
        return row ? { ...row } : null;
      }
      if (sql.includes("FROM cf_conversations"))
        return existingConversation ? { ...existingConversation } : null;
      return null;
    },
    all: async () => {
      if (sql.includes("FROM cf_sync_job_files"))
        return { results: [{ ...file }] };
      return { results: [] };
    },
    run: async () => {
      if (sql.startsWith("INSERT INTO cf_sync_playback_objects")) {
        const key = String(args[1]);
        const existing = playbackLedger.get(key);
        playbackLedger.set(key, {
          uid: args[0],
          storage_key: key,
          conversation_id: args[2],
          audio_file_id: args[3],
          job_id: args[4],
          state: existing?.state === "committed" ? "committed" : "staging",
          updated_at: args[6],
        });
        return { meta: { changes: 1 } };
      }
      if (sql.startsWith("UPDATE cf_sync_playback_objects SET state =")) {
        const key = String(args[2]);
        const row = playbackLedger.get(key);
        if (row) {
          playbackLedger.set(key, {
            ...row,
            state: sql.includes("'committed'") ? "committed" : "stored",
            updated_at: args[0],
          });
        }
        return { meta: { changes: row ? 1 : 0 } };
      }
      if (sql.startsWith("UPDATE cf_sync_jobs SET status = 'running'")) {
        if (job.status !== "queued") return { meta: { changes: 0 } };
        job.status = "running";
        job.attempts += 1;
        job.lease_until = Number(args[0]);
        job.updated_at = Number(args[1]);
        return { meta: { changes: 1 } };
      }
      if (
        sql.startsWith("UPDATE cf_sync_job_files SET status = 'transcribed'")
      ) {
        file.status = "transcribed";
        file.transcription_json = String(args[0]);
        file.speech_ms = Number(args[1]);
        file.duration_ms = Number(args[2]);
        file.detected_language = args[3] === null ? null : String(args[3]);
        return { meta: { changes: 1 } };
      }
      if (sql.startsWith("UPDATE cf_sync_job_files SET status = 'failed'")) {
        file.status = "failed";
        file.last_error = String(args[0]);
        return { meta: { changes: 1 } };
      }
      if (sql.startsWith("INSERT INTO cf_conversations")) {
        conversations.set(String(args[1]), {
          uid: args[0],
          id: args[1],
          created_at: args[2],
          updated_at: args[3],
          private_cloud_sync_enabled: 0,
          structured_json: args[10],
          transcript_segments_json: args[11],
          audio_files_json: "[]",
          conversation_audio_json: null,
        });
        return {
          meta: { changes: 4 },
          results: [{ id: String(args[1]) }],
        };
      }
      if (
        sql.startsWith(
          "UPDATE cf_conversations SET updated_at = ?, private_cloud_sync_enabled = 1",
        )
      ) {
        const id = String(args[4]);
        const conversation = conversations.get(id);
        if (
          !conversation ||
          Number(conversation.updated_at) !== Number(args[5])
        )
          return { meta: { changes: 0 }, results: [] };
        conversations.set(id, {
          ...conversation,
          updated_at: args[0],
          private_cloud_sync_enabled: 1,
          audio_files_json: args[1],
          conversation_audio_json: args[2],
        });
        return { meta: { changes: 1 }, results: [{ id }] };
      }
      if (sql.startsWith("UPDATE cf_conversations SET updated_at")) {
        const id = String(args[7]);
        conversations.set(id, {
          ...(conversations.get(id) || {}),
          updated_at: args[0],
          structured_json: args[4],
          transcript_segments_json: args[5],
        });
        return { meta: { changes: 7 }, results: [{ id }] };
      }
      if (sql.startsWith("INSERT INTO cf_usage_sources")) {
        usage.set(`conversation:${String(args[1])}`, { seconds: args[3] });
        return { meta: { changes: 1 } };
      }
      if (sql.startsWith("INSERT INTO cf_fair_use_usage_sources")) {
        usage.set(`fair-use:${String(args[2])}`, { speech_ms: args[4] });
        return { meta: { changes: 1 } };
      }
      if (sql.startsWith("UPDATE cf_sync_jobs SET status = ?")) {
        job.status = String(args[0]);
        job.total_segments = Number(args[1]);
        job.processed_segments = Number(args[2]);
        job.successful_segments = Number(args[3]);
        job.failed_segments = Number(args[4]);
        job.result_json = String(args[5]);
        job.last_error = args[6] === null ? null : String(args[6]);
        job.reason_code = args[7] === null ? null : String(args[7]);
        job.updated_at = Number(args[8]);
        return { meta: { changes: 1 } };
      }
      if (sql.startsWith("UPDATE cf_sync_content_ledger SET status = ?")) {
        ledgerStatus = String(args[0]);
        return { meta: { changes: 1 } };
      }
      if (sql.startsWith("UPDATE cf_sync_jobs SET status = 'queued'")) {
        job.status = "queued";
        job.last_error = String(args[0]);
        job.updated_at = Number(args[1]);
        job.lease_until = null;
        return { meta: { changes: 1 } };
      }
      return { meta: { changes: 1 } };
    },
  });
  const database = {
    prepare: (sql: string) => ({
      bind: (...args: unknown[]) => bound(sql, args),
    }),
    batch: async (statements: Array<{ run(): Promise<unknown> }>) =>
      Promise.all(statements.map((statement) => statement.run())),
  };
  const blobs = new Map([[file.object_key, audio]]);
  const env = {
    APP_DB: database,
    ASSETS: {
      get: async (
        key: string,
        options?: { range?: { offset: number; length: number } },
      ) => {
        let value = blobs.get(key);
        if (value && options?.range) {
          value = value.slice(
            options.range.offset,
            options.range.offset + options.range.length,
          );
        }
        return value ? { arrayBuffer: async () => value.slice(0) } : null;
      },
      delete: async (key: string) => {
        blobs.delete(key);
      },
      put: async (
        key: string,
        value: ArrayBuffer | ReadableStream<Uint8Array>,
      ) => {
        const bytes =
          value instanceof ReadableStream
            ? await new Response(value).arrayBuffer()
            : value.slice(0);
        blobs.set(key, bytes);
        return { key };
      },
    },
    AI: {
      run: async (model: string) => {
        if (model.includes("whisper")) {
          if (options.failAsr) throw new Error("provider unavailable");
          return {
            text: "hello from cloudflare",
            segments: [{ text: "hello from cloudflare", start: 0, end: 0.01 }],
            detected_language: "en",
          };
        }
        return {
          response: {
            title: "Cloudflare sync",
            overview: "A short validation recording.",
            category: "other",
            action_items: [],
            events: [],
          },
        };
      },
    },
  };
  const delivery = {
    body: {
      jobId: job.job_id,
      uid: job.uid,
      kind: "sync_local_files",
      payload: { lane: "fresh" },
    } satisfies JobMessage,
    attempts: 1,
    acked: 0,
    retries: [] as Array<{ delaySeconds?: number }>,
    ack() {
      this.acked += 1;
    },
    retry(options?: { delaySeconds?: number }) {
      this.retries.push(options || {});
    },
  };
  return {
    job,
    file,
    conversations,
    playbackLedger,
    usage,
    blobs,
    env,
    delivery,
    ledgerStatus: () => ledgerStatus,
  };
}

describe("sync-local-files queue processing", () => {
  it("decodes a staged WAL and commits conversation, usage, and completed ledger", async () => {
    const harness = processingHarness();
    await processSyncJobMessage(
      harness.delivery as never,
      harness.env as never,
    );

    expect(harness.delivery.acked).toBe(1);
    expect(harness.delivery.retries).toEqual([]);
    expect(harness.job.status).toBe("completed");
    expect(harness.ledgerStatus()).toBe("completed");
    expect(harness.conversations).toHaveLength(1);
    const conversation = [...harness.conversations.values()][0];
    expect(JSON.parse(String(conversation.structured_json))).toMatchObject({
      title: "Cloudflare sync",
    });
    expect(
      JSON.parse(String(conversation.transcript_segments_json)),
    ).toHaveLength(1);
    const audioFiles = JSON.parse(String(conversation.audio_files_json));
    expect(audioFiles).toHaveLength(1);
    expect(audioFiles[0]).toMatchObject({
      provider: "cloudflare-r2",
      content_type: "audio/wav",
      conversation_id: conversation.id,
      sample_rate: 16_000,
      channels: 1,
      pcm_bytes: 320,
    });
    expect(
      JSON.parse(String(conversation.conversation_audio_json)),
    ).toMatchObject({
      content_type: "audio/wav",
      captured_duration: 0.01,
      duration: 0.01,
      spans: [
        {
          file_id: audioFiles[0].id,
          wall_offset: 0,
          artifact_offset: 0,
          len: 0.01,
        },
      ],
    });
    expect(harness.usage.get(`fair-use:${"a".repeat(64)}`)).toEqual({
      speech_ms: 10,
    });
    expect([...harness.blobs.keys()].sort()).toEqual([
      expect.stringMatching(
        new RegExp(`^sync-playback/${harness.job.uid}/${conversation.id}/cf-`),
      ),
      `sync-playback/${harness.job.uid}/${conversation.id}/conversation.wav`,
    ]);
    const dense = harness.blobs.get(
      `sync-playback/${harness.job.uid}/${conversation.id}/conversation.wav`,
    );
    expect(new TextDecoder().decode(dense?.slice(0, 4))).toBe("RIFF");
    expect(dense?.byteLength).toBe(364);
    expect([...harness.playbackLedger.values()]).toEqual([
      expect.objectContaining({ state: "committed" }),
      expect.objectContaining({
        audio_file_id: "conversation",
        state: "committed",
      }),
    ]);
    expect(JSON.parse(harness.job.result_json || "{}")).toMatchObject({
      outcome: "completed",
      failed_segments: 0,
      successful_segments: 1,
    });
  });

  it("removes staging bytes without retaining playback when private cloud sync is disabled", async () => {
    const harness = processingHarness({ privateCloudSyncEnabled: false });
    await processSyncJobMessage(
      harness.delivery as never,
      harness.env as never,
    );

    expect(harness.job.status).toBe("completed");
    const conversation = [...harness.conversations.values()][0];
    expect(JSON.parse(String(conversation.audio_files_json))).toEqual([]);
    expect(harness.blobs.size).toBe(0);
  });

  it("retries provider failures, then records a truthful failed terminal state", async () => {
    const harness = processingHarness({ failAsr: true });
    await processSyncJobMessage(
      harness.delivery as never,
      harness.env as never,
    );
    await processSyncJobMessage(
      harness.delivery as never,
      harness.env as never,
    );
    await processSyncJobMessage(
      harness.delivery as never,
      harness.env as never,
    );

    expect(harness.delivery.retries).toEqual([
      { delaySeconds: 15 },
      { delaySeconds: 15 },
    ]);
    expect(harness.delivery.acked).toBe(1);
    expect(harness.job.status).toBe("failed");
    expect(harness.ledgerStatus()).toBe("retryable");
    expect(harness.conversations.size).toBe(0);
    expect(harness.blobs.size).toBe(0);
  });

  it("accepts a successful conversation CAS when FTS triggers inflate D1 changes", async () => {
    const harness = processingHarness({ existingConversation: true });
    await processSyncJobMessage(
      harness.delivery as never,
      harness.env as never,
    );

    expect(harness.job.status).toBe("completed");
    expect(harness.delivery.acked).toBe(1);
    expect(harness.delivery.retries).toEqual([]);
    const conversation = harness.conversations.get("conversation-1");
    expect(JSON.parse(String(conversation?.structured_json))).toMatchObject({
      title: "Cloudflare sync",
    });
  });
});

describe("sync playback reconciliation", () => {
  it("commits referenced intents and deletes only stale unreferenced objects", async () => {
    const states = new Map([
      ["sync-playback/user-1/referenced/audio-1.wav", "stored"],
      ["sync-playback/user-1/referenced/conversation.wav", "stored"],
      ["sync-playback/user-1/orphan/audio-2.wav", "stored"],
    ]);
    const deleted: string[] = [];
    const database = {
      prepare: (sql: string) => ({
        bind: (...args: unknown[]) => ({
          all: async () => ({
            results: [...states.keys()].map((storageKey) => ({
              uid: "user-1",
              storage_key: storageKey,
              conversation_id: storageKey.includes("/referenced/")
                ? "referenced"
                : "orphan",
            })),
          }),
          first: async () =>
            String(args[1]) === "referenced"
              ? {
                  audio_files_json: JSON.stringify([
                    {
                      id: "audio-1",
                      storage_key:
                        "sync-playback/user-1/referenced/audio-1.wav",
                    },
                  ]),
                  conversation_audio_json: JSON.stringify({
                    storage_key:
                      "sync-playback/user-1/referenced/conversation.wav",
                  }),
                }
              : {
                  audio_files_json: "[]",
                  conversation_audio_json: null,
                },
          run: async () => {
            if (sql.startsWith("UPDATE cf_sync_playback_objects")) {
              states.set(String(args[2]), "committed");
            }
            if (sql.startsWith("DELETE FROM cf_sync_playback_objects")) {
              states.delete(String(args[1]));
            }
            return { meta: { changes: 1 } };
          },
        }),
      }),
    };
    const env = {
      APP_DB: database,
      ASSETS: {
        delete: async (key: string) => {
          deleted.push(key);
        },
      },
    };

    await cleanupOrphanPlaybackObjects(env as never, 10_000);

    expect(states).toEqual(
      new Map([
        ["sync-playback/user-1/referenced/audio-1.wav", "committed"],
        ["sync-playback/user-1/referenced/conversation.wav", "committed"],
      ]),
    );
    expect(deleted).toEqual(["sync-playback/user-1/orphan/audio-2.wav"]);
  });
});
