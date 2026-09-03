const MAX_SPEECH_MS = 604_800_000;
const MAX_SOURCE_ID_CHARS = 256;

export type FairUseSourceKind =
  "realtime" | "sync_fresh" | "sync_backfill" | "custom_stt";

export type FairUseUsage = {
  uid: string;
  sourceKind: FairUseSourceKind;
  sourceId: string;
  occurredAt: number;
  speechMs: number;
  dgMs?: number;
  revision?: number;
};

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function transcriptionIntervals(value: unknown): Array<[number, number]> {
  const payload =
    value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  const candidates =
    Array.isArray(payload.segments) && payload.segments.length
      ? payload.segments
      : Array.isArray(payload.words)
        ? payload.words
        : [];
  const intervals: Array<[number, number]> = [];
  for (const value of candidates) {
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const segment = value as Record<string, unknown>;
    const text =
      typeof segment.text === "string"
        ? segment.text
        : typeof segment.word === "string"
          ? segment.word
          : "";
    const start = finiteNumber(segment.start);
    const end = finiteNumber(segment.end);
    if (
      !text.trim() ||
      start === null ||
      end === null ||
      start < 0 ||
      end <= start
    )
      continue;
    const startMs = Math.round(start * 1000);
    const endMs = Math.round(end * 1000);
    if (endMs <= startMs || endMs - startMs > MAX_SPEECH_MS) continue;
    intervals.push([startMs, endMs]);
  }
  return intervals;
}

export function speechMsFromTranscription(value: unknown): number {
  const intervals = transcriptionIntervals(value).sort(
    ([left], [right]) => left - right,
  );
  if (!intervals.length) return 0;
  let total = 0;
  let [currentStart, currentEnd] = intervals[0];
  for (const [start, end] of intervals.slice(1)) {
    if (start <= currentEnd) {
      currentEnd = Math.max(currentEnd, end);
      continue;
    }
    total += currentEnd - currentStart;
    [currentStart, currentEnd] = [start, end];
  }
  return Math.min(total + currentEnd - currentStart, MAX_SPEECH_MS);
}

export async function recordFairUseUsage(
  database: D1Database,
  usage: FairUseUsage,
): Promise<void> {
  const dgMs = usage.dgMs ?? 0;
  const revision = usage.revision ?? 1;
  if (
    !usage.uid ||
    !usage.sourceId ||
    usage.sourceId.length > MAX_SOURCE_ID_CHARS
  )
    throw new Error("invalid fair-use usage identity");
  if (
    !Number.isInteger(usage.occurredAt) ||
    usage.occurredAt <= 0 ||
    !Number.isInteger(usage.speechMs) ||
    usage.speechMs <= 0 ||
    usage.speechMs > MAX_SPEECH_MS ||
    !Number.isInteger(dgMs) ||
    dgMs < 0 ||
    dgMs > MAX_SPEECH_MS ||
    !Number.isInteger(revision) ||
    revision < 1 ||
    revision > 2_147_483_647
  ) {
    throw new Error("invalid fair-use usage value");
  }
  await database
    .prepare(
      "INSERT INTO cf_fair_use_usage_sources " +
        "(uid, source_kind, source_id, occurred_at, speech_ms, dg_ms, updated_at, revision) " +
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) " +
        "ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET " +
        "speech_ms = excluded.speech_ms, dg_ms = excluded.dg_ms, " +
        "updated_at = excluded.updated_at, revision = excluded.revision " +
        "WHERE excluded.revision >= cf_fair_use_usage_sources.revision",
    )
    .bind(
      usage.uid,
      usage.sourceKind,
      usage.sourceId,
      usage.occurredAt,
      usage.speechMs,
      dgMs,
      Math.floor(Date.now() / 1000),
      revision,
    )
    .run();
}
