import type { JobsEnv } from "./env";

export const VECTOR_EMBEDDING_MODEL = "@cf/baai/bge-m3";
export const VECTOR_EMBEDDING_DIMENSIONS = 1_024;

const MAX_EMBEDDING_TEXT_CHARS = 3_500;
const EMBEDDING_TEXT_OVERLAP = 350;
const EMBEDDING_BATCH_SIZE = 32;
const TRANSCRIPT_WINDOW = 8;
const TRANSCRIPT_STRIDE = 6;
const RECONCILE_SOURCE_BATCH_SIZE = 20;
const RECONCILE_OUTBOX_BATCH_SIZE = 20;
const MAX_PROJECTION_ATTEMPTS = 20;

export type VectorSourceKind =
  "memory" | "action_item" | "conversation" | "x_post";
export type VectorProjectionKind = VectorSourceKind | "transcript_chunk";

type VectorProjectionOutboxRow = {
  uid: string;
  source_kind: VectorSourceKind;
  source_id: string;
  desired_version: number;
  operation: "upsert" | "delete";
  attempts: number;
};

type VectorProjectionCandidate = Omit<VectorProjectionOutboxRow, "attempts">;

type VectorProjectionStateRow = {
  projection_kind: VectorProjectionKind;
  sub_id: string;
  vector_id: string;
};

type ProjectionDocument = {
  projectionKind: VectorProjectionKind;
  subId: string;
  text: string;
  createdAt?: number;
};

type ProjectionVector = ProjectionDocument & {
  vectorId: string;
  values: number[];
};

type VectorizeBinding = {
  upsert(vectors: Array<Record<string, unknown>>): Promise<unknown>;
  deleteByIds(ids: string[]): Promise<unknown>;
};

function sourceKind(value: unknown): VectorSourceKind | null {
  return value === "memory" ||
    value === "action_item" ||
    value === "conversation" ||
    value === "x_post"
    ? value
    : null;
}

function safeInteger(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function jsonObject(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "string" || value.length > 1_000_000) return null;
  try {
    return objectValue(JSON.parse(value));
  } catch {
    return null;
  }
}

function jsonArray(value: unknown): unknown[] {
  if (typeof value !== "string" || value.length > 1_000_000) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export async function vectorNamespace(uid: string): Promise<string> {
  return sha256Hex(`omi-vector-namespace\0${uid}`);
}

export async function vectorId(
  projectionKind: VectorProjectionKind,
  uid: string,
  sourceId: string,
  subId = "",
): Promise<string> {
  return sha256Hex(
    `omi-vector-v1\0${projectionKind}\0${uid}\0${sourceId}\0${subId}`,
  );
}

function splitText(value: string): string[] {
  const text = value.trim();
  if (!text) return [];
  if (text.length <= MAX_EMBEDDING_TEXT_CHARS) return [text];
  const chunks: string[] = [];
  const stride = MAX_EMBEDDING_TEXT_CHARS - EMBEDDING_TEXT_OVERLAP;
  for (let offset = 0; offset < text.length; offset += stride) {
    const chunk = text.slice(offset, offset + MAX_EMBEDDING_TEXT_CHARS).trim();
    if (chunk) chunks.push(chunk);
    if (offset + MAX_EMBEDDING_TEXT_CHARS >= text.length) break;
  }
  return chunks;
}

function transcriptDocuments(
  transcriptJson: unknown,
  createdAt: number,
): ProjectionDocument[] {
  const lines: string[] = [];
  for (const value of jsonArray(transcriptJson)) {
    const segment = objectValue(value);
    const text = typeof segment?.text === "string" ? segment.text.trim() : "";
    if (!text) continue;
    const speaker = segment?.is_user
      ? "User"
      : segment?.speaker_id === undefined || segment?.speaker_id === null
        ? "Speaker"
        : `Speaker ${String(segment.speaker_id)}`;
    lines.push(`${speaker}: ${text}`);
  }
  if (!lines.length) return [];
  const date = new Date(createdAt * 1_000);
  const dateHeader = Number.isFinite(date.getTime())
    ? `[Conversation on ${date.toISOString()}]\n`
    : "";
  const documents: ProjectionDocument[] = [];
  let index = 0;
  for (let offset = 0; offset < lines.length; offset += TRANSCRIPT_STRIDE) {
    const text = `${dateHeader}${lines
      .slice(offset, offset + TRANSCRIPT_WINDOW)
      .join("\n")}`.trim();
    if (text) {
      documents.push({
        projectionKind: "transcript_chunk",
        subId: String(index).padStart(6, "0"),
        text: text.slice(0, MAX_EMBEDDING_TEXT_CHARS),
        createdAt,
      });
    }
    if (offset + TRANSCRIPT_WINDOW >= lines.length) break;
    index += 1;
  }
  return documents;
}

function chunkDocuments(
  projectionKind: VectorProjectionKind,
  text: string,
  createdAt?: number,
): ProjectionDocument[] {
  return splitText(text).map((chunk, index) => ({
    projectionKind,
    subId: String(index).padStart(6, "0"),
    text: chunk,
    ...(createdAt === undefined ? {} : { createdAt }),
  }));
}

function embeddingVectors(result: unknown): number[][] | null {
  const payload = objectValue(result);
  const data = payload?.data;
  if (!Array.isArray(data)) return null;
  const vectors: number[][] = [];
  for (const raw of data) {
    if (!Array.isArray(raw) || raw.length !== VECTOR_EMBEDDING_DIMENSIONS) {
      return null;
    }
    const vector = raw.map(Number);
    if (vector.some((value) => !Number.isFinite(value))) return null;
    vectors.push(vector);
  }
  return vectors;
}

async function embedDocuments(
  env: JobsEnv,
  documents: ProjectionDocument[],
): Promise<ProjectionVector[]> {
  const vectors: ProjectionVector[] = [];
  for (
    let offset = 0;
    offset < documents.length;
    offset += EMBEDDING_BATCH_SIZE
  ) {
    const batch = documents.slice(offset, offset + EMBEDDING_BATCH_SIZE);
    const result = await env.AI.run(
      env.WORKERS_AI_VECTOR_MODEL || VECTOR_EMBEDDING_MODEL,
      { text: batch.map(({ text }) => text) },
    );
    const values = embeddingVectors(result);
    if (!values || values.length !== batch.length) {
      throw new Error("invalid vector embedding response");
    }
    for (const [index, document] of batch.entries()) {
      vectors.push({
        ...document,
        vectorId: "",
        values: values[index],
      });
    }
  }
  return vectors;
}

function vectorBinding(
  env: JobsEnv,
  kind: VectorProjectionKind,
): VectorizeBinding {
  if (kind === "memory") return env.MEMORY_VECTORS;
  if (kind === "action_item") return env.ACTION_ITEM_VECTORS;
  if (kind === "conversation") return env.CONVERSATION_VECTORS;
  if (kind === "x_post") return env.X_POST_VECTORS;
  return env.TRANSCRIPT_CHUNK_VECTORS;
}

function sourceKinds(projectionKind: VectorProjectionKind): VectorSourceKind {
  return projectionKind === "transcript_chunk"
    ? "conversation"
    : projectionKind;
}

async function sourceDocuments(
  env: JobsEnv,
  uid: string,
  kind: VectorSourceKind,
  sourceId: string,
): Promise<{ version: number; documents: ProjectionDocument[] } | null> {
  if (kind === "memory") {
    const row = await env.APP_DB.prepare(
      `SELECT content, updated_at, deleted_at, invalid_at, user_review, memory_tier
       FROM cf_memories WHERE uid = ? AND id = ?`,
    )
      .bind(uid, sourceId)
      .first<Record<string, unknown>>();
    const version = safeInteger(row?.updated_at);
    if (
      !row ||
      version === null ||
      row.deleted_at !== null ||
      row.invalid_at !== null ||
      Number(row.user_review) === 0 ||
      row.memory_tier === "archive" ||
      typeof row.content !== "string"
    ) {
      return null;
    }
    return {
      version,
      documents: chunkDocuments("memory", row.content),
    };
  }
  if (kind === "action_item") {
    const row = await env.APP_DB.prepare(
      `SELECT description, updated_at, deleted
       FROM cf_action_items WHERE uid = ? AND id = ?`,
    )
      .bind(uid, sourceId)
      .first<Record<string, unknown>>();
    const version = safeInteger(row?.updated_at);
    if (
      !row ||
      version === null ||
      Number(row.deleted) !== 0 ||
      typeof row.description !== "string"
    ) {
      return null;
    }
    return {
      version,
      documents: chunkDocuments("action_item", row.description),
    };
  }
  if (kind === "x_post") {
    const row = await env.APP_DB.prepare(
      `SELECT text, created_at, updated_at
       FROM cf_x_posts WHERE uid = ? AND id = ?`,
    )
      .bind(uid, sourceId)
      .first<Record<string, unknown>>();
    const version = safeInteger(row?.updated_at);
    const createdAt = safeInteger(row?.created_at);
    if (
      !row ||
      version === null ||
      createdAt === null ||
      typeof row.text !== "string"
    ) {
      return null;
    }
    return {
      version,
      documents: chunkDocuments("x_post", row.text, createdAt),
    };
  }
  const row = await env.APP_DB.prepare(
    `SELECT structured_json, transcript_segments_json, created_at,
            COALESCE(updated_at, created_at) AS source_version,
            discarded, status
     FROM cf_conversations WHERE uid = ? AND id = ?`,
  )
    .bind(uid, sourceId)
    .first<Record<string, unknown>>();
  const version = safeInteger(row?.source_version);
  const createdAt = safeInteger(row?.created_at);
  const structured = jsonObject(row?.structured_json);
  if (
    !row ||
    version === null ||
    createdAt === null ||
    Number(row.discarded) !== 0 ||
    row.status !== "completed" ||
    !structured
  ) {
    return null;
  }
  const title = typeof structured.title === "string" ? structured.title : "";
  const overview =
    typeof structured.overview === "string" ? structured.overview : "";
  const summary = `${title}\n${overview}`.trim();
  const documents = [
    ...chunkDocuments("conversation", summary, createdAt),
    ...transcriptDocuments(row.transcript_segments_json, createdAt),
  ];
  return documents.length ? { version, documents } : null;
}

async function existingState(
  env: JobsEnv,
  uid: string,
  kind: VectorSourceKind,
  sourceId: string,
): Promise<VectorProjectionStateRow[]> {
  const projectionKinds =
    kind === "conversation" ? ["conversation", "transcript_chunk"] : [kind];
  const placeholders = projectionKinds.map(() => "?").join(",");
  const result = await env.APP_DB.prepare(
    `SELECT projection_kind, sub_id, vector_id
     FROM cf_vector_projection_state
     WHERE uid = ? AND source_id = ? AND projection_kind IN (${placeholders})`,
  )
    .bind(uid, sourceId, ...projectionKinds)
    .all<VectorProjectionStateRow>();
  return (result.results || []).filter(
    (row) =>
      typeof row.vector_id === "string" &&
      typeof row.sub_id === "string" &&
      (row.projection_kind === "memory" ||
        row.projection_kind === "action_item" ||
        row.projection_kind === "conversation" ||
        row.projection_kind === "transcript_chunk" ||
        row.projection_kind === "x_post"),
  );
}

async function deleteVectorGroups(
  env: JobsEnv,
  rows: Array<Pick<VectorProjectionStateRow, "projection_kind" | "vector_id">>,
): Promise<void> {
  const groups = new Map<VectorProjectionKind, string[]>();
  for (const row of rows) {
    const ids = groups.get(row.projection_kind) || [];
    ids.push(row.vector_id);
    groups.set(row.projection_kind, ids);
  }
  for (const [kind, ids] of groups) {
    for (let offset = 0; offset < ids.length; offset += 1_000) {
      await vectorBinding(env, kind).deleteByIds(
        ids.slice(offset, offset + 1_000),
      );
    }
  }
}

async function deleteProjection(
  env: JobsEnv,
  row: VectorProjectionOutboxRow,
): Promise<void> {
  const state = await existingState(
    env,
    row.uid,
    row.source_kind,
    row.source_id,
  );
  await deleteVectorGroups(env, state);
  const kinds =
    row.source_kind === "conversation"
      ? ["conversation", "transcript_chunk"]
      : [row.source_kind];
  const placeholders = kinds.map(() => "?").join(",");
  await env.APP_DB.batch([
    env.APP_DB.prepare(
      `DELETE FROM cf_vector_projection_state
       WHERE uid = ? AND source_id = ? AND projection_kind IN (${placeholders})`,
    ).bind(row.uid, row.source_id, ...kinds),
    env.APP_DB.prepare(
      `DELETE FROM cf_vector_projection_outbox
       WHERE uid = ? AND source_kind = ? AND source_id = ?
         AND desired_version = ? AND operation = 'delete'`,
    ).bind(row.uid, row.source_kind, row.source_id, row.desired_version),
  ]);
}

async function upsertProjection(
  env: JobsEnv,
  row: VectorProjectionOutboxRow,
  source: { version: number; documents: ProjectionDocument[] },
): Promise<void> {
  const namespace = await vectorNamespace(row.uid);
  const embedded = await embedDocuments(env, source.documents);
  for (const vector of embedded) {
    vector.vectorId = await vectorId(
      vector.projectionKind,
      row.uid,
      row.source_id,
      vector.subId,
    );
  }
  const groups = new Map<VectorProjectionKind, ProjectionVector[]>();
  for (const vector of embedded) {
    const values = groups.get(vector.projectionKind) || [];
    values.push(vector);
    groups.set(vector.projectionKind, values);
  }
  for (const [kind, vectors] of groups) {
    for (let offset = 0; offset < vectors.length; offset += 1_000) {
      await vectorBinding(env, kind).upsert(
        vectors.slice(offset, offset + 1_000).map((vector) => ({
          id: vector.vectorId,
          namespace,
          values: vector.values,
          ...(vector.createdAt === undefined
            ? {}
            : { metadata: { created_at: vector.createdAt } }),
        })),
      );
    }
  }

  const current = await existingState(
    env,
    row.uid,
    row.source_kind,
    row.source_id,
  );
  const desiredIds = new Set(embedded.map(({ vectorId }) => vectorId));
  await deleteVectorGroups(
    env,
    current.filter(({ vector_id }) => !desiredIds.has(vector_id)),
  );

  const kinds =
    row.source_kind === "conversation"
      ? ["conversation", "transcript_chunk"]
      : [row.source_kind];
  const placeholders = kinds.map(() => "?").join(",");
  const now = Math.floor(Date.now() / 1_000);
  const statements = [
    env.APP_DB.prepare(
      `DELETE FROM cf_vector_projection_state
       WHERE uid = ? AND source_id = ? AND projection_kind IN (${placeholders})
         AND source_version <= ?`,
    ).bind(row.uid, row.source_id, ...kinds, source.version),
    ...embedded.map((vector) =>
      env.APP_DB.prepare(
        `INSERT INTO cf_vector_projection_state
         (uid, projection_kind, source_id, sub_id, vector_id, source_version, model, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(uid, projection_kind, source_id, sub_id) DO UPDATE SET
           vector_id = excluded.vector_id,
           source_version = excluded.source_version,
           model = excluded.model,
           updated_at = excluded.updated_at
         WHERE excluded.source_version >= cf_vector_projection_state.source_version`,
      ).bind(
        row.uid,
        vector.projectionKind,
        row.source_id,
        vector.subId,
        vector.vectorId,
        source.version,
        env.WORKERS_AI_VECTOR_MODEL || VECTOR_EMBEDDING_MODEL,
        now,
      ),
    ),
    env.APP_DB.prepare(
      `DELETE FROM cf_vector_projection_outbox
       WHERE uid = ? AND source_kind = ? AND source_id = ?
         AND desired_version = ? AND operation = 'upsert'`,
    ).bind(row.uid, row.source_kind, row.source_id, source.version),
  ];
  await env.APP_DB.batch(statements);
}

async function markProjectionFailure(
  env: JobsEnv,
  row: VectorProjectionOutboxRow,
): Promise<void> {
  const now = Math.floor(Date.now() / 1_000);
  const attempts = Math.min(MAX_PROJECTION_ATTEMPTS, row.attempts + 1);
  const delay = Math.min(3_600, 2 ** Math.min(attempts, 10));
  await env.APP_DB.prepare(
    `UPDATE cf_vector_projection_outbox
     SET attempts = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
     WHERE uid = ? AND source_kind = ? AND source_id = ?
       AND desired_version = ? AND operation = ?`,
  )
    .bind(
      attempts,
      now + delay,
      "vector projection unavailable",
      now,
      row.uid,
      row.source_kind,
      row.source_id,
      row.desired_version,
      row.operation,
    )
    .run();
}

export async function processVectorProjection(
  env: JobsEnv,
  row: VectorProjectionOutboxRow,
): Promise<boolean> {
  const kind = sourceKind(row.source_kind);
  if (!kind || !row.uid || !row.source_id) return false;
  try {
    const deletion = await env.APP_DB.prepare(
      `SELECT 1 AS fenced FROM cf_account_deletion_intents WHERE uid = ?
       UNION ALL
       SELECT 1 AS fenced FROM cf_account_deletion_tombstones
       WHERE uid = ? AND expires_at > unixepoch()
       LIMIT 1`,
    )
      .bind(row.uid, row.uid)
      .first<{ fenced: number }>();
    if (deletion) return false;
    const source = await sourceDocuments(env, row.uid, kind, row.source_id);
    if (row.operation === "delete" || source === null) {
      await deleteProjection(env, { ...row, source_kind: kind });
      return true;
    }
    await upsertProjection(env, { ...row, source_kind: kind }, source);
    return true;
  } catch {
    await markProjectionFailure(env, { ...row, source_kind: kind });
    return false;
  }
}

export async function processVectorProjectionMessage(
  message: Message<{
    jobId: string;
    uid: string;
    kind: string;
    payload: Record<string, unknown>;
  }>,
  env: JobsEnv,
): Promise<void> {
  const kind = sourceKind(message.body.payload.sourceKind);
  const sourceId =
    typeof message.body.payload.sourceId === "string"
      ? message.body.payload.sourceId
      : "";
  if (!kind || !message.body.uid || !sourceId || sourceId.length > 256) {
    message.ack();
    return;
  }
  const row = await env.APP_DB.prepare(
    `SELECT uid, source_kind, source_id, desired_version, operation, attempts
     FROM cf_vector_projection_outbox
     WHERE uid = ? AND source_kind = ? AND source_id = ?`,
  )
    .bind(message.body.uid, kind, sourceId)
    .first<VectorProjectionOutboxRow>();
  if (!row) {
    message.ack();
    return;
  }
  if (await processVectorProjection(env, row)) message.ack();
  else message.retry({ delaySeconds: 10 });
}

async function enqueueCandidates(
  env: JobsEnv,
  candidates: VectorProjectionCandidate[],
  now: number,
): Promise<void> {
  if (!candidates.length) return;
  await env.APP_DB.batch(
    candidates.map((candidate) =>
      env.APP_DB.prepare(
        `INSERT INTO cf_vector_projection_outbox
         (uid, source_kind, source_id, desired_version, operation, attempts,
          next_attempt_at, last_error, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, 0, ?, NULL, ?, ?)
         ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET
           desired_version = excluded.desired_version,
           operation = excluded.operation,
           attempts = 0,
           next_attempt_at = excluded.next_attempt_at,
           last_error = NULL,
           updated_at = excluded.updated_at
         WHERE excluded.desired_version >= cf_vector_projection_outbox.desired_version`,
      ).bind(
        candidate.uid,
        candidate.source_kind,
        candidate.source_id,
        candidate.desired_version,
        candidate.operation,
        now,
        now,
        now,
      ),
    ),
  );
}

async function seedMissingProjections(
  env: JobsEnv,
  now: number,
): Promise<void> {
  const model = env.WORKERS_AI_VECTOR_MODEL || VECTOR_EMBEDDING_MODEL;
  const queries = [
    env.APP_DB.prepare(
      `SELECT m.uid, 'memory' AS source_kind, m.id AS source_id,
              m.updated_at AS desired_version, 'upsert' AS operation
       FROM cf_memories m
       LEFT JOIN cf_vector_projection_state s
         ON s.uid = m.uid AND s.projection_kind = 'memory'
        AND s.source_id = m.id AND s.sub_id = '000000'
       WHERE m.deleted_at IS NULL AND m.invalid_at IS NULL
         AND m.memory_tier != 'archive' AND COALESCE(m.user_review, 1) != 0
         AND (
           s.source_version IS NULL OR s.source_version < m.updated_at OR
           s.model != ?
         )
         AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents d WHERE d.uid = m.uid)
         AND NOT EXISTS (
           SELECT 1 FROM cf_account_deletion_tombstones t
           WHERE t.uid = m.uid AND t.expires_at > ?
         )
       ORDER BY m.updated_at, m.uid, m.id LIMIT ?`,
    ).bind(model, now, RECONCILE_SOURCE_BATCH_SIZE),
    env.APP_DB.prepare(
      `SELECT a.uid, 'action_item' AS source_kind, a.id AS source_id,
              a.updated_at AS desired_version, 'upsert' AS operation
       FROM cf_action_items a
       LEFT JOIN cf_vector_projection_state s
         ON s.uid = a.uid AND s.projection_kind = 'action_item'
        AND s.source_id = a.id AND s.sub_id = '000000'
       WHERE a.deleted = 0
         AND (
           s.source_version IS NULL OR s.source_version < a.updated_at OR
           s.model != ?
         )
         AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents d WHERE d.uid = a.uid)
         AND NOT EXISTS (
           SELECT 1 FROM cf_account_deletion_tombstones t
           WHERE t.uid = a.uid AND t.expires_at > ?
         )
       ORDER BY a.updated_at, a.uid, a.id LIMIT ?`,
    ).bind(model, now, RECONCILE_SOURCE_BATCH_SIZE),
    env.APP_DB.prepare(
      `SELECT c.uid, 'conversation' AS source_kind, c.id AS source_id,
              COALESCE(c.updated_at, c.created_at) AS desired_version,
              'upsert' AS operation
       FROM cf_conversations c
       LEFT JOIN cf_vector_projection_state s
         ON s.uid = c.uid AND s.projection_kind = 'conversation'
        AND s.source_id = c.id AND s.sub_id = '000000'
       WHERE c.discarded = 0 AND c.status = 'completed'
         AND (
           s.source_version IS NULL OR
           s.source_version < COALESCE(c.updated_at, c.created_at) OR
           s.model != ?
         )
         AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents d WHERE d.uid = c.uid)
         AND NOT EXISTS (
           SELECT 1 FROM cf_account_deletion_tombstones t
           WHERE t.uid = c.uid AND t.expires_at > ?
         )
       ORDER BY COALESCE(c.updated_at, c.created_at), c.uid, c.id LIMIT ?`,
    ).bind(model, now, RECONCILE_SOURCE_BATCH_SIZE),
    env.APP_DB.prepare(
      `SELECT x.uid, 'x_post' AS source_kind, x.id AS source_id,
              x.updated_at AS desired_version, 'upsert' AS operation
       FROM cf_x_posts x
       LEFT JOIN cf_vector_projection_state s
         ON s.uid = x.uid AND s.projection_kind = 'x_post'
        AND s.source_id = x.id AND s.sub_id = '000000'
       WHERE length(trim(x.text)) > 0
         AND (
           s.source_version IS NULL OR s.source_version < x.updated_at OR
           s.model != ?
         )
         AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents d WHERE d.uid = x.uid)
         AND NOT EXISTS (
           SELECT 1 FROM cf_account_deletion_tombstones t
           WHERE t.uid = x.uid AND t.expires_at > ?
         )
       ORDER BY x.updated_at, x.uid, x.id LIMIT ?`,
    ).bind(model, now, RECONCILE_SOURCE_BATCH_SIZE),
    env.APP_DB.prepare(
      `SELECT s.uid,
              CASE WHEN s.projection_kind = 'transcript_chunk'
                   THEN 'conversation' ELSE s.projection_kind END AS source_kind,
              s.source_id, MAX(s.source_version) AS desired_version,
              'delete' AS operation
       FROM cf_vector_projection_state s
       LEFT JOIN cf_memories m
         ON s.projection_kind = 'memory' AND m.uid = s.uid AND m.id = s.source_id
           AND m.deleted_at IS NULL AND m.invalid_at IS NULL
           AND m.memory_tier != 'archive' AND COALESCE(m.user_review, 1) != 0
       LEFT JOIN cf_action_items a
         ON s.projection_kind = 'action_item' AND a.uid = s.uid AND a.id = s.source_id AND a.deleted = 0
       LEFT JOIN cf_conversations c
         ON s.projection_kind IN ('conversation', 'transcript_chunk')
           AND c.uid = s.uid AND c.id = s.source_id AND c.discarded = 0 AND c.status = 'completed'
       LEFT JOIN cf_x_posts x
         ON s.projection_kind = 'x_post' AND x.uid = s.uid AND x.id = s.source_id
           AND length(trim(x.text)) > 0
       WHERE (
         (s.projection_kind = 'memory' AND m.id IS NULL) OR
         (s.projection_kind = 'action_item' AND a.id IS NULL) OR
         (s.projection_kind IN ('conversation', 'transcript_chunk') AND c.id IS NULL) OR
         (s.projection_kind = 'x_post' AND x.id IS NULL)
       )
         AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents d WHERE d.uid = s.uid)
         AND NOT EXISTS (
           SELECT 1 FROM cf_account_deletion_tombstones t
           WHERE t.uid = s.uid AND t.expires_at > ?
         )
       GROUP BY s.uid, source_kind, s.source_id
       ORDER BY desired_version, s.uid, s.source_id LIMIT ?`,
    ).bind(now, RECONCILE_SOURCE_BATCH_SIZE),
  ];
  const results = await env.APP_DB.batch<Record<string, unknown>>(queries);
  for (const result of results) {
    const candidates: VectorProjectionCandidate[] = (
      result.results || []
    ).flatMap((value) => {
      const kind = sourceKind(value.source_kind);
      const version = safeInteger(value.desired_version);
      return kind &&
        typeof value.uid === "string" &&
        typeof value.source_id === "string" &&
        version !== null &&
        (value.operation === "upsert" || value.operation === "delete")
        ? [
            {
              uid: value.uid,
              source_kind: kind,
              source_id: value.source_id,
              desired_version: version,
              operation: value.operation,
            },
          ]
        : [];
    });
    await enqueueCandidates(env, candidates, now);
  }
}

export async function reconcileVectorProjections(
  env: JobsEnv,
  now = Math.floor(Date.now() / 1_000),
): Promise<number> {
  await seedMissingProjections(env, now);
  const result = await env.APP_DB.prepare(
    `SELECT uid, source_kind, source_id, desired_version, operation, attempts
     FROM cf_vector_projection_outbox
     WHERE next_attempt_at <= ?
     ORDER BY next_attempt_at, updated_at, uid, source_kind, source_id
     LIMIT ?`,
  )
    .bind(now, RECONCILE_OUTBOX_BATCH_SIZE)
    .all<VectorProjectionOutboxRow>();
  let completed = 0;
  for (const row of result.results || []) {
    if (await processVectorProjection(env, row)) completed += 1;
  }
  return completed;
}

export async function purgeAccountVectorProjections(
  env: JobsEnv,
  uid: string,
): Promise<number> {
  const result = await env.APP_DB.prepare(
    `SELECT projection_kind, sub_id, vector_id
     FROM cf_vector_projection_state
     WHERE uid = ? ORDER BY projection_kind, source_id, sub_id LIMIT 1000`,
  )
    .bind(uid)
    .all<VectorProjectionStateRow>();
  const rows = (result.results || []).filter(
    (row) => typeof row.vector_id === "string",
  );
  if (!rows.length) return 0;
  await deleteVectorGroups(env, rows);
  await env.APP_DB.batch(
    rows.map((row) =>
      env.APP_DB.prepare(
        `DELETE FROM cf_vector_projection_state
         WHERE uid = ? AND projection_kind = ? AND vector_id = ?`,
      ).bind(uid, row.projection_kind, row.vector_id),
    ),
  );
  return rows.length;
}

export function vectorSourceKindForProjection(
  projectionKind: VectorProjectionKind,
): VectorSourceKind {
  return sourceKinds(projectionKind);
}
