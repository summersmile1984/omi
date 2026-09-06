import type { Approval } from "./approval";
import { objectKeys, sha256 } from "./approval";

export type WriterEnv = {
  APP_DB: D1Database;
  SCREEN_FRAMES: R2Bucket;
  SCREEN_FRAME_SIGNING_SECRET?: string;
  INTERNAL_ASSERTION_SECRET?: string;
};

type WriteRow = {
  jti: string;
  uid: string;
  conversation_id: string;
  phase: string;
  main_upload_id: string | null;
  thumbnail_upload_id: string | null;
};

async function alive(env: WriterEnv, p: Approval): Promise<boolean> {
  return Boolean(
    await env.APP_DB.prepare(
      `SELECT 1 AS active FROM cf_screen_frame_writes w
     JOIN cf_screen_frame_sets s ON s.uid = w.uid AND s.conversation_id = w.conversation_id
     JOIN cf_conversations c ON c.uid = w.uid AND c.id = w.conversation_id
     WHERE w.jti = ? AND w.phase = 'writing' AND w.expires_at > unixepoch() AND w.epoch = s.epoch AND c.status = 'completed'
       AND COALESCE((SELECT enabled FROM cf_screen_frame_settings WHERE uid = w.uid), 1) = 1
       AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = w.uid)
       AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = w.uid)`
    )
      .bind(p.jti)
      .first()
  );
}

async function abortUpload(
  env: WriterEnv,
  key: string,
  uploadId: string | null
): Promise<void> {
  if (!uploadId) return;
  // A completed/aborted upload can resolve or report documented NoSuchUpload.
  // Both mean no future completion is possible. Provider errors retain the
  // journal for retry; object deletion still follows a successful abort.
  try {
    await env.SCREEN_FRAMES.resumeMultipartUpload(key, uploadId).abort();
  } catch (error) {
    if (!(error instanceof Error) || !/\(10024\)$/.test(error.message))
      throw error;
  }
}

async function eraseRow(env: WriterEnv, row: WriteRow): Promise<void> {
  const keys = objectKeys(row.uid, row.jti);
  await abortUpload(env, keys[0], row.main_upload_id);
  await abortUpload(env, keys[1], row.thumbnail_upload_id);
  await env.SCREEN_FRAMES.delete(keys);
  await env.APP_DB.prepare(
    "UPDATE cf_screen_frame_writes SET phase = 'deleted', main_upload_id = NULL, thumbnail_upload_id = NULL WHERE jti = ? AND phase = 'cleanup'"
  )
    .bind(row.jti)
    .run();
}

/** Consume once before R2; every upload handle is durable before its first part. */
export async function writeApproved(
  env: WriterEnv,
  p: Approval,
  jpeg: Uint8Array,
  thumbnail: Uint8Array
): Promise<void> {
  if (
    (await sha256(jpeg)) !== p.canonical_sha256 ||
    (await sha256(thumbnail)) !== p.thumbnail_sha256
  )
    throw new Error("image digest mismatch");
  const reserved = await env.APP_DB.prepare(
    `INSERT OR IGNORE INTO cf_screen_frame_writes
     (jti, uid, conversation_id, attempt_id, epoch, expires_at, phase, canonical_sha256, thumbnail_sha256, metadata_json)
     VALUES (?, ?, ?, ?, ?, ?, 'writing', ?, ?, ?)`
  )
    .bind(
      p.jti,
      p.uid,
      p.conversation_id,
      p.attempt_id,
      p.epoch,
      p.expires_at,
      p.canonical_sha256,
      p.thumbnail_sha256,
      JSON.stringify(p.metadata)
    )
    .run();
  if (!reserved.success || reserved.meta.changes !== 1)
    throw new Error("approval already consumed");
  const keys = objectKeys(p.uid, p.jti);
  try {
    for (const [index, data] of [jpeg, thumbnail].entries()) {
      if (!(await alive(env, p))) throw new Error("screen frame write fenced");
      const upload = await env.SCREEN_FRAMES.createMultipartUpload(
        keys[index],
        { httpMetadata: { contentType: "image/jpeg" } }
      );
      const column = index === 0 ? "main_upload_id" : "thumbnail_upload_id";
      // An initiation that returns after deletion may contain no screen bytes.
      // It cannot upload its first part unless its handle is durably admitted.
      const registered = await env.APP_DB.prepare(
        `UPDATE cf_screen_frame_writes SET ${column} = ? WHERE jti = ? AND phase = 'writing'`
      )
        .bind(upload.uploadId, p.jti)
        .run();
      if (
        !registered.success ||
        registered.meta.changes !== 1 ||
        !(await alive(env, p))
      ) {
        await upload.abort();
        throw new Error("screen frame write fenced");
      }
      const part = await upload.uploadPart(1, data);
      if (!(await alive(env, p))) throw new Error("screen frame write fenced");
      await upload.complete([part]);
    }
    const ready = await env.APP_DB.prepare(
      `UPDATE cf_screen_frame_writes SET phase = 'ready' WHERE jti = ? AND phase = 'writing' AND expires_at > unixepoch()
         AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = cf_screen_frame_writes.uid)
         AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = cf_screen_frame_writes.uid)
         AND EXISTS (SELECT 1 FROM cf_screen_frame_sets s JOIN cf_conversations c ON c.uid = s.uid AND c.id = s.conversation_id
           WHERE s.uid = cf_screen_frame_writes.uid AND s.conversation_id = cf_screen_frame_writes.conversation_id
             AND s.epoch = cf_screen_frame_writes.epoch AND c.status = 'completed')
         AND COALESCE((SELECT enabled FROM cf_screen_frame_settings WHERE uid = cf_screen_frame_writes.uid), 1) = 1`
    )
      .bind(p.jti)
      .run();
    if (!ready.success || ready.meta.changes !== 1)
      throw new Error("screen frame write fenced");
  } catch (error) {
    await env.APP_DB.prepare(
      "UPDATE cf_screen_frame_writes SET phase = 'cleanup' WHERE jti = ? AND phase != 'deleted'"
    )
      .bind(p.jti)
      .run();
    const row = await env.APP_DB.prepare(
      "SELECT * FROM cf_screen_frame_writes WHERE jti = ? AND phase = 'cleanup'"
    )
      .bind(p.jti)
      .first<WriteRow>();
    if (row) await eraseRow(env, row);
    throw error;
  }
}

/** Bounded durable cleanup, also invoked by the account-erasure owner. */
export async function cleanup(env: WriterEnv, uid?: string): Promise<number> {
  const scope = uid === undefined ? "" : " AND uid = ?";
  await env.APP_DB.prepare(
    "UPDATE cf_screen_frame_writes SET phase = 'cleanup' WHERE phase IN ('writing', 'ready') AND expires_at <= unixepoch()" +
      scope
  )
    .bind(...(uid === undefined ? [] : [uid]))
    .run();
  const rows = await env.APP_DB.prepare(
    "SELECT * FROM cf_screen_frame_writes WHERE phase = 'cleanup'" +
      scope +
      " ORDER BY created_at LIMIT 32"
  )
    .bind(...(uid === undefined ? [] : [uid]))
    .all<WriteRow>();
  for (const row of rows.results) await eraseRow(env, row);
  await env.APP_DB.prepare(
    `DELETE FROM cf_screen_frame_writes WHERE phase = 'deleted'${scope}
       AND (expires_at <= unixepoch()
         OR EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = cf_screen_frame_writes.uid)
         OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = cf_screen_frame_writes.uid))`
  )
    .bind(...(uid === undefined ? [] : [uid]))
    .run();
  return rows.results.length;
}

export async function residual(env: WriterEnv, uid: string) {
  const row = await env.APP_DB.prepare(
    "SELECT count(*) AS count FROM cf_screen_frame_writes WHERE uid = ?"
  )
    .bind(uid)
    .first<{ count: number }>();
  const objects = await env.SCREEN_FRAMES.list({
    prefix: `${encodeURIComponent(uid)}/`,
    limit: 1,
  });
  return {
    uid,
    empty: row?.count === 0 && objects.objects.length === 0,
    writes: row?.count ?? null,
    objects_present: objects.objects.length !== 0,
  };
}
