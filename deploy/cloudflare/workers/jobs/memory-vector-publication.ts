import type { JobsEnv } from "./env";

// Queue and cron invocations have a 15-minute wall-clock bound. An abandoned
// writer retains ownership for that entire interval; an API timeout alone
// never proves that its external mutation was not accepted.
const WRITER_SECONDS = 15 * 60;
const CLEANUP_BATCH = 100;

type MemoryVector = { subId: string; values: number[] };
type Publication = {
  uid: string;
  sourceId: string;
  revision: number;
  content: string;
  namespace: string;
  model: string;
  vectors: MemoryVector[];
};
type Artifact = {
  vector_id: string;
  observed_present: number;
  delete_mutation: string | null;
};
type CleanupScope = { uid: string; source?: { id: string; throughRevision: number } };
function mutationId(result: unknown): string {
  const value = (result as { mutationId?: unknown } | null)?.mutationId;
  if (typeof value !== "string" || !value) {
    throw new Error("memory vector mutation receipt missing");
  }
  return value;
}

async function immutableId(attempt: string, subId: string): Promise<string> {
  const hash = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(`omi-memory-vector-v2\0${attempt}\0${subId}`),
  );
  return Array.from(new Uint8Array(hash), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export async function publishMemoryVectors(
  env: JobsEnv,
  publication: Publication,
): Promise<void> {
  const { uid, sourceId, revision, content, namespace, model } = publication;
  if (!publication.vectors.length || publication.vectors.length > 1000) {
    throw new Error("invalid memory vector publication size");
  }
  const attempt = crypto.randomUUID();
  const vectors = await Promise.all(publication.vectors.map(async (vector) => ({
    ...vector,
    id: await immutableId(attempt, vector.subId),
  })));
  // The source comparison runs inside every statement of the D1 transaction.
  const currentSource = `EXISTS (
    SELECT 1 FROM cf_memory_projection_sources
    WHERE uid = ? AND id = ? AND item_revision = ? AND content = ?
      AND operation = 'upsert'
  ) AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?)
    AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones
                    WHERE uid = ? AND expires_at > unixepoch())`;
  const sourceArgs = [uid, sourceId, revision, content, uid, uid];
  const claimed = await env.APP_DB.batch(vectors.map((vector) =>
    env.APP_DB.prepare(
      `INSERT INTO cf_memory_vector_artifacts
       (vector_id, uid, source_id, attempt_id, sub_id, source_version, model, writer_until)
       SELECT ?, ?, ?, ?, ?, ?, ?, unixepoch() + ? WHERE ${currentSource}`,
    ).bind(vector.id, uid, sourceId, attempt, vector.subId, revision, model,
      WRITER_SECONDS, ...sourceArgs),
  ));
  if (claimed.length !== vectors.length) throw new Error("incomplete memory vector claim");
  if (claimed.every((result) => result.meta.changes === 0)) return;
  if (claimed.some((result) => !result.success || result.meta.changes !== 1)) {
    throw new Error("incomplete memory vector claim");
  }

  mutationId(await env.MEMORY_VECTORS.upsert(vectors.map((vector) => ({
    id: vector.id, namespace, values: vector.values,
  }))));
  // Only a successful receipt releases the writer. An exception or a missing
  // response retains its full lease, because acceptance remains uncertain.
  // A retry always receives a different attempt ID.
  await env.APP_DB.prepare(
    "UPDATE cf_memory_vector_artifacts SET writer_done = 1 WHERE attempt_id = ?",
  ).bind(attempt).run();

  const admitted = `${currentSource} AND (
    SELECT COUNT(*) FROM cf_memory_vector_artifacts
    WHERE attempt_id = ? AND retired = 0 AND writer_done = 1
  ) = ?`;
  const args = [...sourceArgs, attempt, vectors.length];
  await env.APP_DB.batch([
    env.APP_DB.prepare(
      `DELETE FROM cf_vector_projection_state
       WHERE uid = ? AND source_id = ? AND projection_kind = 'memory' AND ${admitted}`,
    ).bind(uid, sourceId, ...args),
    ...vectors.map((vector) => env.APP_DB.prepare(
      `INSERT INTO cf_vector_projection_state
       (uid, projection_kind, source_id, sub_id, vector_id, source_version, model, updated_at)
       SELECT ?, 'memory', ?, ?, ?, ?, ?, unixepoch() WHERE ${admitted}`,
    ).bind(uid, sourceId, vector.subId, vector.id, revision, model, ...args)),
    env.APP_DB.prepare(
      `DELETE FROM cf_vector_projection_outbox
       WHERE uid = ? AND source_kind = 'memory' AND source_id = ?
         AND desired_version = ? AND operation = 'upsert' AND ${admitted}`,
    ).bind(uid, sourceId, revision, ...args),
  ]);
}

export async function retractMemoryVectors(
  env: JobsEnv,
  uid: string,
  sourceId: string,
  revision: number,
  operation: string,
): Promise<boolean> {
  // An old delete can only retire mappings up through its observed revision.
  // External IDs are subsequently deleted by the artifact owner, never by
  // source ID or by a stale snapshot of the current publication.
  await env.APP_DB.prepare(
    `DELETE FROM cf_vector_projection_state
     WHERE uid = ? AND source_id = ? AND projection_kind = 'memory'
       AND source_version <= ?`,
  ).bind(uid, sourceId, revision).run();
  const pending = await cleanupMemoryVectors(env, { uid, source: { id: sourceId, throughRevision: revision } });
  if (pending) return false;
  // Keep the exact outbox work durable until every eligible writer has drained
  // and the artifact owner has observed the external deletion. A newer source
  // revision is independent and must not be removed or delay this receipt.
  await env.APP_DB.prepare(
    `DELETE FROM cf_vector_projection_outbox
     WHERE uid = ? AND source_kind = 'memory' AND source_id = ?
       AND desired_version = ? AND operation = ?`,
  ).bind(uid, sourceId, revision, operation).run();
  return true;
}

export async function cleanupMemoryVectors(env: JobsEnv, scope?: CleanupScope): Promise<number> {
  let owner = scope === undefined ? "" : " AND uid = ?";
  const owners: Array<string | number> = scope === undefined ? [] : [scope.uid];
  if (scope?.source) {
    owner += " AND source_id = ? AND source_version <= ?";
    owners.push(scope.source.id, scope.source.throughRevision);
  }
  await env.APP_DB.prepare(
    `UPDATE cf_memory_vector_artifacts SET retired = 1
     WHERE retired = 0${owner}
       AND (writer_done = 1 OR writer_until <= unixepoch())
       AND NOT EXISTS (SELECT 1 FROM cf_vector_projection_state s
         WHERE s.projection_kind = 'memory'
           AND s.vector_id = cf_memory_vector_artifacts.vector_id)`,
  ).bind(...owners).run();
  const result = await env.APP_DB.prepare(
    `SELECT vector_id, observed_present, delete_mutation FROM cf_memory_vector_artifacts
     WHERE retired = 1${owner} AND (writer_done = 1 OR writer_until <= unixepoch())
     ORDER BY writer_until, vector_id LIMIT ?`,
  ).bind(...owners, CLEANUP_BATCH).all<Artifact>();
  const rows = result.results || [];
  if (rows.length) {
    const binding = env.MEMORY_VECTORS;
    const ids = rows.map((row) => row.vector_id);
    const present = new Set((await binding.getByIds(ids)).map((vector) => vector.id));
    const hasReceipt = rows.some((row) => row.delete_mutation !== null);
    const processed = hasReceipt ? (await binding.describe()).processedUpToMutation : null;
    const finished = rows.filter((row) => !present.has(row.vector_id) &&
      row.delete_mutation !== null &&
      (row.observed_present === 1 || processed === row.delete_mutation));
    if (finished.length) {
      await env.APP_DB.batch(finished.map((row) => env.APP_DB.prepare(
        "DELETE FROM cf_memory_vector_artifacts WHERE vector_id = ? AND retired = 1",
      ).bind(row.vector_id)));
    }
    const done = new Set(finished.map((row) => row.vector_id));
    const remaining = rows.filter((row) => !done.has(row.vector_id));
    if (remaining.length) {
      const receipt = mutationId(await binding.deleteByIds(remaining.map((row) => row.vector_id)));
      await env.APP_DB.batch(remaining.map((row) => env.APP_DB.prepare(
        `UPDATE cf_memory_vector_artifacts
         SET delete_mutation = ?, observed_present = MAX(observed_present, ?)
         WHERE vector_id = ? AND retired = 1`,
      ).bind(receipt, present.has(row.vector_id) ? 1 : 0, row.vector_id)));
    }
  }
  const pending = await env.APP_DB.prepare(
    `SELECT COUNT(*) AS count FROM cf_memory_vector_artifacts
     WHERE 1 = 1${owner}${scope === undefined ? " AND retired = 1" : ""}`,
  ).bind(...owners).first<{ count: number }>();
  return Number(pending?.count || 0);
}
