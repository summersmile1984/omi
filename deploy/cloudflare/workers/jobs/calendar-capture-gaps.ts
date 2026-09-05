/** Read-only SCA-381 selection; mirrors upstream calendar_linking.select_capture_gaps. */
export type CaptureConversation = {
  started_at: number | null;
  finished_at: number | null;
  discarded: number;
};

export const CAPTURE_GAP_MAX_EVENTS = 250;
export const CAPTURE_GAP_MAX_CONVERSATIONS = 500;
export const DAY_MS = 86_400_000;

// datetime query values follow FastAPI's UTC convention for zone-less values.
// Validate the calendar day before Date.parse, which otherwise rolls February 30
// into March. Numeric input follows Pydantic's seconds/milliseconds boundary.
export function captureGapTimestamp(value: string | undefined): number | null {
  if (!value) return null;
  if (/^[+-]?\d+(?:\.\d+)?$/.test(value)) {
    const raw = Number(value);
    const result = Math.abs(raw) <= 20_000_000_000 ? raw * 1_000 : raw;
    return Number.isFinite(new Date(result).getTime()) ? result : null;
  }
  const match =
    /^(\d{4})-(\d{2})-(\d{2})(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?([Zz]|[+-]\d{2}:?\d{2})?)?$/.exec(
      value
    );
  if (!match) return null;
  const [, year, month, day, hour, minute, second, fraction, zone] = match;
  const date = `${year}-${month}-${day}`;
  const midnight = new Date(`${date}T00:00:00Z`);
  if (
    !Number.isFinite(midnight.getTime()) ||
    midnight.toISOString().slice(0, 10) !== date
  )
    return null;
  if (hour === undefined) return midnight.getTime();
  if (Number(hour) > 23 || Number(minute) > 59 || Number(second ?? 0) > 59)
    return null;
  const result = Date.parse(
    `${date}T${hour}:${minute}:${second ?? "00"}.${(fraction ?? "0")
      .padEnd(3, "0")
      .slice(0, 3)}${zone?.toUpperCase() ?? "Z"}`
  );
  return Number.isFinite(result) ? result : null;
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function selectCaptureGaps(
  events: unknown[],
  conversations: CaptureConversation[]
) {
  const windows = conversations.filter(
    (row) =>
      !row.discarded &&
      row.started_at !== null &&
      row.finished_at !== null &&
      row.finished_at > row.started_at
  );
  const rows = [];
  for (const raw of events.slice(0, CAPTURE_GAP_MAX_EVENTS)) {
    const event = object(raw);
    if (!event) continue;
    const status =
      event.status === undefined ? "confirmed" : String(event.status);
    if (status.trim().toLowerCase() !== "confirmed") continue;
    if (
      Array.isArray(event.attendees) &&
      event.attendees.some((raw) => {
        const attendee = object(raw);
        return (
          attendee?.self &&
          String(attendee.responseStatus ?? "")
            .trim()
            .toLowerCase() === "declined"
        );
      })
    )
      continue;
    const start = object(event.start)?.dateTime;
    const end = object(event.end)?.dateTime;
    // All-day blocks have date rather than dateTime and never qualify.
    if (typeof start !== "string" || typeof end !== "string") continue;
    const startMs = captureGapTimestamp(start);
    const endMs = captureGapTimestamp(end);
    if (
      startMs === null ||
      endMs === null ||
      endMs <= startMs ||
      endMs - startMs > 8 * 3_600_000
    )
      continue;
    if (
      windows.some(
        (row) =>
          Math.min(endMs, row.finished_at! * 1_000) -
            Math.max(startMs, row.started_at! * 1_000) >=
          10_000
      )
    )
      continue;
    rows.push({
      event_id: String(event.id ?? ""),
      title: String(event.summary ?? "Untitled Event"),
      start_time: new Date(startMs).toISOString(),
      end_time: new Date(endMs).toISOString(),
      status,
      coverage: "not_captured",
    });
  }
  return rows;
}
