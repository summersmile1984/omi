import { describe, expect, it } from "vitest";
import type { JobsEnv } from "../workers/jobs/env";
import {
  LegacyAudioSourceError,
  legacyAudioFiles,
  legacyAudioFilesFingerprint,
  rebuildLegacyConversationAudio,
} from "../workers/jobs/legacy-audio-import";

type ConversationRow = {
  id: string;
  uid: string;
  created_at: number;
  updated_at: number | null;
  started_at: number | null;
  is_locked: number;
  audio_files_json: string;
  conversation_audio_json: string | null;
};

type PlaybackObject = {
  state: string;
  updatedAt: number;
};

function bytes(value: Int16Array | Uint8Array): Uint8Array {
  return new Uint8Array(
    value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength),
  );
}

function fakeAssets(initial: Record<string, Uint8Array>) {
  const objects = new Map(
    Object.entries(initial).map(([key, value]) => [key, value.slice()]),
  );
  return {
    objects,
    list: async ({ prefix }: { prefix: string }) => ({
      objects: [...objects.entries()]
        .filter(([key]) => key.startsWith(prefix))
        .map(([key, value]) => ({ key, size: value.byteLength })),
      truncated: false,
    }),
    head: async (key: string) =>
      objects.has(key) ? { key, size: objects.get(key)!.byteLength } : null,
    get: async (
      key: string,
      options?: { range?: { offset: number; length: number } },
    ) => {
      const value = objects.get(key);
      if (!value) return null;
      const selected = options?.range
        ? value.slice(
            options.range.offset,
            options.range.offset + options.range.length,
          )
        : value.slice();
      return {
        body: new Response(selected).body,
        arrayBuffer: async () => selected.slice().buffer,
      };
    },
    put: async (key: string, body: BodyInit) => {
      const stored = new Uint8Array(await new Response(body).arrayBuffer());
      objects.set(key, stored);
      return { key, size: stored.byteLength };
    },
  };
}

function fakeDatabase(initial: ConversationRow) {
  const row = { ...initial };
  const playback = new Map<string, PlaybackObject>();
  return {
    row,
    playback,
    prepare: (sql: string) => ({
      bind: (...args: unknown[]) => ({
        first: async () => {
          if (!sql.includes("FROM cf_conversations")) return null;
          return args[0] === row.uid && args[1] === row.id ? { ...row } : null;
        },
        run: async () => {
          if (sql.includes("INSERT INTO cf_sync_playback_objects")) {
            const storageKey = String(args[1]);
            const existing = playback.get(storageKey);
            playback.set(storageKey, {
              state: existing?.state === "committed" ? "committed" : "staging",
              updatedAt: Number(args[6]),
            });
            return { success: true, meta: { changes: 1 } };
          }
          if (
            sql.includes("UPDATE cf_sync_playback_objects") &&
            sql.includes("state = 'stored'")
          ) {
            const storageKey = String(args[2]);
            const current = playback.get(storageKey);
            if (current && current.state !== "committed") {
              current.state = "stored";
              current.updatedAt = Number(args[0]);
            }
            return { success: true, meta: { changes: current ? 1 : 0 } };
          }
          if (
            sql.includes("UPDATE cf_sync_playback_objects") &&
            sql.includes("state = 'committed'")
          ) {
            const storageKey = String(args[2]);
            const current = playback.get(storageKey);
            if (current) {
              current.state = "committed";
              current.updatedAt = Number(args[0]);
            }
            return { success: true, meta: { changes: current ? 1 : 0 } };
          }
          if (sql.includes("UPDATE cf_conversations SET updated_at")) {
            const revision = row.updated_at ?? row.created_at;
            if (
              args[3] !== row.uid ||
              args[4] !== row.id ||
              Number(args[5]) !== revision
            ) {
              return { success: true, meta: { changes: 0 }, results: [] };
            }
            row.updated_at = Number(args[0]);
            row.audio_files_json = String(args[1]);
            row.conversation_audio_json = String(args[2]);
            return {
              success: true,
              meta: { changes: 1 },
              results: [{ id: row.id }],
            };
          }
          throw new Error(`unhandled SQL: ${sql}`);
        },
      }),
    }),
  };
}

function fixture(audioFiles: Record<string, unknown>[]) {
  return {
    id: "conversation-1",
    uid: "user-1",
    created_at: 1_000,
    updated_at: null,
    started_at: 1_000,
    is_locked: 0,
    audio_files_json: JSON.stringify(audioFiles),
    conversation_audio_json: null,
  } satisfies ConversationRow;
}

function environment(
  row: ConversationRow,
  initialObjects: Record<string, Uint8Array>,
  encryptionSecret?: string,
) {
  const database = fakeDatabase(row);
  const assets = fakeAssets(initialObjects);
  const env = {
    APP_DB: database,
    ASSETS: assets,
    LEGACY_AUDIO_ENCRYPTION_SECRET: encryptionSecret,
  } as unknown as JobsEnv;
  return { env, database, assets };
}

async function encryptedFrame(
  clear: Uint8Array,
  secret: string,
  uid: string,
  nonceByte: number,
): Promise<Uint8Array> {
  const base = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    "HKDF",
    false,
    ["deriveKey"],
  );
  const key = await crypto.subtle.deriveKey(
    {
      name: "HKDF",
      hash: "SHA-256",
      salt: new TextEncoder().encode(uid),
      info: new TextEncoder().encode("user-data-encryption"),
    },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt"],
  );
  const nonce = new Uint8Array(12);
  nonce.fill(nonceByte);
  const cipher = new Uint8Array(
    await crypto.subtle.encrypt(
      { name: "AES-GCM", iv: nonce.buffer },
      key,
      clear.slice().buffer,
    ),
  );
  const payloadLength = nonce.byteLength + cipher.byteLength;
  const output = new Uint8Array(4 + payloadLength);
  new DataView(output.buffer).setUint32(0, payloadLength, false);
  output.set(nonce, 4);
  output.set(cipher, 4 + nonce.byteLength);
  return output;
}

describe("legacy audio import", () => {
  it("streams raw legacy PCM chunks into per-file and dense Worker WAV objects", async () => {
    const audioFiles = [
      {
        id: "audio-1",
        provider: "gcs",
        chunk_timestamps: [1_000, 1_000.01],
      },
    ];
    const first = new Int16Array(160);
    const second = new Int16Array(160);
    first.fill(1_111);
    second.fill(-2_222);
    const { env, database, assets } = environment(fixture(audioFiles), {
      "chunks/user-1/conversation-1/1000.bin": bytes(first),
      "chunks/user-1/conversation-1/1000.01.bin": bytes(second),
    });
    const fingerprint = await legacyAudioFilesFingerprint(
      legacyAudioFiles(database.row.audio_files_json),
    );

    const result = await rebuildLegacyConversationAudio(
      env,
      { uid: "user-1", job_id: "job-1" },
      "conversation-1",
      fingerprint,
      2_000,
    );

    expect(result).toMatchObject({
      audio_file_count: 1,
      rebuilt_audio_file_count: 1,
      unavailable_audio_file_ids: [],
      dense_storage_key: "sync-playback/user-1/conversation-1/conversation.wav",
    });
    const window = assets.objects.get(
      "sync-playback/user-1/conversation-1/audio-1.wav",
    )!;
    const dense = assets.objects.get(result.dense_storage_key)!;
    expect(new TextDecoder().decode(window.subarray(0, 4))).toBe("RIFF");
    expect(new DataView(window.buffer).getUint32(40, true)).toBe(640);
    expect(dense).toEqual(window);
    expect(new Int16Array(window.buffer, 44, 320).slice(0, 160)).toEqual(first);
    expect(new Int16Array(window.buffer, 44, 320).slice(160)).toEqual(second);

    const storedFiles = JSON.parse(database.row.audio_files_json);
    expect(storedFiles[0]).toMatchObject({
      provider: "cloudflare-r2",
      storage_key: "sync-playback/user-1/conversation-1/audio-1.wav",
      content_type: "audio/wav",
      pcm_bytes: 640,
      duration: 0.02,
    });
    expect(JSON.parse(database.row.conversation_audio_json!)).toMatchObject({
      storage_key: result.dense_storage_key,
      captured_duration: 0.02,
      spans: [{ file_id: "audio-1", wall_offset: 0, len: 0.02 }],
    });
    expect([...database.playback.values()].map((value) => value.state)).toEqual(
      ["committed", "committed"],
    );
  });

  it("decrypts legacy batch frames with the backend HKDF and AES-GCM contract", async () => {
    const secret = "legacy-secret-with-at-least-thirty-two-bytes-123456";
    const audioFiles = [
      {
        id: "audio-1",
        provider: "gcs",
        chunk_timestamps: [1_000, 1_000.01],
      },
    ];
    const first = bytes(new Int16Array([10, 20, 30, 40]));
    const second = bytes(new Int16Array([-10, -20, -30, -40]));
    const firstFrame = await encryptedFrame(first, secret, "user-1", 1);
    const secondFrame = await encryptedFrame(second, secret, "user-1", 2);
    const encrypted = new Uint8Array(
      firstFrame.byteLength + secondFrame.byteLength,
    );
    encrypted.set(firstFrame);
    encrypted.set(secondFrame, firstFrame.byteLength);
    const { env, database, assets } = environment(
      fixture(audioFiles),
      {
        "chunks/user-1/conversation-1/1000-1000.01.batch.enc": encrypted,
      },
      secret,
    );
    const fingerprint = await legacyAudioFilesFingerprint(
      legacyAudioFiles(database.row.audio_files_json),
    );

    await rebuildLegacyConversationAudio(
      env,
      { uid: "user-1", job_id: "job-encrypted" },
      "conversation-1",
      fingerprint,
      2_000,
    );

    const wav = assets.objects.get(
      "sync-playback/user-1/conversation-1/audio-1.wav",
    )!;
    expect(wav.subarray(44)).toEqual(new Uint8Array([...first, ...second]));
  });

  it("fails deterministically when the copied R2 inventory has no legacy chunks", async () => {
    const audioFiles = [
      { id: "audio-1", provider: "gcs", chunk_timestamps: [1_000] },
    ];
    const { env, database, assets } = environment(fixture(audioFiles), {});
    const fingerprint = await legacyAudioFilesFingerprint(
      legacyAudioFiles(database.row.audio_files_json),
    );

    await expect(
      rebuildLegacyConversationAudio(
        env,
        { uid: "user-1", job_id: "job-missing" },
        "conversation-1",
        fingerprint,
        2_000,
      ),
    ).rejects.toThrow(LegacyAudioSourceError);
    expect(database.row.updated_at).toBeNull();
    expect(database.row.conversation_audio_json).toBeNull();
    expect(assets.objects.size).toBe(0);
  });
});
