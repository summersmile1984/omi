import type { JobsEnv } from "./env";
import {
  buildConversationPlayback,
  fixedLengthPlaybackStream,
  markPlaybackStored,
  recordPlaybackIntent,
  recordingStorageEnabled,
  type PlaybackAudioFile,
  type PlaybackJob,
} from "./sync-local-files";
import {
  decodeLegacyOpusContainer,
  pcm16WavHeader,
  PLAYBACK_CHANNELS,
  PLAYBACK_SAMPLE_RATE,
} from "./sync-audio";
import { encodePcm16MonoToMp3 } from "./mp3-encoder";

const MAX_AUDIO_FILES = 100;
const MAX_LEGACY_OBJECTS = 5_000;
const MAX_ENCRYPTED_FRAMES = 20_000;
const MAX_ENCRYPTED_FRAME_BYTES = 16 * 1024 * 1024;
const MAX_LEGACY_OPUS_OBJECT_BYTES = 16 * 1024 * 1024;
const MAX_CONVERSATION_AUDIO_JSON_BYTES = 1_000_000;
const MAX_WAV_PCM_BYTES = 0xffff_ffff - 36;
const MAX_MP3_PCM_BYTES = 64 * 1024 * 1024;
const PCM_BYTES_PER_SECOND = PLAYBACK_SAMPLE_RATE * PLAYBACK_CHANNELS * 2;
const ZERO_CHUNK = new Uint8Array(64 * 1024);
const SAFE_AUDIO_FILE_ID = /^[A-Za-z0-9._-]{1,128}$/;

type LegacyAudioFile = Record<string, unknown> & {
  id: string;
  chunk_timestamps: number[];
};

type LegacySourceKind = "pcm" | "opus";

type LegacySource = {
  key: string;
  size: number;
  start: number;
  end: number;
  batch: boolean;
  encrypted: boolean;
  kind: LegacySourceKind;
  priority: number;
};

type EncryptedFrame = {
  payloadOffset: number;
  payloadLength: number;
  plaintextLength: number;
};

type PlannedSource = LegacySource & {
  pcmBytes: number;
  gapBytes: number;
};

type ConversationRow = {
  created_at: number;
  updated_at: number | null;
  started_at: number | null;
  is_locked: number;
  audio_files_json: string;
  conversation_audio_json: string | null;
};

export type LegacyAudioReadiness = {
  audioFileCount: number;
  readyAudioFileCount: number;
  denseReady: boolean;
};

export type LegacyAudioRebuildResult = {
  conversation_id: string;
  audio_files_fingerprint: string;
  audio_file_count: number;
  rebuilt_audio_file_count: number;
  unavailable_audio_file_ids: string[];
  dense_storage_key: string;
};

export class LegacyAudioSourceError extends Error {}

export function isLegacyAudioPathSegment(value: string): boolean {
  return (
    value.length > 0 &&
    value.length <= 128 &&
    value !== "." &&
    value !== ".." &&
    !value.includes("/") &&
    !value.includes("\\")
  );
}

function copiedArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return copy.buffer;
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function jsonArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (typeof value !== "string") return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function jsonObject(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value))
    return value as Record<string, unknown>;
  if (typeof value !== "string") return {};
  try {
    return objectValue(JSON.parse(value)) || {};
  } catch {
    return {};
  }
}

function normalizedTimestamp(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0
    ? Math.round(parsed * 1_000) / 1_000
    : null;
}

export function legacyAudioFiles(value: unknown): LegacyAudioFile[] {
  return jsonArray(value)
    .slice(0, MAX_AUDIO_FILES)
    .map((entry) => {
      const file = objectValue(entry);
      const id = typeof file?.id === "string" ? file.id : "";
      const timestamps = Array.isArray(file?.chunk_timestamps)
        ? file.chunk_timestamps
            .map(normalizedTimestamp)
            .filter((timestamp): timestamp is number => timestamp !== null)
        : [];
      if (!SAFE_AUDIO_FILE_ID.test(id) || !timestamps.length) return null;
      return {
        ...file,
        id,
        chunk_timestamps: [...new Set(timestamps)].sort(
          (left, right) => left - right,
        ),
      } as LegacyAudioFile;
    })
    .filter((file): file is LegacyAudioFile => file !== null);
}

function stableFingerprintRows(audioFiles: LegacyAudioFile[]): unknown[] {
  return audioFiles
    .map((file) => [
      file.id,
      file.chunk_timestamps.length,
      file.chunk_timestamps.at(-1),
      file.chunk_timestamps[0],
    ])
    .sort((left, right) => String(left[0]).localeCompare(String(right[0])));
}

export async function legacyAudioFilesFingerprint(
  audioFiles: LegacyAudioFile[],
): Promise<string> {
  const bytes = new TextEncoder().encode(
    JSON.stringify(stableFingerprintRows(audioFiles)),
  );
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

/** Match the twelve-character fingerprint embedded by the legacy v2 task. */
export async function legacyAudioFilesLegacyFingerprint(
  audioFiles: LegacyAudioFile[],
): Promise<string> {
  const rows = audioFiles
    .map((file) => [
      file.id,
      file.chunk_timestamps.length,
      Math.round(file.chunk_timestamps.at(-1)! * 1_000) / 1_000,
    ])
    .sort((left, right) => String(left[0]).localeCompare(String(right[0])));
  const digest = await crypto.subtle.digest(
    "SHA-1",
    new TextEncoder().encode(JSON.stringify(rows)),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  )
    .join("")
    .slice(0, 12);
}

function parseLegacySource(
  prefix: string,
  object: { key: string; size: number },
): LegacySource | null {
  if (!object.key.startsWith(prefix) || object.key.length > 512) return null;
  const filename = object.key.slice(prefix.length);
  if (!filename || filename.includes("/") || object.size <= 0) return null;
  const formats = [
    {
      suffix: ".batch.enc",
      batch: true,
      encrypted: true,
      kind: "pcm",
      priority: 0,
    },
    {
      suffix: ".batch.bin",
      batch: true,
      encrypted: false,
      kind: "pcm",
      priority: 1,
    },
    {
      suffix: ".opus.enc",
      batch: false,
      encrypted: true,
      kind: "opus",
      priority: 0,
    },
    { suffix: ".enc", batch: false, encrypted: true, kind: "pcm", priority: 1 },
    {
      suffix: ".opus",
      batch: false,
      encrypted: false,
      kind: "opus",
      priority: 2,
    },
    {
      suffix: ".bin",
      batch: false,
      encrypted: false,
      kind: "pcm",
      priority: 3,
    },
  ] as const;
  const format = formats.find((candidate) =>
    filename.endsWith(candidate.suffix),
  );
  if (!format) return null;
  const stem = filename.slice(0, -format.suffix.length);
  const [startText, endText] = format.batch ? stem.split("-", 2) : [stem, stem];
  const start = normalizedTimestamp(startText);
  const end = normalizedTimestamp(endText || startText);
  if (start === null || end === null || end < start) return null;
  return {
    key: object.key,
    size: object.size,
    start,
    end,
    batch: format.batch,
    encrypted: format.encrypted,
    kind: format.kind,
    priority: format.priority,
  };
}

async function listLegacySources(
  env: JobsEnv,
  uid: string,
  conversationId: string,
): Promise<LegacySource[]> {
  const prefix = `chunks/${uid}/${conversationId}/`;
  const sources: LegacySource[] = [];
  let cursor: string | undefined;
  do {
    const page = await env.ASSETS.list({ prefix, cursor, limit: 1_000 });
    for (const object of page.objects) {
      const source = parseLegacySource(prefix, object);
      if (source) sources.push(source);
      if (sources.length > MAX_LEGACY_OBJECTS)
        throw new LegacyAudioSourceError("legacy audio inventory is too large");
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return sources;
}

function selectLegacySources(
  inventory: LegacySource[],
  timestamps: number[],
): LegacySource[] {
  const individuals = new Map<number, LegacySource>();
  const batches = inventory
    .filter((source) => source.batch)
    .sort(
      (left, right) =>
        left.start - right.start ||
        left.end - right.end ||
        left.priority - right.priority ||
        left.key.localeCompare(right.key),
    );
  for (const source of inventory.filter((candidate) => !candidate.batch)) {
    const current = individuals.get(source.start);
    if (
      !current ||
      source.priority < current.priority ||
      (source.priority === current.priority && source.key < current.key)
    ) {
      individuals.set(source.start, source);
    }
  }

  const selected = new Map<string, LegacySource>();
  for (const timestamp of timestamps) {
    const batch = batches.find(
      (candidate) => candidate.start <= timestamp && timestamp <= candidate.end,
    );
    const source = batch || individuals.get(timestamp);
    if (source) selected.set(source.key, source);
  }
  return [...selected.values()].sort(
    (left, right) =>
      left.start - right.start || left.key.localeCompare(right.key),
  );
}

async function rangeBytes(
  env: JobsEnv,
  key: string,
  offset: number,
  length: number,
): Promise<Uint8Array> {
  if (
    !Number.isSafeInteger(offset) ||
    !Number.isSafeInteger(length) ||
    length <= 0
  )
    throw new LegacyAudioSourceError("legacy audio range is invalid");
  const object = await env.ASSETS.get(key, { range: { offset, length } });
  if (!object)
    throw new LegacyAudioSourceError("legacy audio source is missing");
  const bytes = new Uint8Array(await object.arrayBuffer());
  if (bytes.byteLength !== length)
    throw new LegacyAudioSourceError(
      "legacy audio source changed during rebuild",
    );
  return bytes;
}

async function fullObjectBytes(
  env: JobsEnv,
  source: LegacySource,
): Promise<Uint8Array> {
  if (source.size > MAX_LEGACY_OPUS_OBJECT_BYTES)
    throw new LegacyAudioSourceError("legacy Opus object is too large");
  return rangeBytes(env, source.key, 0, source.size);
}

async function encryptedFrames(
  env: JobsEnv,
  source: LegacySource,
): Promise<EncryptedFrame[]> {
  const frames: EncryptedFrame[] = [];
  let offset = 0;
  while (offset < source.size) {
    if (frames.length >= MAX_ENCRYPTED_FRAMES || source.size - offset < 4)
      throw new LegacyAudioSourceError(
        "legacy encrypted audio framing is invalid",
      );
    const header = await rangeBytes(env, source.key, offset, 4);
    const payloadLength = new DataView(
      header.buffer,
      header.byteOffset,
      header.byteLength,
    ).getUint32(0, false);
    if (
      payloadLength < 28 ||
      payloadLength > MAX_ENCRYPTED_FRAME_BYTES ||
      offset + 4 + payloadLength > source.size
    ) {
      throw new LegacyAudioSourceError(
        "legacy encrypted audio frame is invalid",
      );
    }
    frames.push({
      payloadOffset: offset + 4,
      payloadLength,
      plaintextLength: payloadLength - 28,
    });
    offset += 4 + payloadLength;
  }
  if (offset !== source.size)
    throw new LegacyAudioSourceError("legacy encrypted audio is truncated");
  return frames;
}

async function legacyEncryptionKey(
  secret: string,
  uid: string,
): Promise<CryptoKey> {
  if (new TextEncoder().encode(secret).byteLength < 32)
    throw new LegacyAudioSourceError(
      "legacy audio encryption is not configured",
    );
  const base = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    "HKDF",
    false,
    ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    {
      name: "HKDF",
      hash: "SHA-256",
      salt: new TextEncoder().encode(uid),
      info: new TextEncoder().encode("user-data-encryption"),
    },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["decrypt"],
  );
}

async function decryptFrame(
  env: JobsEnv,
  source: LegacySource,
  frame: EncryptedFrame,
  key: CryptoKey,
): Promise<Uint8Array> {
  const payload = await rangeBytes(
    env,
    source.key,
    frame.payloadOffset,
    frame.payloadLength,
  );
  try {
    const clear = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: copiedArrayBuffer(payload.subarray(0, 12)) },
      key,
      copiedArrayBuffer(payload.subarray(12)),
    );
    if (clear.byteLength !== frame.plaintextLength)
      throw new Error("unexpected plaintext length");
    return new Uint8Array(clear);
  } catch {
    throw new LegacyAudioSourceError(
      "legacy encrypted audio authentication failed",
    );
  }
}

async function decryptedObject(
  env: JobsEnv,
  source: LegacySource,
  key: CryptoKey,
): Promise<Uint8Array> {
  const frames = await encryptedFrames(env, source);
  const size = frames.reduce(
    (total, frame) => total + frame.plaintextLength,
    0,
  );
  if (size > MAX_LEGACY_OPUS_OBJECT_BYTES)
    throw new LegacyAudioSourceError(
      "legacy encrypted Opus object is too large",
    );
  const output = new Uint8Array(size);
  let offset = 0;
  for (const frame of frames) {
    const clear = await decryptFrame(env, source, frame, key);
    output.set(clear, offset);
    offset += clear.byteLength;
  }
  return output;
}

async function decodedOpus(
  env: JobsEnv,
  source: LegacySource,
  key: CryptoKey | null,
): Promise<Uint8Array> {
  if (source.encrypted && !key)
    throw new LegacyAudioSourceError(
      "legacy audio encryption is not configured",
    );
  const encoded = source.encrypted
    ? await decryptedObject(env, source, key as CryptoKey)
    : await fullObjectBytes(env, source);
  const decoded = await decodeLegacyOpusContainer(copiedArrayBuffer(encoded));
  return decoded.byteLength % 2 === 0
    ? decoded
    : decoded.subarray(0, decoded.byteLength - 1);
}

async function sourcePcmBytes(
  env: JobsEnv,
  source: LegacySource,
  key: CryptoKey | null,
): Promise<number> {
  if (source.kind === "opus")
    return (await decodedOpus(env, source, key)).byteLength;
  if (!source.encrypted) return source.size - (source.size % 2);
  const frames = await encryptedFrames(env, source);
  const total = frames.reduce((sum, frame) => sum + frame.plaintextLength, 0);
  return total - (total % 2);
}

async function* bodyChunks(object: R2ObjectBody): AsyncGenerator<Uint8Array> {
  const body = object.body;
  if (!body) {
    yield new Uint8Array(await object.arrayBuffer());
    return;
  }
  const reader = body.getReader();
  try {
    while (true) {
      const result = await reader.read();
      if (result.done) return;
      if (result.value.byteLength) yield result.value;
    }
  } finally {
    reader.releaseLock();
  }
}

async function* pcmChunks(
  env: JobsEnv,
  source: PlannedSource,
  key: CryptoKey | null,
): AsyncGenerator<Uint8Array> {
  let remaining = source.pcmBytes;
  if (source.kind === "opus") {
    const decoded = await decodedOpus(env, source, key);
    if (decoded.byteLength !== remaining)
      throw new LegacyAudioSourceError(
        "legacy Opus output changed during rebuild",
      );
    yield decoded;
    return;
  }
  if (source.encrypted) {
    if (!key)
      throw new LegacyAudioSourceError(
        "legacy audio encryption is not configured",
      );
    for (const frame of await encryptedFrames(env, source)) {
      if (!remaining) break;
      const clear = await decryptFrame(env, source, frame, key);
      const emitted = clear.subarray(0, Math.min(remaining, clear.byteLength));
      if (emitted.byteLength) yield emitted;
      remaining -= emitted.byteLength;
    }
  } else {
    const object = await env.ASSETS.get(source.key, {
      range: { offset: 0, length: source.pcmBytes },
    });
    if (!object)
      throw new LegacyAudioSourceError("legacy audio source is missing");
    for await (const chunk of bodyChunks(object)) {
      if (!remaining) break;
      const emitted = chunk.subarray(0, Math.min(remaining, chunk.byteLength));
      if (emitted.byteLength) yield emitted;
      remaining -= emitted.byteLength;
    }
  }
  if (remaining)
    throw new LegacyAudioSourceError(
      "legacy audio source changed during rebuild",
    );
}

async function* zeros(length: number): AsyncGenerator<Uint8Array> {
  let remaining = length;
  while (remaining > 0) {
    const size = Math.min(remaining, ZERO_CHUNK.byteLength);
    yield ZERO_CHUNK.subarray(0, size);
    remaining -= size;
  }
}

function streamFromGenerator(
  generator: AsyncGenerator<Uint8Array>,
): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const next = await generator.next();
        if (next.done) controller.close();
        else controller.enqueue(next.value);
      } catch (error) {
        controller.error(error);
      }
    },
    async cancel() {
      await generator.return(undefined);
    },
  });
}

async function planSources(
  env: JobsEnv,
  sources: LegacySource[],
  key: CryptoKey | null,
): Promise<{ planned: PlannedSource[]; totalPcmBytes: number }> {
  const planned: PlannedSource[] = [];
  let currentTime: number | null = null;
  let totalPcmBytes = 0;
  for (const source of sources) {
    const pcmBytes = await sourcePcmBytes(env, source, key);
    if (!pcmBytes) continue;
    const gapSeconds =
      currentTime === null ? 0 : Math.max(0, source.start - currentTime);
    const gapBytes =
      Math.floor(gapSeconds * PLAYBACK_SAMPLE_RATE) * PLAYBACK_CHANNELS * 2;
    totalPcmBytes += gapBytes + pcmBytes;
    if (totalPcmBytes > MAX_WAV_PCM_BYTES)
      throw new LegacyAudioSourceError(
        "legacy playback WAV exceeds RIFF limits",
      );
    planned.push({ ...source, pcmBytes, gapBytes });
    currentTime = source.start + pcmBytes / PCM_BYTES_PER_SECOND;
  }
  return { planned, totalPcmBytes };
}

/** Materialize one bounded canonical PCM artifact before MP3 encoding. */
async function collectPcm(
  env: JobsEnv,
  planned: PlannedSource[],
  totalPcmBytes: number,
  key: CryptoKey | null,
): Promise<Uint8Array> {
  if (
    !Number.isSafeInteger(totalPcmBytes) ||
    totalPcmBytes <= 0 ||
    totalPcmBytes > MAX_MP3_PCM_BYTES ||
    totalPcmBytes % 2 !== 0
  ) {
    throw new LegacyAudioSourceError("legacy playback MP3 is too large");
  }
  const output = new Uint8Array(totalPcmBytes);
  let offset = 0;
  const append = (chunk: Uint8Array): void => {
    if (chunk.byteLength > output.byteLength - offset)
      throw new LegacyAudioSourceError("legacy playback PCM length changed");
    output.set(chunk, offset);
    offset += chunk.byteLength;
  };
  for (const source of planned) {
    if (source.gapBytes) {
      const gap = Math.min(source.gapBytes, output.byteLength - offset);
      output.fill(0, offset, offset + gap);
      offset += gap;
    }
    for await (const chunk of pcmChunks(env, source, key)) append(chunk);
  }
  if (offset !== totalPcmBytes)
    throw new LegacyAudioSourceError("legacy playback PCM length changed");
  return output;
}

async function collectFilePcm(
  env: JobsEnv,
  inventory: LegacySource[],
  sourceFile: LegacyAudioFile,
  key: CryptoKey | null,
): Promise<{ pcm: Uint8Array; startedAt: number } | null> {
  const sources = selectLegacySources(inventory, sourceFile.chunk_timestamps);
  if (!sources.length) return null;
  const { planned, totalPcmBytes } = await planSources(env, sources, key);
  if (!planned.length || !totalPcmBytes) return null;
  return {
    pcm: await collectPcm(env, planned, totalPcmBytes, key),
    startedAt: planned[0].start,
  };
}

async function storeLegacyPlaybackFile(
  env: JobsEnv,
  job: PlaybackJob,
  conversationId: string,
  sourceFile: LegacyAudioFile,
  inventory: LegacySource[],
  key: CryptoKey | null,
  now: number,
): Promise<PlaybackAudioFile | null> {
  const sources = selectLegacySources(inventory, sourceFile.chunk_timestamps);
  if (!sources.length) return null;
  const { planned, totalPcmBytes } = await planSources(env, sources, key);
  if (!planned.length || !totalPcmBytes) return null;
  const storageKey = `sync-playback/${job.uid}/${conversationId}/${sourceFile.id}.wav`;
  await recordPlaybackIntent(
    env,
    job,
    conversationId,
    sourceFile.id,
    storageKey,
    now,
  );

  async function* wavChunks(): AsyncGenerator<Uint8Array> {
    yield new Uint8Array(
      pcm16WavHeader(totalPcmBytes, PLAYBACK_SAMPLE_RATE, PLAYBACK_CHANNELS),
    );
    for (const source of planned) {
      if (source.gapBytes) yield* zeros(source.gapBytes);
      yield* pcmChunks(env, source, key);
    }
  }
  const body = fixedLengthPlaybackStream(
    streamFromGenerator(wavChunks()),
    totalPcmBytes + 44,
  );
  await Promise.all([
    env.ASSETS.put(storageKey, body.readable, {
      httpMetadata: { contentType: "audio/wav" },
      customMetadata: {
        uid: job.uid,
        conversationId,
        audioFileId: sourceFile.id,
        sampleRate: String(PLAYBACK_SAMPLE_RATE),
        channels: String(PLAYBACK_CHANNELS),
        importedFrom: "legacy-gcs-chunks",
      },
    }),
    body.completed,
  ]);
  await markPlaybackStored(env, job.uid, storageKey, now);

  const startedAt = planned[0].start;
  return {
    ...sourceFile,
    id: sourceFile.id,
    provider: "cloudflare-r2",
    started_at: new Date(startedAt * 1_000).toISOString(),
    chunk_timestamps: sourceFile.chunk_timestamps,
    duration: totalPcmBytes / PCM_BYTES_PER_SECOND,
    storage_key: storageKey,
    content_type: "audio/wav",
    sample_rate: PLAYBACK_SAMPLE_RATE,
    channels: PLAYBACK_CHANNELS,
    pcm_bytes: totalPcmBytes,
  } as PlaybackAudioFile;
}

function playbackCandidates(
  uid: string,
  conversationId: string,
  id: string,
  explicit: unknown,
): string[] {
  const prefix = `sync-playback/${uid}/${conversationId}/`;
  const candidates = [
    `${prefix}${id}.wav`,
    `playback/${uid}/${conversationId}/${id}.mp3`,
    `merged/${uid}/${conversationId}/${id}.wav`,
  ];
  if (
    typeof explicit === "string" &&
    (explicit.startsWith(prefix) ||
      explicit.startsWith(`playback/${uid}/${conversationId}/`) ||
      explicit.startsWith(`merged/${uid}/${conversationId}/`))
  ) {
    candidates.unshift(explicit);
  }
  return [...new Set(candidates)];
}

async function anyObject(env: JobsEnv, keys: string[]): Promise<boolean> {
  for (const key of keys) {
    if (await env.ASSETS.head(key)) return true;
  }
  return false;
}

export async function legacyAudioReadiness(
  env: JobsEnv,
  uid: string,
  conversationId: string,
  audioFiles: LegacyAudioFile[],
  conversationAudioValue: unknown,
): Promise<LegacyAudioReadiness> {
  let readyAudioFileCount = 0;
  for (const file of audioFiles) {
    if (
      await anyObject(
        env,
        playbackCandidates(uid, conversationId, file.id, file.storage_key),
      )
    ) {
      readyAudioFileCount += 1;
    }
  }
  const conversationAudio = jsonObject(conversationAudioValue);
  const denseReady =
    Object.keys(conversationAudio).length > 0 &&
    (await anyObject(
      env,
      playbackCandidates(
        uid,
        conversationId,
        "conversation",
        conversationAudio.storage_key,
      ),
    ));
  return {
    audioFileCount: audioFiles.length,
    readyAudioFileCount,
    denseReady,
  };
}

async function commitPlaybackObjects(
  env: JobsEnv,
  uid: string,
  storageKeys: string[],
  revision: number,
): Promise<void> {
  await Promise.all(
    storageKeys.map((storageKey) =>
      env.APP_DB.prepare(
        "UPDATE cf_sync_playback_objects SET state = 'committed', updated_at = ? " +
          "WHERE uid = ? AND storage_key = ?",
      )
        .bind(revision, uid, storageKey)
        .run(),
    ),
  );
}

export async function rebuildLegacyConversationAudio(
  env: JobsEnv,
  job: PlaybackJob,
  conversationId: string,
  expectedFingerprint: string,
  now: number,
): Promise<LegacyAudioRebuildResult> {
  if (
    !isLegacyAudioPathSegment(job.uid) ||
    !isLegacyAudioPathSegment(conversationId)
  )
    throw new LegacyAudioSourceError("legacy audio identity is invalid");
  if (!(await recordingStorageEnabled(env, job.uid)))
    throw new LegacyAudioSourceError("recording storage is disabled");
  const initial = await env.APP_DB.prepare(
    "SELECT created_at, updated_at, started_at, is_locked, audio_files_json, conversation_audio_json " +
      "FROM cf_conversations WHERE uid = ? AND id = ?",
  )
    .bind(job.uid, conversationId)
    .first<ConversationRow>();
  if (!initial)
    throw new LegacyAudioSourceError("conversation no longer exists");
  if (initial.is_locked)
    throw new LegacyAudioSourceError(
      "locked conversation cannot rebuild audio",
    );
  const sourceFiles = legacyAudioFiles(initial.audio_files_json);
  if (!sourceFiles.length)
    throw new LegacyAudioSourceError(
      "conversation has no legacy audio metadata",
    );
  if ((await legacyAudioFilesFingerprint(sourceFiles)) !== expectedFingerprint)
    throw new LegacyAudioSourceError(
      "conversation audio changed before rebuild",
    );

  const inventory = await listLegacySources(env, job.uid, conversationId);
  if (!inventory.length)
    throw new LegacyAudioSourceError(
      "legacy audio chunks are not available in R2",
    );
  const needsEncryption = inventory.some((source) => source.encrypted);
  const encryptionKey = needsEncryption
    ? await legacyEncryptionKey(
        env.LEGACY_AUDIO_ENCRYPTION_SECRET || "",
        job.uid,
      )
    : null;

  const playback: PlaybackAudioFile[] = [];
  const unavailableAudioFileIds: string[] = [];
  for (const sourceFile of sourceFiles) {
    const stored = await storeLegacyPlaybackFile(
      env,
      job,
      conversationId,
      sourceFile,
      inventory,
      encryptionKey,
      now,
    );
    if (stored) playback.push(stored);
    else unavailableAudioFileIds.push(sourceFile.id);
  }
  if (!playback.length)
    throw new LegacyAudioSourceError(
      "legacy audio chunks do not match conversation metadata",
    );

  const conversationAudio = await buildConversationPlayback(
    env,
    job,
    conversationId,
    initial.started_at ?? initial.created_at,
    playback,
    now,
  );
  if (!conversationAudio)
    throw new LegacyAudioSourceError(
      "legacy conversation artifact could not be built",
    );
  const encodedConversationAudio = JSON.stringify(conversationAudio);
  if (
    new TextEncoder().encode(encodedConversationAudio).byteLength >
    MAX_CONVERSATION_AUDIO_JSON_BYTES
  ) {
    throw new LegacyAudioSourceError(
      "legacy conversation artifact metadata is too large",
    );
  }

  let committedRevision: number | null = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const row =
      attempt === 0
        ? initial
        : await env.APP_DB.prepare(
            "SELECT created_at, updated_at, started_at, is_locked, audio_files_json, conversation_audio_json " +
              "FROM cf_conversations WHERE uid = ? AND id = ?",
          )
            .bind(job.uid, conversationId)
            .first<ConversationRow>();
    if (!row) throw new LegacyAudioSourceError("conversation no longer exists");
    const currentFiles = legacyAudioFiles(row.audio_files_json);
    if (
      (await legacyAudioFilesFingerprint(currentFiles)) !== expectedFingerprint
    )
      throw new LegacyAudioSourceError(
        "conversation audio changed during rebuild",
      );
    const replacements = new Map(playback.map((file) => [file.id, file]));
    const merged = jsonArray(row.audio_files_json).map((value) => {
      const current = objectValue(value);
      const id = typeof current?.id === "string" ? current.id : "";
      return replacements.get(id) || value;
    });
    const encodedFiles = JSON.stringify(merged);
    if (
      new TextEncoder().encode(encodedFiles).byteLength >
      MAX_CONVERSATION_AUDIO_JSON_BYTES
    ) {
      throw new LegacyAudioSourceError("legacy playback metadata is too large");
    }
    const revision = row.updated_at ?? row.created_at;
    const nextRevision = Math.max(now, revision + 1);
    const updated = await env.APP_DB.prepare(
      "UPDATE cf_conversations SET updated_at = ?, audio_files_json = ?, conversation_audio_json = ? " +
        "WHERE uid = ? AND id = ? AND COALESCE(updated_at, created_at) = ? RETURNING id",
    )
      .bind(
        nextRevision,
        encodedFiles,
        encodedConversationAudio,
        job.uid,
        conversationId,
        revision,
      )
      .run<{ id: string }>();
    if (updated.results?.[0]?.id === conversationId) {
      committedRevision = nextRevision;
      break;
    }
  }
  if (committedRevision === null)
    throw new Error("legacy playback metadata changed concurrently");

  await commitPlaybackObjects(
    env,
    job.uid,
    [
      ...playback.map((file) => file.storage_key),
      conversationAudio.storage_key,
    ],
    committedRevision,
  );
  return {
    conversation_id: conversationId,
    audio_files_fingerprint: expectedFingerprint,
    audio_file_count: sourceFiles.length,
    rebuilt_audio_file_count: playback.length,
    unavailable_audio_file_ids: unavailableAudioFileIds,
    dense_storage_key: conversationAudio.storage_key,
  };
}

export type LegacyMp3RebuildResult = {
  conversation_id: string;
  audio_file_id: string;
  content_type: "audio/mpeg";
  storage_key: string;
  bytes: number;
};

type LegacyMp3Inputs = {
  row: ConversationRow;
  sourceFiles: LegacyAudioFile[];
  inventory: LegacySource[];
  encryptionKey: CryptoKey | null;
};

async function legacyMp3Inputs(
  env: JobsEnv,
  job: PlaybackJob,
  conversationId: string,
  expectedFingerprint: string | null,
): Promise<LegacyMp3Inputs> {
  if (
    !isLegacyAudioPathSegment(job.uid) ||
    !isLegacyAudioPathSegment(conversationId)
  )
    throw new LegacyAudioSourceError("legacy audio identity is invalid");
  if (!(await recordingStorageEnabled(env, job.uid)))
    throw new LegacyAudioSourceError("recording storage is disabled");
  const row = await env.APP_DB.prepare(
    "SELECT created_at, updated_at, started_at, is_locked, audio_files_json, conversation_audio_json " +
      "FROM cf_conversations WHERE uid = ? AND id = ?",
  )
    .bind(job.uid, conversationId)
    .first<ConversationRow>();
  if (!row) throw new LegacyAudioSourceError("conversation no longer exists");
  if (row.is_locked)
    throw new LegacyAudioSourceError(
      "locked conversation cannot rebuild audio",
    );
  const sourceFiles = legacyAudioFiles(row.audio_files_json);
  if (!sourceFiles.length)
    throw new LegacyAudioSourceError(
      "conversation has no legacy audio metadata",
    );
  if (expectedFingerprint) {
    const modernFingerprint = await legacyAudioFilesFingerprint(sourceFiles);
    const legacyFingerprint = await legacyAudioFilesLegacyFingerprint(sourceFiles);
    if (
      expectedFingerprint !== modernFingerprint &&
      expectedFingerprint !== legacyFingerprint
    )
      throw new LegacyAudioSourceError(
        "conversation audio changed before rebuild",
      );
  }
  const inventory = await listLegacySources(env, job.uid, conversationId);
  if (!inventory.length)
    throw new LegacyAudioSourceError(
      "legacy audio chunks are not available in R2",
    );
  const needsEncryption = inventory.some((source) => source.encrypted);
  const encryptionKey = needsEncryption
    ? await legacyEncryptionKey(
        env.LEGACY_AUDIO_ENCRYPTION_SECRET || "",
        job.uid,
      )
    : null;
  return { row, sourceFiles, inventory, encryptionKey };
}

function validLegacyTimestamps(value: unknown): value is number[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.length <= MAX_ENCRYPTED_FRAMES &&
    value.every(
      (timestamp) =>
        typeof timestamp === "number" &&
        Number.isFinite(timestamp) &&
        timestamp > 0 &&
        timestamp <= 4_102_444_800,
    )
  );
}

/** Build the legacy schema-v1 per-file MP3 artifact from R2 chunks. */
export async function rebuildLegacyAudioFileMp3(
  env: JobsEnv,
  job: PlaybackJob,
  conversationId: string,
  audioFileId: string,
  timestamps: number[],
  now: number,
): Promise<LegacyMp3RebuildResult> {
  if (!isLegacyAudioPathSegment(audioFileId) || !validLegacyTimestamps(timestamps))
    throw new LegacyAudioSourceError("legacy audio file payload is invalid");
  const { sourceFiles, inventory, encryptionKey } = await legacyMp3Inputs(
    env,
    job,
    conversationId,
    null,
  );
  const sourceFile = sourceFiles.find((file) => file.id === audioFileId);
  if (!sourceFile)
    throw new LegacyAudioSourceError("legacy audio file is not in conversation");
  const source = await collectFilePcm(
    env,
    inventory,
    {
      ...sourceFile,
      chunk_timestamps: [...new Set(timestamps)].sort(
        (left, right) => left - right,
      ),
    },
    encryptionKey,
  );
  if (!source)
    throw new LegacyAudioSourceError(
      "legacy audio chunks do not match requested timestamps",
    );
  const encoded = await encodePcm16MonoToMp3(source.pcm);
  const storageKey = `playback/${job.uid}/${conversationId}/${audioFileId}.mp3`;
  await recordPlaybackIntent(
    env,
    job,
    conversationId,
    audioFileId,
    storageKey,
    now,
  );
  await env.ASSETS.put(storageKey, encoded, {
    httpMetadata: { contentType: "audio/mpeg" },
    customMetadata: {
      uid: job.uid,
      conversationId,
      audioFileId,
      sampleRate: String(PLAYBACK_SAMPLE_RATE),
      channels: String(PLAYBACK_CHANNELS),
      bitrate: "48",
      importedFrom: "legacy-gcs-chunks",
    },
  });
  await markPlaybackStored(env, job.uid, storageKey, now);
  return {
    conversation_id: conversationId,
    audio_file_id: audioFileId,
    content_type: "audio/mpeg",
    storage_key: storageKey,
    bytes: encoded.byteLength,
  };
}

/** Build the legacy schema-v2 dense MP3 and atomically stamp its D1 metadata. */
export async function rebuildLegacyConversationMp3(
  env: JobsEnv,
  job: PlaybackJob,
  conversationId: string,
  expectedFingerprint: string | null,
  now: number,
): Promise<LegacyAudioRebuildResult> {
  const { row, sourceFiles, inventory, encryptionKey } =
    await legacyMp3Inputs(env, job, conversationId, expectedFingerprint);
  const parts: Array<{ id: string; pcm: Uint8Array; startedAt: number }> = [];
  for (const sourceFile of sourceFiles) {
    const part = await collectFilePcm(
      env,
      inventory,
      sourceFile,
      encryptionKey,
    );
    if (part)
      parts.push({ id: sourceFile.id, pcm: part.pcm, startedAt: part.startedAt });
  }
  if (!parts.length)
    throw new LegacyAudioSourceError(
      "legacy audio chunks do not match conversation metadata",
    );
  const totalPcmBytes = parts.reduce((total, part) => total + part.pcm.byteLength, 0);
  if (totalPcmBytes > MAX_MP3_PCM_BYTES)
    throw new LegacyAudioSourceError("legacy conversation MP3 is too large");
  const pcm = new Uint8Array(totalPcmBytes);
  let pcmOffset = 0;
  let artifactOffset = 0;
  const startedAt = row.started_at ?? row.created_at;
  const spans = parts.map((part) => {
    pcm.set(part.pcm, pcmOffset);
    pcmOffset += part.pcm.byteLength;
    const length = part.pcm.byteLength / PCM_BYTES_PER_SECOND;
    const span = {
      file_id: part.id,
      wall_offset:
        Math.round(Math.max(0, part.startedAt - startedAt) * 1_000) / 1_000,
      artifact_offset: Math.round(artifactOffset * 1_000) / 1_000,
      len: Math.round(length * 1_000) / 1_000,
    };
    artifactOffset += length;
    return span;
  });
  const encoded = await encodePcm16MonoToMp3(pcm);
  const storageKey = `playback/${job.uid}/${conversationId}/conversation.mp3`;
  await recordPlaybackIntent(
    env,
    job,
    conversationId,
    "conversation",
    storageKey,
    now,
  );
  await env.ASSETS.put(storageKey, encoded, {
    httpMetadata: { contentType: "audio/mpeg" },
    customMetadata: {
      uid: job.uid,
      conversationId,
      audioFileId: "conversation",
      sampleRate: String(PLAYBACK_SAMPLE_RATE),
      channels: String(PLAYBACK_CHANNELS),
      bitrate: "48",
      importedFrom: "legacy-gcs-chunks",
    },
  });
  await markPlaybackStored(env, job.uid, storageKey, now);
  const currentFingerprint = await legacyAudioFilesLegacyFingerprint(sourceFiles);
  const conversationAudio = {
    audio_files_fingerprint: currentFingerprint,
    duration:
      Math.round((spans.at(-1)!.wall_offset + spans.at(-1)!.len) * 1_000) /
      1_000,
    captured_duration: Math.round(artifactOffset * 1_000) / 1_000,
    spans,
    content_type: "audio/mpeg",
    storage_key: storageKey,
    built_at: now,
  };
  const encodedConversationAudio = JSON.stringify(conversationAudio);
  if (
    new TextEncoder().encode(encodedConversationAudio).byteLength >
    MAX_CONVERSATION_AUDIO_JSON_BYTES
  )
    throw new LegacyAudioSourceError(
      "legacy conversation artifact metadata is too large",
    );
  const revision = row.updated_at ?? row.created_at;
  const nextRevision = Math.max(now, revision + 1);
  const updated = await env.APP_DB.prepare(
    "UPDATE cf_conversations SET updated_at = ?, conversation_audio_json = ? " +
      "WHERE uid = ? AND id = ? AND COALESCE(updated_at, created_at) = ? " +
      "AND NOT EXISTS (SELECT 1 FROM cf_recording_deletion_intents WHERE uid = ?)",
  )
    .bind(
      nextRevision,
      encodedConversationAudio,
      job.uid,
      conversationId,
      revision,
      job.uid,
    )
    .run();
  if (updated.meta?.changes !== 1)
    throw new Error("legacy conversation metadata changed concurrently");
  await commitPlaybackObjects(env, job.uid, [storageKey], nextRevision);
  return {
    conversation_id: conversationId,
    audio_files_fingerprint: currentFingerprint,
    audio_file_count: sourceFiles.length,
    rebuilt_audio_file_count: parts.length,
    unavailable_audio_file_ids: sourceFiles
      .filter((sourceFile) => !parts.some((part) => part.id === sourceFile.id))
      .map((sourceFile) => sourceFile.id),
    dense_storage_key: storageKey,
  };
}
