import type { JobsEnv } from "./env";

type FrameEnv = Pick<
  JobsEnv,
  "APP_DB" | "FRAME_REQUESTS" | "FRAME_REQUESTS_TEMPORARY"
>;
type FrameObject = {
  object_id: string;
  request_id: string;
  storage_id: string;
  uid: string;
  tier: "temporary" | "permanent";
  upload_id: string | null;
  attempts: number;
};

function objectKey(row: FrameObject) {
  if (
    !row.uid ||
    /[/\\]/.test(row.uid) ||
    !/^[a-f0-9]{32}$/.test(row.object_id)
  )
    throw new Error("invalid frame storage identity");
  return `frame-requests/${row.uid}/${row.object_id}.jpg`;
}

/** Abort every durable handle before deleting its object. A late complete
 * cannot recreate an image after the cleanup receipt has been removed. */
export async function cleanupFramePixels(
  env: FrameEnv,
  uid?: string,
  now = Math.floor(Date.now() / 1000)
) {
  const scope = uid === undefined ? "" : " AND uid=?";
  const args = uid === undefined ? [] : [uid];
  await env.APP_DB.prepare(
    `UPDATE cf_frame_requests SET state='expired',terminal_reason='expired',
      cleanup_state=CASE WHEN storage_id IS NULL THEN cleanup_state ELSE 'pending' END,
      cleanup_next_attempt_at=CASE WHEN storage_id IS NULL THEN NULL ELSE ? END
     WHERE (uid,request_id) IN (SELECT uid,request_id FROM cf_frame_requests
       WHERE state IN ('requested','claimed','uploaded') AND (expires_at<=?
        OR account_generation!=COALESCE((SELECT account_generation FROM cf_account_cutover a WHERE a.uid=cf_frame_requests.uid),0)
        OR EXISTS (SELECT 1 FROM cf_account_deletion_intents a WHERE a.uid=cf_frame_requests.uid)
        OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones a WHERE a.uid=cf_frame_requests.uid))${scope}
       ORDER BY expires_at LIMIT 32)`
  )
    .bind(now, now, ...args)
    .run();
  await env.APP_DB.prepare(
    `UPDATE cf_frame_objects SET phase='cleanup',next_attempt_at=0 WHERE phase NOT IN ('cleanup','deleted')
      AND ((phase IN ('writing','ready') AND write_expires_at<=?)
        OR EXISTS (SELECT 1 FROM cf_account_deletion_intents a WHERE a.uid=cf_frame_objects.uid)
        OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones a WHERE a.uid=cf_frame_objects.uid)
        OR NOT EXISTS (SELECT 1 FROM cf_frame_requests f WHERE f.uid=cf_frame_objects.uid AND f.request_id=cf_frame_objects.request_id))${scope}`
  )
    .bind(now, ...args)
    .run();
  const rows = await env.APP_DB.prepare(
    `SELECT object_id,request_id,storage_id,uid,tier,upload_id,attempts FROM cf_frame_objects WHERE phase='cleanup' AND next_attempt_at<=?${scope} ORDER BY created_at,object_id LIMIT 32`
  )
    .bind(now, ...args)
    .all<FrameObject>();
  let deleted = 0;
  for (const row of rows.results) {
    try {
      const bucket =
        row.tier === "temporary"
          ? env.FRAME_REQUESTS_TEMPORARY
          : env.FRAME_REQUESTS;
      if (!bucket) throw new Error("frame storage unavailable");
      const key = objectKey(row);
      if (row.upload_id) {
        try {
          await bucket.resumeMultipartUpload(key, row.upload_id).abort();
        } catch (error) {
          if (!(error instanceof Error) || !/\(10024\)$/.test(error.message))
            throw error;
        }
      }
      await bucket.delete(key);
      await env.APP_DB.prepare(
        "UPDATE cf_frame_objects SET phase='deleted',upload_id=NULL WHERE object_id=? AND phase='cleanup'"
      )
        .bind(row.object_id)
        .run();
      deleted++;
    } catch {
      const next = now + Math.min(3600, 30 * 2 ** Math.min(row.attempts, 7));
      await env.APP_DB.prepare(
        "UPDATE cf_frame_requests SET cleanup_state='failed',cleanup_attempts=MIN(cleanup_attempts+1,1000),cleanup_next_attempt_at=? " +
          "WHERE uid=? AND request_id=? AND storage_id=? AND state IN ('offline','pruned','failed','expired','cancelled')"
      )
        .bind(next, row.uid, row.request_id, row.storage_id)
        .run();
      await env.APP_DB.prepare(
        "UPDATE cf_frame_objects SET attempts=attempts+1,next_attempt_at=? WHERE object_id=? AND phase='cleanup'"
      )
        .bind(
          now + Math.min(3600, 30 * 2 ** Math.min(row.attempts, 7)),
          row.object_id
        )
        .run();
    }
  }
  await env.APP_DB.prepare(
    `DELETE FROM cf_frame_objects WHERE phase='deleted'${scope}`
  )
    .bind(...args)
    .run();
  await env.APP_DB.prepare(
    `UPDATE cf_frame_requests SET cleanup_state='deleted',cleanup_next_attempt_at=NULL
      WHERE state IN ('offline','pruned','failed','expired','cancelled') AND storage_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM cf_frame_objects o WHERE o.uid=cf_frame_requests.uid AND o.request_id=cf_frame_requests.request_id)${scope}`
  )
    .bind(...args)
    .run();
  await env.APP_DB.prepare(
    `DELETE FROM cf_frame_requests WHERE (uid,request_id) IN (SELECT uid,request_id FROM cf_frame_requests
      WHERE state IN ('offline','pruned','failed','expired','cancelled') AND expires_at<=?
      AND cleanup_state IN ('not_required','deleted')${scope} ORDER BY expires_at LIMIT 32)`
  )
    .bind(now, ...args)
    .run();
  return deleted;
}

export async function framePixelResidual(env: FrameEnv, uid: string) {
  if (!uid || /[/\\]/.test(uid)) throw new Error("invalid frame owner");
  const rows = await env.APP_DB.prepare(
    "SELECT count(*) AS count FROM cf_frame_objects WHERE uid=?"
  )
    .bind(uid)
    .first<{ count: number }>();
  if (!rows || !Number.isSafeInteger(rows.count) || rows.count < 0)
    throw new Error("frame residual unavailable");
  const counts = await Promise.all(
    [env.FRAME_REQUESTS_TEMPORARY, env.FRAME_REQUESTS].map(async (bucket) => {
      // An older runtime with no bucket binding could never write pixels without
      // a durable journal. Nonempty journals still require the missing binding.
      if (!bucket) {
        if (rows.count) throw new Error("frame storage unavailable");
        return 0;
      }
      return (await bucket.list({ prefix: `frame-requests/${uid}/`, limit: 1 }))
        .objects.length;
    })
  );
  return {
    empty: rows.count === 0 && counts.every((count) => count === 0),
    writes: rows.count,
  };
}
