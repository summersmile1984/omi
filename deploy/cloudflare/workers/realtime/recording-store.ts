export type RecordingSegment = {
  text: string;
  start: number;
  end: number;
  speaker: string;
  is_user?: boolean;
  person_id?: string | null;
};

export type RecordingBinding = {
  uid: string;
  recording_session_id: string;
  conversation_id: string;
  owner_token: string;
  lifecycle_sequence: number;
  transcript_segments_json: string;
};

export class RecordingOwnershipLost extends Error {
  constructor() {
    super("recording ownership is no longer active");
  }
}

export function recordingId(value: string | null): string {
  const candidate = value?.trim().toLowerCase();
  return candidate &&
    /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(candidate)
    ? candidate
    : crypto.randomUUID();
}

// Metadata and transcript writes share D1's transaction authority. The first
// insert token can create a conversation only once; retained session metadata
// is a tombstone when its conversation was deleted, never a recreation permit.
export async function openRecording(
  database: D1Database,
  {
    uid,
    sessionId,
    source,
    language,
  }: {
    uid: string;
    sessionId: string;
    source: string;
    language: string;
  },
): Promise<RecordingBinding> {
  if (
    source.length > 64 ||
    language.length > 35 ||
    !/^[A-Za-z0-9-]+$/.test(language)
  )
    throw new Error("invalid recording metadata");
  let selectedId = sessionId;
  for (let attempt = 0; attempt < 2; attempt++) {
    const token = crypto.randomUUID();
    const now = Math.floor(Date.now() / 1000);
    const results = await database.batch([
      database
        .prepare(
          `INSERT OR IGNORE INTO cf_live_recording_sessions
        (uid, recording_session_id, conversation_id, initial_token, owner_token, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)`,
        )
        .bind(uid, selectedId, selectedId, token, token, now, now),
      database
        .prepare(
          `INSERT OR IGNORE INTO cf_conversations
        (uid, id, created_at, updated_at, started_at, source, language, status, visibility)
        SELECT uid, conversation_id, ?, ?, ?, ?, ?, 'in_progress', 'private'
        FROM cf_live_recording_sessions
        WHERE uid = ? AND recording_session_id = ? AND initial_token = ?`,
        )
        .bind(now, now, now, source, language, uid, selectedId, token),
      database
        .prepare(
          `UPDATE cf_live_recording_sessions
        SET owner_token = ?, lifecycle_sequence = lifecycle_sequence + 1, updated_at = ?
        WHERE uid = ? AND recording_session_id = ? AND EXISTS (
          SELECT 1 FROM cf_conversations c WHERE c.uid = cf_live_recording_sessions.uid
          AND c.id = cf_live_recording_sessions.conversation_id
          AND c.status = 'in_progress' AND c.discarded = 0
        )`,
        )
        .bind(token, now, uid, selectedId),
      database
        .prepare(
          `SELECT s.uid, s.recording_session_id, s.conversation_id,
        s.owner_token, s.lifecycle_sequence, c.transcript_segments_json
        FROM cf_live_recording_sessions s JOIN cf_conversations c ON c.uid = s.uid AND c.id = s.conversation_id
        WHERE s.uid = ? AND s.recording_session_id = ? AND s.owner_token = ?
        AND c.status = 'in_progress' AND c.discarded = 0`,
        )
        .bind(uid, selectedId, token),
    ]);
    const binding = results[3].results?.[0] as RecordingBinding | undefined;
    if (binding) return binding;
    // Terminal/missing old generations roll forward to a fresh immutable ID.
    selectedId = crypto.randomUUID();
  }
  throw new RecordingOwnershipLost();
}

export function mergeRecordingSegments(
  existing: RecordingSegment[],
  incoming: RecordingSegment[],
): RecordingSegment[] {
  const segments = new Map(
    existing.map((segment) => [
      `${segment.start}:${segment.end}:${segment.speaker}`,
      segment,
    ]),
  );
  for (const segment of incoming) {
    if (
      !segment.text.trim() ||
      !Number.isFinite(segment.start) ||
      !Number.isFinite(segment.end) ||
      segment.start < 0 ||
      segment.end <= segment.start
    )
      continue;
    if (segment.text.length > 100000)
      throw new Error("recording segment capacity reached");
    segments.set(`${segment.start}:${segment.end}:${segment.speaker}`, segment);
  }
  const result = [...segments.values()].sort(
    (a, b) => a.start - b.start || a.end - b.end,
  );
  if (
    result.length > 2000 ||
    result.reduce((size, item) => size + item.text.length, 0) > 500000
  )
    throw new Error("recording transcript capacity reached");
  return result;
}

export async function writeRecordingSegments(
  database: D1Database,
  binding: RecordingBinding,
  segments: RecordingSegment[],
): Promise<void> {
  const result = await database
    .prepare(
      `UPDATE cf_conversations
    SET transcript_segments_json = ?, updated_at = ?
    WHERE uid = ? AND id = ? AND status = 'in_progress' AND discarded = 0
    AND EXISTS (SELECT 1 FROM cf_live_recording_sessions s WHERE s.uid = cf_conversations.uid
      AND s.conversation_id = cf_conversations.id AND s.recording_session_id = ? AND s.owner_token = ?) RETURNING id`,
    )
    .bind(
      JSON.stringify(segments),
      Math.floor(Date.now() / 1000),
      binding.uid,
      binding.conversation_id,
      binding.recording_session_id,
      binding.owner_token,
    )
    .first<{ id: string }>();
  if (result?.id !== binding.conversation_id)
    throw new RecordingOwnershipLost();
}

export function recordingSessionEvent(binding: RecordingBinding) {
  return {
    type: "conversation_session",
    status: "in_progress",
    lifecycle_version: 1,
    recording_session_id: binding.recording_session_id,
    conversation_id: binding.conversation_id,
    lifecycle_phase: "in_progress",
    lifecycle_sequence: binding.lifecycle_sequence,
  };
}

export const recordingStore = {
  open: openRecording,
  write: writeRecordingSegments,
};
export type RecordingStore = typeof recordingStore;
