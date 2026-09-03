import { recordFallback } from "../shared/fallback";
import {
  BASIC_MONTHLY_TRANSCRIPTION_SECONDS,
  FAIR_USE_CLASSIFIER_MODEL,
  capsForPlan,
  chooseFairUseTransition,
  defaultClassifierResult,
  fairUseClassifierInput,
  fairUseNotification,
  parseFairUseClassifierResponse,
  triggeredFairUseCaps,
  type FairUseClassifierResult,
  type FairUseConversationSummary,
  type FairUseStage,
  type FairUseUsageWindow,
} from "../shared/fair-use-policy";
import type { JobsEnv } from "./env";

const DAY_SECONDS = 86_400;
const EVALUATION_COOLDOWN_SECONDS = 12 * 60 * 60;
const EVALUATION_LEASE_SECONDS = 15 * 60;
const EVALUATION_BATCH_SIZE = 25;
const MAX_CLASSIFIER_JSON_BYTES = 32_000;
const PAID_PLANS = [
  "unlimited",
  "plus",
  "unlimited_v2",
  "operator",
  "architect",
];

export type FairUseCandidate = {
  uid: string;
  plan: string;
  daily_ms: number;
  three_day_ms: number;
  weekly_ms: number;
};

type FairUseStateRow = {
  stage: FairUseStage;
  evaluation_lease_token: string | null;
};

type ConversationRow = {
  id: string;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  source: string;
  structured_json: string;
};

function finiteNonnegative(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : 0;
}

function monthStartUtc(now: number): number {
  const date = new Date(now * 1000);
  return Math.floor(
    Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1) / 1000,
  );
}

function randomCaseReference(): string {
  return `FU-${crypto.randomUUID().replaceAll("-", "").slice(0, 12).toUpperCase()}`;
}

function structuredFields(value: string): Record<string, unknown> {
  if (new TextEncoder().encode(value).byteLength > 64_000) return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function shortString(value: unknown, maximum: number): string {
  return typeof value === "string" ? value.slice(0, maximum) : "";
}

export function conversationSummary(
  row: ConversationRow,
): FairUseConversationSummary {
  const structured = structuredFields(row.structured_json || "{}");
  const startedAt = finiteNonnegative(row.started_at);
  const finishedAt = finiteNonnegative(row.finished_at);
  const durationMinutes =
    startedAt > 0 && finishedAt >= startedAt
      ? Math.round(((finishedAt - startedAt) / 60) * 10) / 10
      : 0;
  return {
    conversation_id: shortString(row.id, 256),
    title: shortString(structured.title, 300),
    overview: shortString(structured.overview, 200),
    category: shortString(structured.category, 100),
    duration_minutes: durationMinutes,
    source: shortString(row.source, 100),
    created_at: new Date(
      finiteNonnegative(row.created_at) * 1000,
    ).toISOString(),
  };
}

export async function normalizeFairUseStates(
  database: D1Database,
  now: number,
): Promise<void> {
  await database
    .prepare(
      "UPDATE cf_fair_use_states SET stage = 'none', violation_count_7d = 0, violation_count_30d = 0, " +
        "throttle_until = NULL, restrict_until = NULL, cleared_by = 'subscription_upgrade', cleared_at = ?, " +
        "evaluation_lease_token = NULL, evaluation_lease_until = NULL, next_evaluation_at = NULL, updated_at = ? " +
        "WHERE stage != 'none' AND last_classifier_type = 'free_exhausted' AND EXISTS (" +
        "SELECT 1 FROM cf_user_subscriptions AS subscription WHERE subscription.uid = cf_fair_use_states.uid " +
        `AND subscription.status = 'active' AND subscription.plan IN (${PAID_PLANS.map(() => "?").join(", ")})` +
        ")",
    )
    .bind(now, now, ...PAID_PLANS)
    .run();

  const malformed = await database
    .prepare(
      "UPDATE cf_fair_use_states SET stage = 'throttle', restrict_until = NULL, updated_at = ? " +
        "WHERE stage = 'restrict' AND (restrict_until IS NULL OR typeof(restrict_until) != 'integer')",
    )
    .bind(now)
    .run();
  if ((malformed.meta?.changes || 0) > 0) {
    recordFallback({
      component: "other",
      from: "restrict",
      to: "throttle",
      reason: "malformed_doc",
      outcome: "recovered",
    });
  }

  await database
    .prepare(
      "UPDATE cf_fair_use_states SET stage = 'throttle', restrict_until = NULL, updated_at = ? " +
        "WHERE stage = 'restrict' AND typeof(restrict_until) = 'integer' AND restrict_until < ?",
    )
    .bind(now, now)
    .run();
}

export async function scanFairUseCandidates(
  database: D1Database,
  now: number,
  limit = EVALUATION_BATCH_SIZE,
): Promise<FairUseCandidate[]> {
  const result = await database
    .prepare(
      "SELECT usage.uid AS uid, " +
        "CASE WHEN subscription.status = 'active' THEN COALESCE(subscription.plan, 'basic') ELSE 'basic' END AS plan, " +
        "COALESCE(SUM(CASE WHEN usage.occurred_at >= ? THEN usage.speech_ms ELSE 0 END), 0) AS daily_ms, " +
        "COALESCE(SUM(CASE WHEN usage.occurred_at >= ? THEN usage.speech_ms ELSE 0 END), 0) AS three_day_ms, " +
        "COALESCE(SUM(usage.speech_ms), 0) AS weekly_ms " +
        "FROM cf_fair_use_usage_sources AS usage " +
        "LEFT JOIN cf_user_subscriptions AS subscription ON subscription.uid = usage.uid " +
        "LEFT JOIN cf_fair_use_states AS state ON state.uid = usage.uid " +
        "WHERE usage.source_kind IN ('realtime', 'sync_fresh') AND usage.occurred_at >= ? " +
        "AND COALESCE(state.stage, 'none') != 'restrict' " +
        "AND (state.next_evaluation_at IS NULL OR state.next_evaluation_at <= ?) " +
        "AND (state.evaluation_lease_until IS NULL OR state.evaluation_lease_until <= ?) " +
        "GROUP BY usage.uid " +
        "HAVING daily_ms > CASE WHEN plan IN ('unlimited', 'unlimited_v2', 'operator', 'architect') THEN 14400000 ELSE 7200000 END " +
        "OR three_day_ms > CASE WHEN plan IN ('unlimited', 'unlimited_v2', 'operator', 'architect') THEN 57600000 ELSE 28800000 END " +
        "OR weekly_ms > CASE WHEN plan IN ('unlimited', 'unlimited_v2', 'operator', 'architect') THEN 72000000 ELSE 36000000 END " +
        "ORDER BY MAX(usage.occurred_at) DESC, usage.uid ASC LIMIT ?",
    )
    .bind(
      now - DAY_SECONDS,
      now - 3 * DAY_SECONDS,
      now - 7 * DAY_SECONDS,
      now,
      now,
      limit,
    )
    .all<FairUseCandidate>();
  return (result.results || []).map((row) => ({
    uid: String(row.uid),
    plan: String(row.plan || "basic"),
    daily_ms: finiteNonnegative(row.daily_ms),
    three_day_ms: finiteNonnegative(row.three_day_ms),
    weekly_ms: finiteNonnegative(row.weekly_ms),
  }));
}

async function claimEvaluation(
  database: D1Database,
  uid: string,
  token: string,
  now: number,
): Promise<FairUseStateRow | null> {
  const claimed = await database
    .prepare(
      "INSERT INTO cf_fair_use_states " +
        "(uid, stage, updated_at, evaluation_lease_token, evaluation_lease_until) VALUES (?, 'none', ?, ?, ?) " +
        "ON CONFLICT(uid) DO UPDATE SET evaluation_lease_token = excluded.evaluation_lease_token, " +
        "evaluation_lease_until = excluded.evaluation_lease_until, updated_at = excluded.updated_at " +
        "WHERE cf_fair_use_states.stage != 'restrict' " +
        "AND (cf_fair_use_states.next_evaluation_at IS NULL OR cf_fair_use_states.next_evaluation_at <= ?) " +
        "AND (cf_fair_use_states.evaluation_lease_until IS NULL OR cf_fair_use_states.evaluation_lease_until <= ?)",
    )
    .bind(uid, now, token, now + EVALUATION_LEASE_SECONDS, now, now)
    .run();
  if (claimed.meta?.changes !== 1) return null;
  return database
    .prepare(
      "SELECT stage, evaluation_lease_token FROM cf_fair_use_states WHERE uid = ?",
    )
    .bind(uid)
    .first<FairUseStateRow>();
}

async function priorViolationCounts(
  database: D1Database,
  uid: string,
  now: number,
): Promise<{ sevenDays: number; thirtyDays: number }> {
  const row = await database
    .prepare(
      "SELECT COUNT(*) AS thirty_days, " +
        "COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS seven_days " +
        "FROM cf_fair_use_events WHERE uid = ? AND created_at >= ?",
    )
    .bind(now - 7 * DAY_SECONDS, uid, now - 30 * DAY_SECONDS)
    .first<{ seven_days: number; thirty_days: number }>();
  return {
    sevenDays: finiteNonnegative(row?.seven_days),
    thirtyDays: finiteNonnegative(row?.thirty_days),
  };
}

async function isBasicCreditsExhausted(
  database: D1Database,
  candidate: FairUseCandidate,
  now: number,
): Promise<boolean> {
  if (candidate.plan !== "basic") return false;
  const row = await database
    .prepare(
      "SELECT COALESCE(SUM(transcription_seconds), 0) AS used_seconds " +
        "FROM cf_usage_sources WHERE uid = ? AND occurred_at >= ? AND occurred_at < ?",
    )
    .bind(candidate.uid, monthStartUtc(now), now + 1)
    .first<{ used_seconds: number }>();
  return (
    finiteNonnegative(row?.used_seconds) >= BASIC_MONTHLY_TRANSCRIPTION_SECONDS
  );
}

async function recentConversations(
  database: D1Database,
  uid: string,
  now: number,
): Promise<FairUseConversationSummary[]> {
  const result = await database
    .prepare(
      "SELECT id, created_at, started_at, finished_at, source, structured_json " +
        "FROM cf_conversations WHERE uid = ? AND created_at >= ? AND discarded = 0 " +
        "ORDER BY created_at DESC, id DESC LIMIT 30",
    )
    .bind(uid, now - 7 * DAY_SECONDS)
    .all<ConversationRow>();
  return (result.results || []).map(conversationSummary);
}

async function classifyCandidate(
  env: JobsEnv,
  candidate: FairUseCandidate,
  now: number,
): Promise<FairUseClassifierResult> {
  const model = env.WORKERS_AI_FAIR_USE_MODEL || FAIR_USE_CLASSIFIER_MODEL;
  if (await isBasicCreditsExhausted(env.APP_DB, candidate, now)) {
    return {
      ...defaultClassifierResult(model),
      misuse_score: 1,
      usage_type: "free_exhausted",
      confidence: 1,
    };
  }
  const summaries = await recentConversations(env.APP_DB, candidate.uid, now);
  if (!summaries.length) return defaultClassifierResult(model);
  let response: unknown;
  try {
    response = await env.AI.run(model, {
      ...fairUseClassifierInput(summaries),
      max_tokens: 1_024,
      temperature: 0,
    });
  } catch {
    recordFallback({
      component: "other",
      from: "workers_ai_classifier",
      to: "conservative_no_action",
      reason: "dependency_unavailable",
      outcome: "degraded",
    });
    return defaultClassifierResult(model);
  }
  const result = parseFairUseClassifierResponse(response, model);
  const responseText =
    response && typeof response === "object"
      ? (response as Record<string, unknown>).response
      : undefined;
  let validJsonObject = false;
  if (typeof responseText === "string") {
    const fenced = responseText.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1];
    try {
      const parsed = JSON.parse((fenced || responseText).trim());
      validJsonObject =
        !!parsed && typeof parsed === "object" && !Array.isArray(parsed);
    } catch {
      validJsonObject = false;
    }
  }
  if (!validJsonObject) {
    recordFallback({
      component: "other",
      from: "workers_ai_classifier",
      to: "conservative_no_action",
      reason: "malformed_response",
      outcome: "degraded",
    });
  }
  return result;
}

async function releaseEvaluationLease(
  database: D1Database,
  uid: string,
  token: string,
): Promise<void> {
  await database
    .prepare(
      "UPDATE cf_fair_use_states SET evaluation_lease_token = NULL, evaluation_lease_until = NULL " +
        "WHERE uid = ? AND evaluation_lease_token = ?",
    )
    .bind(uid, token)
    .run();
}

async function persistEvaluation(
  env: JobsEnv,
  candidate: FairUseCandidate,
  state: FairUseStateRow,
  counts: { sevenDays: number; thirtyDays: number },
  classifier: FairUseClassifierResult,
  leaseToken: string,
  now: number,
): Promise<boolean> {
  const usage: FairUseUsageWindow = {
    daily: candidate.daily_ms,
    threeDay: candidate.three_day_ms,
    weekly: candidate.weekly_ms,
  };
  const caps = capsForPlan(candidate.plan);
  const trigger = triggeredFairUseCaps(usage, caps)[0];
  if (!trigger) return false;
  const transition = chooseFairUseTransition(
    state.stage,
    counts.sevenDays,
    classifier.misuse_score,
  );
  const eventId = crypto.randomUUID();
  const caseRef = randomCaseReference();
  const classifierJson = JSON.stringify(classifier);
  const safeClassifierJson =
    new TextEncoder().encode(classifierJson).byteLength <=
    MAX_CLASSIFIER_JSON_BYTES
      ? classifierJson
      : JSON.stringify(defaultClassifierResult(classifier.model));
  const eventStatement = env.APP_DB.prepare(
    "INSERT INTO cf_fair_use_events " +
      "(event_id, uid, case_ref, created_at, session_id, trigger, daily_speech_ms, three_day_speech_ms, " +
      "weekly_speech_ms, daily_threshold_ms, three_day_threshold_ms, weekly_threshold_ms, classifier_json, " +
      "enforcement_action, previous_stage, new_stage) " +
      "SELECT ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? " +
      "FROM cf_fair_use_states WHERE uid = ? AND evaluation_lease_token = ?",
  ).bind(
    eventId,
    candidate.uid,
    caseRef,
    now,
    trigger,
    usage.daily,
    usage.threeDay,
    usage.weekly,
    caps.daily,
    caps.threeDay,
    caps.weekly,
    safeClassifierJson,
    transition.action,
    state.stage,
    transition.nextStage,
    candidate.uid,
    leaseToken,
  );
  const stateStatement = env.APP_DB.prepare(
    "UPDATE cf_fair_use_states SET stage = ?, " +
      "last_case_ref = CASE WHEN ? != 'none' THEN ? ELSE last_case_ref END, " +
      "throttle_until = CASE WHEN ? = 'throttle' THEN ? ELSE throttle_until END, " +
      "restrict_until = CASE WHEN ? = 'restrict' THEN ? ELSE restrict_until END, " +
      "last_violation_at = CASE WHEN ? != 'none' THEN ? ELSE last_violation_at END, " +
      "last_classifier_score = CASE WHEN ? != 'none' THEN ? ELSE last_classifier_score END, " +
      "last_classifier_type = CASE WHEN ? != 'none' THEN ? ELSE last_classifier_type END, " +
      "violation_count_7d = CASE WHEN ? != 'none' THEN ? ELSE violation_count_7d END, " +
      "violation_count_30d = CASE WHEN ? != 'none' THEN ? ELSE violation_count_30d END, " +
      "next_evaluation_at = ?, evaluation_lease_token = NULL, evaluation_lease_until = NULL, updated_at = ? " +
      "WHERE uid = ? AND evaluation_lease_token = ?",
  ).bind(
    transition.nextStage,
    transition.action,
    caseRef,
    transition.action,
    now + 7 * DAY_SECONDS,
    transition.action,
    now + 30 * DAY_SECONDS,
    transition.action,
    now,
    transition.action,
    classifier.misuse_score,
    transition.action,
    classifier.usage_type,
    transition.action,
    counts.sevenDays,
    transition.action,
    counts.thirtyDays,
    now + EVALUATION_COOLDOWN_SECONDS,
    now,
    candidate.uid,
    leaseToken,
  );
  const statements = [eventStatement, stateStatement];
  if (transition.action !== "none") {
    const notification = fairUseNotification(transition.action, caseRef);
    statements.push(
      env.APP_DB.prepare(
        "INSERT INTO cf_notification_outbox " +
          "(notification_id, source_kind, source_id, uid, title, body, data_json, status, attempts, not_before, created_at, updated_at) " +
          "SELECT ?, 'fair_use', ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ? FROM cf_fair_use_events WHERE event_id = ?",
      ).bind(
        crypto.randomUUID(),
        eventId,
        candidate.uid,
        notification.title,
        notification.body,
        JSON.stringify(notification.data),
        now,
        now,
        now,
        eventId,
      ),
    );
  }
  const results = await env.APP_DB.batch(statements);
  return results[1]?.meta?.changes === 1;
}

export async function evaluateFairUseCandidate(
  env: JobsEnv,
  candidate: FairUseCandidate,
  now: number,
): Promise<boolean> {
  const leaseToken = crypto.randomUUID();
  const state = await claimEvaluation(
    env.APP_DB,
    candidate.uid,
    leaseToken,
    now,
  );
  if (!state || state.evaluation_lease_token !== leaseToken) return false;
  try {
    const counts = await priorViolationCounts(env.APP_DB, candidate.uid, now);
    const classifier = await classifyCandidate(env, candidate, now);
    return await persistEvaluation(
      env,
      candidate,
      state,
      counts,
      classifier,
      leaseToken,
      now,
    );
  } catch (error) {
    await releaseEvaluationLease(env.APP_DB, candidate.uid, leaseToken).catch(
      () => undefined,
    );
    throw error;
  }
}

export async function evaluateFairUseBatch(
  env: JobsEnv,
  now = Math.floor(Date.now() / 1000),
): Promise<number> {
  await normalizeFairUseStates(env.APP_DB, now);
  const candidates = await scanFairUseCandidates(env.APP_DB, now);
  let evaluated = 0;
  for (const candidate of candidates) {
    try {
      if (await evaluateFairUseCandidate(env, candidate, now)) evaluated += 1;
    } catch (error) {
      console.error(
        JSON.stringify({
          event: "fair_use_evaluation_failed",
          error_type:
            error instanceof Error ? error.constructor.name : "UnknownError",
        }),
      );
    }
  }
  return evaluated;
}
