import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import { recordFallback } from "../shared/fallback";
import { validAccountDeletionUid } from "./account-deletion-residual";
import type { JobMessage, JobsEnv } from "./env";

const LEASE_SECONDS = 5 * 60;
const RETRY_BASE_SECONDS = 60;
const RETRY_MAX_SECONDS = 60 * 60;
const ZERO_SCAN_SETTLE_SECONDS = 30;
const R2_DELETE_BATCH_SIZE = 1_000;
const D1_DELETE_BATCH_SIZE = 250;
const RECONCILE_BATCH_SIZE = 50;

export const RECORDING_ASSET_PREFIX_PATTERNS = Object.freeze([
  "cf-sync/{uid}/",
  "sync-playback/{uid}/",
  "playback/{uid}/",
  "merged/{uid}/",
  "chunks/{uid}/",
] as const);

export const CONVERSATION_RECORDING_PREFIX_PATTERN = "{uid}/" as const;

type RecordingDeletionIntentRow = {
  uid: unknown;
  job_id: unknown;
  status: unknown;
  attempts: unknown;
  lease_token: unknown;
  lease_until: unknown;
  next_attempt_at: unknown;
  settled_at: unknown;
};

type RecordingDeletionIntent = {
  uid: string;
  jobId: string;
  status: "pending" | "running" | "failed";
  attempts: number;
  leaseToken: string | null;
  leaseUntil: number | null;
  nextAttemptAt: number;
  settledAt: number | null;
};

type RequestContext = Context<{ Bindings: JobsEnv }>;

function integer(value: unknown, label: string): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new Error(`invalid recording deletion ${label}`);
  }
  return parsed;
}

function optionalInteger(value: unknown, label: string): number | null {
  if (value === null || value === undefined) return null;
  return integer(value, label);
}

function optionalString(value: unknown, label: string): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || !value) {
    throw new Error(`invalid recording deletion ${label}`);
  }
  return value;
}

function parseIntent(row: RecordingDeletionIntentRow): RecordingDeletionIntent {
  const uid = String(row.uid || "");
  if (!validAccountDeletionUid(uid)) {
    throw new Error("invalid recording deletion uid");
  }
  if (typeof row.job_id !== "string" || !row.job_id) {
    throw new Error("invalid recording deletion job id");
  }
  if (
    row.status !== "pending" &&
    row.status !== "running" &&
    row.status !== "failed"
  ) {
    throw new Error("invalid recording deletion status");
  }
  return {
    uid,
    jobId: row.job_id,
    status: row.status,
    attempts: integer(row.attempts, "attempts"),
    leaseToken: optionalString(row.lease_token, "lease token"),
    leaseUntil: optionalInteger(row.lease_until, "lease until"),
    nextAttemptAt: integer(row.next_attempt_at, "next attempt"),
    settledAt: optionalInteger(row.settled_at, "settled at"),
  };
}

function prefixFor(pattern: string, uid: string): string {
  return pattern.replace("{uid}", uid);
}

function recordingDeletionMessage(jobId: string): JobMessage {
  return { jobId, uid: "", kind: "recording_delete", payload: {} };
}

async function queueRecordingDeletion(
  env: JobsEnv,
  jobId: string,
  delaySeconds = 0,
): Promise<boolean> {
  try {
    await env.JOBS.send(
      recordingDeletionMessage(jobId),
      delaySeconds > 0 ? { delaySeconds } : undefined,
    );
    return true;
  } catch {
    recordFallback({
      component: "other",
      from: "d1",
      to: "none",
      reason: "dependency_unavailable",
      outcome: "degraded",
    });
    return false;
  }
}

async function admitRecordingDeletion(
  c: RequestContext,
  context: SignedAuthContext,
): Promise<Response> {
  if (!validAccountDeletionUid(context.uid)) {
    return c.json({ error: "recording_deletion_unavailable" }, 503);
  }
  const now = Math.floor(Date.now() / 1_000);
  const jobId = crypto.randomUUID();
  try {
    const results = await c.env.APP_DB.batch([
      c.env.APP_DB.prepare(
        `INSERT INTO cf_user_privacy_settings
           (uid, store_recording_permission, private_cloud_sync_enabled,
            created_at, updated_at)
         VALUES (?, 0, 1, ?, ?)
         ON CONFLICT(uid) DO UPDATE SET
           store_recording_permission = 0,
           updated_at = excluded.updated_at`,
      ).bind(context.uid, now, now),
      c.env.APP_DB.prepare(
        `INSERT INTO cf_recording_deletion_intents
           (uid, job_id, status, attempts, next_attempt_at, created_at, updated_at)
         VALUES (?, ?, 'pending', 0, ?, ?, ?)
         ON CONFLICT(uid) DO NOTHING`,
      ).bind(context.uid, jobId, now, now, now),
    ]);
    if (results.length !== 2 || results.some((result) => !result.success)) {
      throw new Error("recording deletion admission batch failed");
    }
    if (Number(results[1].meta?.changes ?? 0) === 1) {
      await queueRecordingDeletion(c.env, jobId);
    }
    return c.json({ status: "ok" });
  } catch {
    return c.json({ error: "recording_deletion_unavailable" }, 503);
  }
}

export function registerRecordingDeletionRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: (
    c: Context<{ Bindings: JobsEnv }>,
  ) => Promise<SignedAuthContext | null>,
) {
  app.delete("/v1/users/store-recording-permission", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    return admitRecordingDeletion(c, context);
  });
}

async function readIntent(
  env: JobsEnv,
  jobId: string,
): Promise<RecordingDeletionIntent | null> {
  const row = await env.APP_DB.prepare(
    `SELECT uid, job_id, status, attempts, lease_token, lease_until,
            next_attempt_at, settled_at
     FROM cf_recording_deletion_intents WHERE job_id = ?`,
  )
    .bind(jobId)
    .first<RecordingDeletionIntentRow>();
  return row ? parseIntent(row) : null;
}

async function claimIntent(
  env: JobsEnv,
  jobId: string,
  now: number,
): Promise<RecordingDeletionIntent | null> {
  const leaseToken = crypto.randomUUID();
  const claimed = await env.APP_DB.prepare(
    `UPDATE cf_recording_deletion_intents
     SET status = 'running', attempts = attempts + 1, lease_token = ?,
         lease_until = ?, last_error = NULL, updated_at = ?
     WHERE job_id = ?
       AND ((status IN ('pending', 'failed') AND next_attempt_at <= ?)
         OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?))`,
  )
    .bind(leaseToken, now + LEASE_SECONDS, now, jobId, now, now)
    .run();
  if (claimed.meta?.changes !== 1) return null;
  const intent = await readIntent(env, jobId);
  if (!intent || intent.leaseToken !== leaseToken) {
    throw new Error("recording deletion lease was not persisted");
  }
  return intent;
}

function recordingR2Surfaces(env: JobsEnv, uid: string) {
  return [
    {
      bucket: env.CONVERSATION_RECORDINGS,
      prefix: prefixFor(CONVERSATION_RECORDING_PREFIX_PATTERN, uid),
    },
    ...RECORDING_ASSET_PREFIX_PATTERNS.map((pattern) => ({
      bucket: env.ASSETS,
      prefix: prefixFor(pattern, uid),
    })),
  ];
}

async function purgeOneR2Page(env: JobsEnv, uid: string): Promise<boolean> {
  for (const surface of recordingR2Surfaces(env, uid)) {
    const listed = await surface.bucket.list({
      prefix: surface.prefix,
      limit: R2_DELETE_BATCH_SIZE,
    });
    const keys = listed.objects.map(({ key }) => key);
    if (!keys.length) continue;
    await surface.bucket.delete(keys);
    return true;
  }
  return false;
}

async function purgeOneAudioAsset(env: JobsEnv, uid: string): Promise<boolean> {
  const cleanup = await env.APP_DB.prepare(
    `SELECT storage_key, content_type FROM cf_asset_cleanup_tasks
     WHERE uid = ? AND (content_type IS NULL OR content_type LIKE 'audio/%')
     ORDER BY content_type IS NULL, created_at LIMIT 1`,
  )
    .bind(uid)
    .first<{ storage_key?: unknown; content_type?: unknown }>();
  if (cleanup) {
    const storageKey = String(cleanup.storage_key || "");
    if (!storageKey) throw new Error("invalid asset cleanup storage key");
    const active = await env.APP_DB.prepare(
      `SELECT content_type FROM cf_asset_objects
       WHERE uid = ? AND storage_key = ? LIMIT 1`,
    )
      .bind(uid, storageKey)
      .first<{ content_type?: unknown }>();
    if (active) {
      await env.APP_DB.prepare(
        "DELETE FROM cf_asset_cleanup_tasks WHERE uid = ? AND storage_key = ?",
      )
        .bind(uid, storageKey)
        .run();
      return true;
    }

    let contentType =
      typeof cleanup.content_type === "string" ? cleanup.content_type : null;
    if (contentType === null) {
      const stored = await env.ASSETS.head(storageKey);
      if (!stored) {
        await env.APP_DB.prepare(
          "DELETE FROM cf_asset_cleanup_tasks WHERE uid = ? AND storage_key = ?",
        )
          .bind(uid, storageKey)
          .run();
        return true;
      }
      contentType = stored.httpMetadata?.contentType || null;
      if (contentType && !contentType.startsWith("audio/")) {
        await env.APP_DB.prepare(
          `UPDATE cf_asset_cleanup_tasks SET content_type = ?, updated_at = ?
           WHERE uid = ? AND storage_key = ?`,
        )
          .bind(
            contentType.slice(0, 200),
            Math.floor(Date.now() / 1_000),
            uid,
            storageKey,
          )
          .run();
        return true;
      }
    }

    // A cleanup task already declares this object disposable. Missing legacy
    // MIME metadata therefore fails toward deletion instead of retaining a
    // potentially raw recording after the privacy request completes.
    await env.ASSETS.delete(storageKey);
    await env.APP_DB.prepare(
      "DELETE FROM cf_asset_cleanup_tasks WHERE uid = ? AND storage_key = ?",
    )
      .bind(uid, storageKey)
      .run();
    return true;
  }

  const active = await env.APP_DB.prepare(
    `SELECT object_key, storage_key FROM cf_asset_objects
     WHERE uid = ? AND content_type LIKE 'audio/%'
     ORDER BY created_at LIMIT 1`,
  )
    .bind(uid)
    .first<{ object_key?: unknown; storage_key?: unknown }>();
  if (!active) return false;
  const objectKey = String(active.object_key || "");
  const storageKey = String(active.storage_key || "");
  if (!objectKey || !storageKey.startsWith(`cf-assets/${uid}/`)) {
    throw new Error("audio asset escaped uid prefix");
  }
  await env.ASSETS.delete(storageKey);
  const results = await env.APP_DB.batch([
    env.APP_DB.prepare(
      "DELETE FROM cf_asset_cleanup_tasks WHERE uid = ? AND logical_key = ?",
    ).bind(uid, objectKey),
    env.APP_DB.prepare(
      "DELETE FROM cf_asset_objects WHERE uid = ? AND object_key = ? AND storage_key = ?",
    ).bind(uid, objectKey, storageKey),
  ]);
  if (results.length !== 2 || results.some((result) => !result.success)) {
    throw new Error("audio asset metadata purge failed");
  }
  return true;
}

async function purgeOneD1Batch(env: JobsEnv, uid: string): Promise<void> {
  const results = await env.APP_DB.batch([
    env.APP_DB.prepare(
      `DELETE FROM cf_sync_playback_objects
       WHERE rowid IN (
         SELECT rowid FROM cf_sync_playback_objects WHERE uid = ? LIMIT ?
       )`,
    ).bind(uid, D1_DELETE_BATCH_SIZE),
    env.APP_DB.prepare(
      `DELETE FROM cf_sync_job_files
       WHERE rowid IN (
         SELECT rowid FROM cf_sync_job_files WHERE uid = ? LIMIT ?
       )`,
    ).bind(uid, D1_DELETE_BATCH_SIZE),
    env.APP_DB.prepare(
      `UPDATE cf_conversations
       SET audio_files_json = '[]', conversation_audio_json = NULL,
           private_cloud_sync_enabled = 0
       WHERE rowid IN (
         SELECT rowid FROM cf_conversations
         WHERE uid = ? AND (
           COALESCE(json_array_length(audio_files_json), 0) > 0
           OR conversation_audio_json IS NOT NULL
           OR private_cloud_sync_enabled <> 0
         ) LIMIT ?
       )`,
    ).bind(uid, D1_DELETE_BATCH_SIZE),
  ]);
  if (results.length !== 3 || results.some((result) => !result.success)) {
    throw new Error("recording metadata purge failed");
  }
}

async function recordingResidualIsEmpty(
  env: JobsEnv,
  uid: string,
): Promise<boolean> {
  const [database, r2] = await Promise.all([
    env.APP_DB.batch<{ count?: unknown }>([
      env.APP_DB.prepare(
        "SELECT COUNT(*) AS count FROM cf_sync_playback_objects WHERE uid = ?",
      ).bind(uid),
      env.APP_DB.prepare(
        "SELECT COUNT(*) AS count FROM cf_sync_job_files WHERE uid = ?",
      ).bind(uid),
      env.APP_DB.prepare(
        `SELECT COUNT(*) AS count FROM cf_conversations
         WHERE uid = ? AND (
           COALESCE(json_array_length(audio_files_json), 0) > 0
           OR conversation_audio_json IS NOT NULL
           OR private_cloud_sync_enabled <> 0
         )`,
      ).bind(uid),
      env.APP_DB.prepare(
        "SELECT COUNT(*) AS count FROM cf_asset_objects WHERE uid = ? AND content_type LIKE 'audio/%'",
      ).bind(uid),
      env.APP_DB.prepare(
        `SELECT COUNT(*) AS count
         FROM cf_asset_cleanup_tasks
         WHERE uid = ? AND (content_type IS NULL OR content_type LIKE 'audio/%')`,
      ).bind(uid),
    ]),
    Promise.all(
      recordingR2Surfaces(env, uid).map(async (surface) => {
        const listed = await surface.bucket.list({
          prefix: surface.prefix,
          limit: 1,
        });
        return listed.objects.length;
      }),
    ),
  ]);
  if (database.length !== 5) {
    throw new Error("recording residual batch is incomplete");
  }
  for (const result of database) {
    if (!result.success || result.results?.length !== 1) {
      throw new Error("recording residual query failed");
    }
    if (integer(result.results[0]?.count ?? -1, "residual count") !== 0) {
      return false;
    }
  }
  return r2.every((count) => count === 0);
}

async function releaseIntent(
  env: JobsEnv,
  intent: RecordingDeletionIntent,
  options: { settledAt?: number | null; delaySeconds: number },
) {
  if (!intent.leaseToken)
    throw new Error("recording deletion lease is missing");
  const now = Math.floor(Date.now() / 1_000);
  const updated = await env.APP_DB.prepare(
    `UPDATE cf_recording_deletion_intents
     SET status = 'pending', lease_token = NULL, lease_until = NULL,
         next_attempt_at = ?, settled_at = ?, updated_at = ?
     WHERE job_id = ? AND lease_token = ?`,
  )
    .bind(
      now + options.delaySeconds,
      options.settledAt === undefined ? intent.settledAt : options.settledAt,
      now,
      intent.jobId,
      intent.leaseToken,
    )
    .run();
  if (updated.meta?.changes !== 1) {
    throw new Error("recording deletion lease was lost");
  }
  await queueRecordingDeletion(env, intent.jobId, options.delaySeconds);
}

async function markIntentFailed(env: JobsEnv, intent: RecordingDeletionIntent) {
  if (!intent.leaseToken) return;
  const now = Math.floor(Date.now() / 1_000);
  const delay = Math.min(
    RETRY_MAX_SECONDS,
    RETRY_BASE_SECONDS * 2 ** Math.min(intent.attempts, 6),
  );
  await env.APP_DB.prepare(
    `UPDATE cf_recording_deletion_intents
     SET status = 'failed', lease_token = NULL, lease_until = NULL,
         next_attempt_at = ?, last_error = ?, updated_at = ?
     WHERE job_id = ? AND lease_token = ?`,
  )
    .bind(
      now + delay,
      "recording cleanup unavailable",
      now,
      intent.jobId,
      intent.leaseToken,
    )
    .run();
}

async function completeIntent(
  env: JobsEnv,
  intent: RecordingDeletionIntent,
): Promise<void> {
  if (!intent.leaseToken)
    throw new Error("recording deletion lease is missing");
  const deleted = await env.APP_DB.prepare(
    "DELETE FROM cf_recording_deletion_intents WHERE job_id = ? AND lease_token = ?",
  )
    .bind(intent.jobId, intent.leaseToken)
    .run();
  if (deleted.meta?.changes !== 1) {
    throw new Error("recording deletion lease was lost");
  }
}

export async function processRecordingDeletionMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  if (message.body.kind !== "recording_delete" || message.body.uid !== "") {
    throw new Error("invalid recording deletion queue message");
  }
  const now = Math.floor(Date.now() / 1_000);
  const intent = await claimIntent(env, message.body.jobId, now);
  if (!intent) {
    message.ack();
    return;
  }
  try {
    if (await purgeOneR2Page(env, intent.uid)) {
      await releaseIntent(env, intent, {
        settledAt: null,
        delaySeconds: 1,
      });
      message.ack();
      return;
    }
    if (await purgeOneAudioAsset(env, intent.uid)) {
      await releaseIntent(env, intent, {
        settledAt: null,
        delaySeconds: 1,
      });
      message.ack();
      return;
    }
    await purgeOneD1Batch(env, intent.uid);
    if (!(await recordingResidualIsEmpty(env, intent.uid))) {
      await releaseIntent(env, intent, {
        settledAt: null,
        delaySeconds: 1,
      });
      message.ack();
      return;
    }
    if (intent.settledAt === null) {
      await releaseIntent(env, intent, {
        settledAt: now,
        delaySeconds: ZERO_SCAN_SETTLE_SECONDS,
      });
      message.ack();
      return;
    }
    const remaining = Math.max(
      0,
      intent.settledAt + ZERO_SCAN_SETTLE_SECONDS - now,
    );
    if (remaining > 0) {
      await releaseIntent(env, intent, { delaySeconds: remaining });
      message.ack();
      return;
    }
    await completeIntent(env, intent);
    message.ack();
  } catch {
    await markIntentFailed(env, intent);
    message.ack();
  }
}

export async function reconcileRecordingDeletions(
  env: JobsEnv,
  now = Math.floor(Date.now() / 1_000),
) {
  const rows = await env.APP_DB.prepare(
    `SELECT job_id FROM cf_recording_deletion_intents
     WHERE (status IN ('pending', 'failed') AND next_attempt_at <= ?)
        OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?)
     ORDER BY next_attempt_at, created_at LIMIT ?`,
  )
    .bind(now, now, RECONCILE_BATCH_SIZE)
    .all<{ job_id?: unknown }>();
  let dispatched = 0;
  for (const row of rows.results || []) {
    if (typeof row.job_id !== "string" || !row.job_id) continue;
    if (await queueRecordingDeletion(env, row.job_id)) dispatched += 1;
  }
  return dispatched;
}
