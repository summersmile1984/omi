import type { JobsEnv } from "./env";

type IdentityColumn =
  | "uid"
  | "owner_uid"
  | "recipient_uid"
  | "reviewer_uid"
  | "sender_uid"
  | "uid_hint";

type D1IdentitySurface = Readonly<{
  table: string;
  column: IdentityColumn;
}>;

/**
 * Every App-D1 surface that can retain a Better Auth/Firebase uid.
 *
 * Keep this explicit: account deletion is an authority boundary, so a table
 * must never become deletable merely because its name happens to match a
 * heuristic. The schema-coverage test fails whenever a migration introduces a
 * new identity-bearing table/column without adding it here.
 */
export const ACCOUNT_DELETION_D1_SURFACES = Object.freeze([
  { table: "cf_account_cutover", column: "uid" },
  { table: "cf_action_items", column: "uid" },
  { table: "cf_announcement_dismissals", column: "uid" },
  { table: "cf_app_catalog", column: "owner_uid" },
  { table: "cf_app_reviews", column: "reviewer_uid" },
  { table: "cf_asset_cleanup_tasks", column: "uid" },
  { table: "cf_asset_objects", column: "uid" },
  { table: "cf_calendar_meetings", column: "uid" },
  { table: "cf_chat_messages", column: "uid" },
  { table: "cf_chat_quota_events", column: "uid" },
  { table: "cf_chat_sessions", column: "uid" },
  { table: "cf_chat_shares", column: "sender_uid" },
  { table: "cf_conversations", column: "uid" },
  { table: "cf_conversations_fts", column: "uid" },
  { table: "cf_daily_summaries", column: "uid" },
  { table: "cf_fair_use_events", column: "uid" },
  { table: "cf_fair_use_notification_outbox", column: "uid" },
  { table: "cf_fair_use_states", column: "uid" },
  { table: "cf_fair_use_usage_sources", column: "uid" },
  { table: "cf_focus_sessions", column: "uid" },
  { table: "cf_folders", column: "uid" },
  { table: "cf_goal_mutations", column: "uid" },
  { table: "cf_goal_progress_events", column: "uid" },
  { table: "cf_goal_progress_history", column: "uid" },
  { table: "cf_goals", column: "uid" },
  { table: "cf_jobs", column: "uid" },
  { table: "cf_llm_usage_daily", column: "uid" },
  { table: "cf_memories", column: "uid" },
  { table: "cf_people", column: "uid" },
  { table: "cf_realtime_sessions", column: "uid" },
  { table: "cf_realtime_usage", column: "uid" },
  { table: "cf_screen_activity", column: "uid" },
  { table: "cf_stripe_customers", column: "uid" },
  { table: "cf_stripe_webhook_events", column: "uid_hint" },
  { table: "cf_sync_capture_claims", column: "uid" },
  { table: "cf_sync_content_ledger", column: "uid" },
  { table: "cf_sync_job_files", column: "uid" },
  { table: "cf_sync_jobs", column: "uid" },
  { table: "cf_sync_playback_objects", column: "uid" },
  { table: "cf_task_share_acceptances", column: "recipient_uid" },
  { table: "cf_task_shares", column: "sender_uid" },
  { table: "cf_usage_sources", column: "uid" },
  { table: "cf_user_ai_profiles", column: "uid" },
  { table: "cf_user_assistant_settings", column: "uid" },
  { table: "cf_user_calendar_onboarding", column: "uid" },
  { table: "cf_user_developer_webhooks", column: "uid" },
  { table: "cf_user_enabled_apps", column: "uid" },
  { table: "cf_user_fcm_tokens", column: "uid" },
  { table: "cf_user_feedback", column: "uid" },
  { table: "cf_user_geolocation", column: "uid" },
  { table: "cf_user_location_context_consent", column: "uid" },
  { table: "cf_user_notification_preferences", column: "uid" },
  { table: "cf_user_notification_settings", column: "uid" },
  { table: "cf_user_onboarding", column: "uid" },
  { table: "cf_user_privacy_settings", column: "uid" },
  { table: "cf_user_subscriptions", column: "uid" },
  { table: "cf_user_training_data_opt_in", column: "uid" },
  { table: "cf_user_transcription_preferences", column: "uid" },
  { table: "cf_worker_probe", column: "uid" },
  { table: "cf_workstream_artifacts", column: "uid" },
  { table: "cf_workstream_checkpoints", column: "uid" },
  { table: "cf_workstream_events", column: "uid" },
  { table: "cf_workstream_mutations", column: "uid" },
  { table: "cf_workstreams", column: "uid" },
] satisfies readonly D1IdentitySurface[]);

/**
 * Minimal control-plane rows intentionally retained while product residuals
 * are being driven to zero. They are transferred from the live intent to the
 * short-lived JWT tombstone only after Auth deletion succeeds.
 */
export const ACCOUNT_DELETION_CONTROL_D1_SURFACES = Object.freeze([
  { table: "cf_account_deletion_intents", column: "uid" },
  { table: "cf_account_deletion_tombstones", column: "uid" },
] satisfies readonly D1IdentitySurface[]);

const PURGE_PRIORITY = Object.freeze([
  "cf_task_share_acceptances.recipient_uid",
  "cf_task_shares.sender_uid",
  "cf_chat_shares.sender_uid",
  "cf_app_reviews.reviewer_uid",
  "cf_app_catalog.owner_uid",
  "cf_fair_use_notification_outbox.uid",
  "cf_fair_use_events.uid",
  "cf_sync_job_files.uid",
  "cf_sync_jobs.uid",
] as const);

const PURGE_PRIORITY_SET = new Set<string>(PURGE_PRIORITY);

const PURGE_ORDER = Object.freeze([
  ...PURGE_PRIORITY,
  ...ACCOUNT_DELETION_D1_SURFACES.map(
    ({ table, column }) => `${table}.${column}`,
  ).filter((key) => !PURGE_PRIORITY_SET.has(key)),
]);

export const ACCOUNT_DELETION_D1_PURGE_SURFACES = Object.freeze(
  PURGE_ORDER.map((key) => {
    const surface = ACCOUNT_DELETION_D1_SURFACES.find(
      ({ table, column }) => `${table}.${column}` === key,
    );
    if (!surface) throw new Error(`unknown account deletion surface ${key}`);
    return surface;
  }),
);

/** User-scoped object families currently stored in the shared ASSETS bucket. */
export const ACCOUNT_DELETION_R2_PREFIX_PATTERNS = Object.freeze([
  "cf-assets/{uid}/",
  "cf-transcriptions/{uid}/",
  "cf-sync/{uid}/",
  "sync-playback/{uid}/",
  "playback/{uid}/",
  "merged/{uid}/",
  "chunks/{uid}/",
] as const);

export type AccountProductResidual = Readonly<{
  uid: string;
  empty: boolean;
  d1: Readonly<Record<string, number>>;
  r2: Readonly<Record<string, number>>;
}>;

export function validAccountDeletionUid(value: string): boolean {
  return value.length > 0 && value.length <= 256 && !value.includes("/");
}

function databaseCount(value: unknown): number {
  const count = typeof value === "number" ? value : Number(value);
  if (!Number.isSafeInteger(count) || count < 0) {
    throw new Error("invalid product residual count");
  }
  return count;
}

function residualKey(surface: D1IdentitySurface): string {
  return `${surface.table}.${surface.column}`;
}

function r2Prefix(pattern: string, uid: string): string {
  return pattern.replace("{uid}", uid);
}

export async function readAccountProductResidual(
  env: Pick<JobsEnv, "APP_DB" | "ASSETS">,
  uid: string,
): Promise<AccountProductResidual> {
  if (!validAccountDeletionUid(uid)) {
    throw new Error("invalid account deletion uid");
  }

  const statements = ACCOUNT_DELETION_D1_SURFACES.map((surface) =>
    env.APP_DB.prepare(
      `SELECT COUNT(*) AS count FROM ${surface.table} WHERE ${surface.column} = ?`,
    ).bind(uid),
  );
  const [d1Results, r2Results] = await Promise.all([
    env.APP_DB.batch<{ count?: unknown }>(statements),
    Promise.all(
      ACCOUNT_DELETION_R2_PREFIX_PATTERNS.map(async (pattern) => {
        const prefix = r2Prefix(pattern, uid);
        const listed = await env.ASSETS.list({ prefix, limit: 1 });
        return [prefix, listed.objects.length > 0 ? 1 : 0] as const;
      }),
    ),
  ]);

  if (d1Results.length !== ACCOUNT_DELETION_D1_SURFACES.length) {
    throw new Error("product residual batch is incomplete");
  }
  const d1: Record<string, number> = {};
  for (const [index, result] of d1Results.entries()) {
    if (!result.success || !Array.isArray(result.results)) {
      throw new Error("product residual query failed");
    }
    if (result.results.length !== 1) {
      throw new Error("product residual query returned invalid rows");
    }
    d1[residualKey(ACCOUNT_DELETION_D1_SURFACES[index])] = databaseCount(
      result.results[0]?.count,
    );
  }
  const r2 = Object.fromEntries(r2Results);
  const empty =
    Object.values(d1).every((count) => count === 0) &&
    Object.values(r2).every((count) => count === 0);
  return Object.freeze({
    uid,
    empty,
    d1: Object.freeze(d1),
    r2: Object.freeze(r2),
  });
}
