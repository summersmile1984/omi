import { DEFAULT_FAIR_USE_CAPS } from "./fair-use-policy";
import { recordFallback } from "./fallback";

const MAX_DAILY_AUDIO_MS = 108_000_000;
const DEFAULT_RETRY_AFTER_SECONDS = 60 * 60;
const MAX_RETRY_AFTER_SECONDS = 30 * 24 * 60 * 60;
const DAY_SECONDS = 86_400;

type RestrictionRow = {
  stage?: unknown;
  restrict_until?: unknown;
  daily_ms?: unknown;
  three_day_ms?: unknown;
  weekly_ms?: unknown;
};

export type FairUseRestriction = {
  blocked: true;
  reason: "fair_use_restricted" | "daily_audio_ceiling";
  retryAfter: number;
  stage: "none" | "warning" | "throttle" | "restrict";
};

function nonnegativeInteger(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : 0;
}

function boundedRetryAfter(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed)
    ? Math.min(MAX_RETRY_AFTER_SECONDS, Math.max(1, Math.floor(parsed)))
    : DEFAULT_RETRY_AFTER_SECONDS;
}

function nextUtcDay(now: number): number {
  const date = new Date(now * 1000);
  return (
    Math.floor(
      Date.UTC(
        date.getUTCFullYear(),
        date.getUTCMonth(),
        date.getUTCDate() + 1,
      ) / 1000,
    ) - now
  );
}

export async function readFairUseRestriction(
  database: D1Database | undefined,
  uid: string,
  now = Math.floor(Date.now() / 1000),
): Promise<FairUseRestriction | null> {
  if (!database || !uid) return null;
  let row: RestrictionRow | null;
  try {
    row = await database
      .prepare(
        "SELECT COALESCE(state.stage, 'none') AS stage, state.restrict_until, " +
          "COALESCE(usage.daily_ms, 0) AS daily_ms, COALESCE(usage.three_day_ms, 0) AS three_day_ms, " +
          "COALESCE(usage.weekly_ms, 0) AS weekly_ms FROM (SELECT ? AS uid) AS subject " +
          "LEFT JOIN cf_fair_use_states AS state ON state.uid = subject.uid " +
          "LEFT JOIN (SELECT uid, " +
          "SUM(CASE WHEN occurred_at >= ? THEN speech_ms ELSE 0 END) AS daily_ms, " +
          "SUM(CASE WHEN occurred_at >= ? THEN speech_ms ELSE 0 END) AS three_day_ms, " +
          "SUM(speech_ms) AS weekly_ms FROM cf_fair_use_usage_sources " +
          "WHERE uid = ? AND source_kind IN ('realtime', 'sync_fresh') AND occurred_at >= ? GROUP BY uid" +
          ") AS usage ON usage.uid = subject.uid",
      )
      .bind(
        uid,
        now - DAY_SECONDS,
        now - 3 * DAY_SECONDS,
        uid,
        now - 7 * DAY_SECONDS,
      )
      .first<RestrictionRow>();
  } catch {
    recordFallback({
      component: "other",
      from: "d1",
      to: "none",
      reason: "dependency_unavailable",
      outcome: "degraded",
    });
    return null;
  }

  const stageValue = String(row?.stage || "none");
  let stage = new Set(["none", "warning", "throttle", "restrict"]).has(
    stageValue,
  )
    ? (stageValue as FairUseRestriction["stage"])
    : "none";
  const restrictUntil =
    typeof row?.restrict_until === "number" &&
    Number.isInteger(row.restrict_until)
      ? row.restrict_until
      : null;
  if (stage === "restrict" && (restrictUntil === null || restrictUntil < now)) {
    try {
      await database
        .prepare(
          "UPDATE cf_fair_use_states SET stage = 'throttle', restrict_until = NULL, updated_at = ? " +
            "WHERE uid = ? AND stage = 'restrict'",
        )
        .bind(now, uid)
        .run();
    } catch {
      recordFallback({
        component: "other",
        from: "d1",
        to: "none",
        reason: "dependency_unavailable",
        outcome: "degraded",
      });
    }
    if (restrictUntil === null) {
      recordFallback({
        component: "other",
        from: "restrict",
        to: "throttle",
        reason: "malformed_doc",
        outcome: "recovered",
      });
    }
    stage = "throttle";
  }

  const daily = nonnegativeInteger(row?.daily_ms);
  const threeDay = nonnegativeInteger(row?.three_day_ms);
  const weekly = nonnegativeInteger(row?.weekly_ms);
  if (daily >= MAX_DAILY_AUDIO_MS) {
    return {
      blocked: true,
      reason: "daily_audio_ceiling",
      retryAfter: boundedRetryAfter(nextUtcDay(now)),
      stage,
    };
  }
  if (
    stage === "restrict" &&
    (daily > DEFAULT_FAIR_USE_CAPS.daily ||
      threeDay > DEFAULT_FAIR_USE_CAPS.threeDay ||
      weekly > DEFAULT_FAIR_USE_CAPS.weekly)
  ) {
    return {
      blocked: true,
      reason: "fair_use_restricted",
      retryAfter: boundedRetryAfter(
        restrictUntil === null ? null : restrictUntil - now,
      ),
      stage,
    };
  }
  return null;
}

export function fairUseRestrictionResponse(
  restriction: FairUseRestriction,
): Response {
  return Response.json(
    {
      code: "fair_use_restricted",
      detail: "Account temporarily restricted due to fair-use policy",
    },
    {
      status: 429,
      headers: {
        "Retry-After": String(restriction.retryAfter),
        "X-Omi-Rate-Limit-Reason": "fair_use",
      },
    },
  );
}
