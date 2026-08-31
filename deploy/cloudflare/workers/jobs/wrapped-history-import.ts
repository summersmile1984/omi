import type { Context, Hono } from "hono";
import type { JobsEnv } from "./env";

const REVIEW_PATH = "/internal/wrapped-history/reviews";
const MAX_BODY_BYTES = 8 * 1024 * 1024;
const MAX_ENTRIES = 40;
const REVIEW_TTL_SECONDS = 60 * 60;
const MAX_RESULT_BYTES = 256 * 1024;
const MAX_NESTED_OBJECT_BYTES = 32 * 1024;
const SUPPORTED_YEAR = 2025;
const SHA256 = /^[0-9a-f]{64}$/;
const UID = /^[^/\u0000\u0001-\u001f\u007f]{1,256}$/;
const IDENTIFIER = /^[^/\u0000\u0001-\u001f\u007f]{1,128}$/;
const REQUIRED_OBJECTS = [
  "decision_style",
  "memorable_days",
  "funniest_event",
  "most_embarrassing_event",
  "obsessions",
  "struggle",
  "personal_win",
];
const REQUIRED_ARRAYS = ["top_phrases", "top_buddies", "movie_recommendations"];
const SENSITIVE_KEYS = new Set([
  "access_token",
  "api_key",
  "authorization",
  "bearer",
  "client_secret",
  "credential",
  "credentials",
  "custom_token",
  "firebase_id_token",
  "id_token",
  "openai_api_key",
  "password",
  "private_key",
  "refresh_token",
  "secret",
  "secret_key",
  "token",
]);

type JobsContext = Context<{ Bindings: JobsEnv }>;

type ReviewEntry = {
  uid: string;
  year: number;
  jobId: string;
  requestFingerprint: string;
  sourceFingerprint: string;
  accountGeneration: number;
  resultJson: string;
  resultSha256: string;
  createdAt: number;
  updatedAt: number;
  sourceRowSha256: string;
  planHash: string;
};

type ReviewPlan = {
  mode: "dry-run";
  schema_version: 1;
  source: {
    kind: "firestore";
    collection: "users/{uid}/wrapped/{year}";
    export_sha256: string;
  };
  manifest_sha256: string;
  max_rows: number;
  total: number;
  stage: number;
  blocked: number;
  entries: ReviewEntry[];
};

type ReviewBatch = {
  review_id: string;
  manifest_sha256: string;
  entry_count: number;
  status: "approved" | "applied" | "revoked";
  expires_at: number;
};

type ExistingJob = {
  uid: string;
  year: number;
  job_id: string;
  request_fingerprint: string;
  source_fingerprint: string;
  account_generation: number;
  status: string;
  result_json: string | null;
};

type ExistingApply = {
  uid: string;
  year: number;
  review_id: string;
  job_id: string;
  source_row_sha256: string;
  plan_hash: string;
  account_generation: number;
  status: string;
};

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`)
      .join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) throw new Error("unsupported value");
  return encoded;
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function constantTimeEqual(left: string, right: string): boolean {
  const a = new TextEncoder().encode(left);
  const b = new TextEncoder().encode(right);
  let difference = a.length ^ b.length;
  for (let index = 0; index < Math.max(a.length, b.length); index += 1)
    difference |= (a[index] || 0) ^ (b[index] || 0);
  return difference === 0;
}

function noStoreHeaders(): Record<string, string> {
  return { "cache-control": "no-store" };
}

function adminAuthorized(c: JobsContext): boolean {
  const expected = c.env.ADMIN_KEY;
  const provided = c.req.header("secret-key");
  return Boolean(expected && provided && constantTimeEqual(provided, expected));
}

function gate(c: JobsContext): Response | null {
  if (c.env.WRAPPED_HISTORY_IMPORT_STAGING_ENABLED !== "true") {
    return c.json(
      { error: "wrapped_history_import_unavailable" },
      503,
      noStoreHeaders(),
    );
  }
  if (!adminAuthorized(c))
    return c.json({ error: "unauthorized" }, 403, noStoreHeaders());
  return null;
}

async function readBody(c: JobsContext): Promise<unknown | null> {
  const declared = Number(c.req.header("content-length"));
  if (Number.isFinite(declared) && (declared < 0 || declared > MAX_BODY_BYTES))
    return null;
  const bytes = await c.req.raw.arrayBuffer();
  if (bytes.byteLength < 1 || bytes.byteLength > MAX_BODY_BYTES) return null;
  try {
    const decoded = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    return JSON.parse(decoded) as unknown;
  } catch {
    return null;
  }
}

function sensitiveField(value: unknown, path = ""): string | null {
  if (!value || typeof value !== "object") return null;
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      const found = sensitiveField(value[index], `${path}[${index}]`);
      if (found) return found;
    }
    return null;
  }
  for (const [key, nested] of Object.entries(
    value as Record<string, unknown>,
  )) {
    const current = path ? `${path}.${key}` : key;
    if (SENSITIVE_KEYS.has(key.toLowerCase())) return current;
    const found = sensitiveField(nested, current);
    if (found) return found;
  }
  return null;
}

function validInteger(value: unknown, minimum = 0): value is number {
  return (
    typeof value === "number" && Number.isSafeInteger(value) && value >= minimum
  );
}

function parseResult(value: unknown): { json: string; sha256: string } | null {
  if (
    typeof value !== "string" ||
    new TextEncoder().encode(value).byteLength > MAX_RESULT_BYTES
  )
    return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return null;
  }
  const object = objectValue(parsed);
  if (!object || sensitiveField(object)) return null;
  let json: string;
  try {
    json = stableJson(object);
  } catch {
    return null;
  }
  if (new TextEncoder().encode(json).byteLength > MAX_RESULT_BYTES) return null;
  for (const key of REQUIRED_OBJECTS) {
    const nested = objectValue(object[key]);
    if (
      !nested ||
      new TextEncoder().encode(stableJson(nested)).byteLength >
        MAX_NESTED_OBJECT_BYTES
    )
      return null;
  }
  for (const key of REQUIRED_ARRAYS)
    if (!Array.isArray(object[key]) || (object[key] as unknown[]).length > 5)
      return null;
  return { json, sha256: "" };
}

async function normalizePlan(value: unknown): Promise<ReviewPlan | null> {
  const plan = objectValue(value);
  const source = objectValue(plan?.source);
  if (
    !plan ||
    plan.mode !== "dry-run" ||
    plan.schema_version !== 1 ||
    !source ||
    source.kind !== "firestore" ||
    source.collection !== "users/{uid}/wrapped/{year}" ||
    typeof source.export_sha256 !== "string" ||
    !SHA256.test(source.export_sha256) ||
    typeof plan.manifest_sha256 !== "string" ||
    !SHA256.test(plan.manifest_sha256) ||
    !Array.isArray(plan.entries) ||
    plan.entries.length < 1 ||
    plan.entries.length > MAX_ENTRIES ||
    !validInteger(plan.max_rows, 1) ||
    plan.max_rows > 5_000 ||
    plan.total !== plan.entries.length ||
    plan.stage !== plan.entries.length ||
    plan.blocked !== 0
  )
    return null;
  const entries: ReviewEntry[] = [];
  let previousKey = "";
  for (const raw of plan.entries) {
    const entry = objectValue(raw);
    if (!entry) return null;
    const allowed = new Set([
      "uid",
      "year",
      "jobId",
      "requestFingerprint",
      "sourceFingerprint",
      "accountGeneration",
      "resultJson",
      "resultSha256",
      "createdAt",
      "updatedAt",
      "sourceRowSha256",
      "action",
      "status",
      "lastError",
    ]);
    if (Object.keys(entry).some((key) => !allowed.has(key))) return null;
    if (sensitiveField(entry)) return null;
    const uid = entry.uid;
    const year = entry.year;
    if (
      typeof uid !== "string" ||
      !UID.test(uid) ||
      !validInteger(year) ||
      year !== SUPPORTED_YEAR ||
      typeof entry.jobId !== "string" ||
      !IDENTIFIER.test(entry.jobId) ||
      typeof entry.requestFingerprint !== "string" ||
      !SHA256.test(entry.requestFingerprint) ||
      typeof entry.sourceFingerprint !== "string" ||
      !SHA256.test(entry.sourceFingerprint) ||
      !validInteger(entry.accountGeneration) ||
      typeof entry.resultJson !== "string" ||
      typeof entry.resultSha256 !== "string" ||
      !SHA256.test(entry.resultSha256) ||
      !validInteger(entry.createdAt, 1) ||
      !validInteger(entry.updatedAt, 1) ||
      typeof entry.sourceRowSha256 !== "string" ||
      !SHA256.test(entry.sourceRowSha256) ||
      entry.action !== "stage" ||
      entry.status !== "planned" ||
      entry.lastError !== null
    )
      return null;
    const parsedResult = parseResult(entry.resultJson);
    if (!parsedResult) return null;
    const resultSha256 = await sha256(parsedResult.json);
    if (
      resultSha256 !== entry.resultSha256 ||
      parsedResult.json !== entry.resultJson
    )
      return null;
    const requestFingerprint = await sha256(`wrapped\0${uid}\0${year}`);
    if (
      requestFingerprint !== entry.requestFingerprint ||
      entry.jobId !==
        `wrapped-history-${year}-${requestFingerprint.slice(0, 40)}`
    )
      return null;
    const sourceRowSha256 = await sha256(
      stableJson({
        uid,
        year,
        source_fingerprint: entry.sourceFingerprint,
        account_generation: entry.accountGeneration,
        created_at: entry.createdAt,
        updated_at: entry.updatedAt,
        result_sha256: entry.resultSha256,
      }),
    );
    if (sourceRowSha256 !== entry.sourceRowSha256) return null;
    const key = `${uid}\0${year}`;
    if (key <= previousKey) return null;
    previousKey = key;
    entries.push({
      uid,
      year,
      jobId: entry.jobId,
      requestFingerprint,
      sourceFingerprint: entry.sourceFingerprint,
      accountGeneration: entry.accountGeneration,
      resultJson: parsedResult.json,
      resultSha256,
      createdAt: entry.createdAt,
      updatedAt: entry.updatedAt,
      sourceRowSha256,
      planHash: "",
    });
  }
  const manifestSha256 = await sha256(
    stableJson({
      schema_version: 1,
      source: {
        kind: "firestore",
        collection: "users/{uid}/wrapped/{year}",
        export_sha256: source.export_sha256,
        ...(typeof source.exported_at === "string"
          ? { exported_at: source.exported_at }
          : {}),
      },
      rows: entries.map((entry) => entry.sourceRowSha256),
    }),
  );
  if (manifestSha256 !== plan.manifest_sha256) return null;
  for (const entry of entries)
    entry.planHash = await sha256(
      `${manifestSha256}\0${entry.uid}\0${entry.year}\0${entry.sourceRowSha256}`,
    );
  return {
    mode: "dry-run",
    schema_version: 1,
    source: {
      kind: "firestore",
      collection: "users/{uid}/wrapped/{year}",
      export_sha256: source.export_sha256,
      ...(typeof source.exported_at === "string"
        ? { exported_at: source.exported_at }
        : {}),
    },
    manifest_sha256: manifestSha256,
    max_rows: plan.max_rows,
    total: entries.length,
    stage: entries.length,
    blocked: 0,
    entries,
  };
}

async function accountReady(
  c: JobsContext,
  entry: ReviewEntry,
  now: number,
): Promise<boolean> {
  const fenced = await c.env.APP_DB.prepare(
    "SELECT 1 AS blocked FROM cf_account_deletion_intents WHERE uid = ? UNION ALL SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ? LIMIT 1",
  )
    .bind(entry.uid, entry.uid, now)
    .first<{ blocked?: number }>();
  if (fenced) return false;
  const cutover = await c.env.APP_DB.prepare(
    "SELECT 1 AS ready FROM cf_account_cutover WHERE uid = ? AND state = 'new' AND checkpoint_phase = 'completed' AND destination_backend_bound = 1 AND account_generation = ? LIMIT 1",
  )
    .bind(entry.uid, entry.accountGeneration)
    .first<{ ready?: number }>();
  return Number(cutover?.ready) === 1;
}

function sqlEntry(entry: ReviewEntry, reviewId: string, now: number) {
  const values = [
    entry.uid,
    entry.year,
    entry.jobId,
    entry.requestFingerprint,
    entry.sourceFingerprint,
    entry.accountGeneration,
    entry.resultJson,
    entry.createdAt,
    entry.updatedAt,
  ];
  return { entry, reviewId, now, values };
}

async function review(c: JobsContext): Promise<Response> {
  const parsed = await readBody(c);
  const plan = await normalizePlan(parsed);
  if (!plan)
    return c.json(
      { error: "invalid_wrapped_history_plan" },
      422,
      noStoreHeaders(),
    );
  const now = Math.floor(Date.now() / 1_000);
  for (const entry of plan.entries) {
    if (!(await accountReady(c, entry, now)))
      return c.json(
        { error: "wrapped_history_account_not_ready" },
        409,
        noStoreHeaders(),
      );
  }
  const reviewId = crypto.randomUUID();
  try {
    const statements = [
      c.env.APP_DB.prepare(
        "INSERT INTO cf_wrapped_history_review_batches (review_id, manifest_sha256, entry_count, status, reviewed_at, expires_at, updated_at) VALUES (?, ?, ?, 'approved', ?, ?, ?)",
      ).bind(
        reviewId,
        plan.manifest_sha256,
        plan.entries.length,
        now,
        now + REVIEW_TTL_SECONDS,
        now,
      ),
      ...plan.entries.map((entry) =>
        c.env.APP_DB.prepare(
          "INSERT INTO cf_wrapped_history_review_items (review_id, uid, year, job_id, request_fingerprint, source_fingerprint, account_generation, result_json, result_sha256, created_at, updated_at, source_row_sha256, plan_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ).bind(
          reviewId,
          entry.uid,
          entry.year,
          entry.jobId,
          entry.requestFingerprint,
          entry.sourceFingerprint,
          entry.accountGeneration,
          entry.resultJson,
          entry.resultSha256,
          entry.createdAt,
          entry.updatedAt,
          entry.sourceRowSha256,
          entry.planHash,
        ),
      ),
    ];
    await c.env.APP_DB.batch(statements);
    return c.json(
      {
        review_id: reviewId,
        manifest_sha256: plan.manifest_sha256,
        entry_count: plan.entries.length,
        status: "approved",
        expires_at: now + REVIEW_TTL_SECONDS,
      },
      201,
      noStoreHeaders(),
    );
  } catch {
    return c.json(
      { error: "wrapped_history_review_unavailable" },
      503,
      noStoreHeaders(),
    );
  }
}

async function apply(c: JobsContext): Promise<Response> {
  const reviewId = c.req.param("reviewId") || "";
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      reviewId,
    )
  )
    return c.json({ error: "invalid_request" }, 400, noStoreHeaders());
  const now = Math.floor(Date.now() / 1_000);
  try {
    const batch = await c.env.APP_DB.prepare(
      "SELECT review_id, manifest_sha256, entry_count, status, expires_at FROM cf_wrapped_history_review_batches WHERE review_id = ? LIMIT 1",
    )
      .bind(reviewId)
      .first<ReviewBatch>();
    if (!batch) return c.json({ error: "not_found" }, 404, noStoreHeaders());
    if (batch.status === "revoked" || batch.expires_at <= now)
      return c.json(
        { error: "wrapped_history_review_expired" },
        409,
        noStoreHeaders(),
      );
    const rows = await c.env.APP_DB.prepare(
      "SELECT uid, year, job_id, request_fingerprint, source_fingerprint, account_generation, result_json, result_sha256, created_at, updated_at, source_row_sha256, plan_hash FROM cf_wrapped_history_review_items WHERE review_id = ? ORDER BY uid, year",
    )
      .bind(reviewId)
      .all<
        ReviewEntry & {
          job_id: string;
          request_fingerprint: string;
          source_fingerprint: string;
          account_generation: number;
          result_json: string;
          result_sha256: string;
          created_at: number;
          updated_at: number;
          source_row_sha256: string;
          plan_hash: string;
        }
      >();
    if (
      !Array.isArray(rows.results) ||
      rows.results.length !== batch.entry_count ||
      rows.results.length > MAX_ENTRIES
    )
      return c.json(
        { error: "wrapped_history_review_incomplete" },
        409,
        noStoreHeaders(),
      );
    const entries: ReviewEntry[] = rows.results.map((row) => ({
      uid: row.uid,
      year: Number(row.year),
      jobId: row.job_id,
      requestFingerprint: row.request_fingerprint,
      sourceFingerprint: row.source_fingerprint,
      accountGeneration: Number(row.account_generation),
      resultJson: row.result_json,
      resultSha256: row.result_sha256,
      createdAt: Number(row.created_at),
      updatedAt: Number(row.updated_at),
      sourceRowSha256: row.source_row_sha256,
      planHash: row.plan_hash,
    }));
    const existing = await Promise.all(
      entries.map((entry) =>
        c.env.APP_DB.prepare(
          "SELECT uid, year, job_id, request_fingerprint, source_fingerprint, account_generation, status, result_json FROM cf_wrapped_jobs WHERE uid = ? AND year = ? LIMIT 1",
        )
          .bind(entry.uid, entry.year)
          .first<ExistingJob>(),
      ),
    );
    const markers = await Promise.all(
      entries.map((entry) =>
        c.env.APP_DB.prepare(
          "SELECT uid, year, review_id, job_id, source_row_sha256, plan_hash, account_generation, status FROM cf_wrapped_history_applies WHERE uid = ? AND year = ? LIMIT 1",
        )
          .bind(entry.uid, entry.year)
          .first<ExistingApply>(),
      ),
    );
    let alreadyApplied = 0;
    for (let index = 0; index < entries.length; index += 1) {
      const entry = entries[index];
      if (!(await accountReady(c, entry, now)))
        return c.json(
          { error: "account_deletion_or_generation_fence" },
          409,
          noStoreHeaders(),
        );
      const job = existing[index];
      if (
        job &&
        (job.job_id !== entry.jobId ||
          job.request_fingerprint !== entry.requestFingerprint ||
          job.source_fingerprint !== entry.sourceFingerprint ||
          Number(job.account_generation) !== entry.accountGeneration ||
          job.status !== "completed" ||
          job.result_json !== entry.resultJson)
      )
        return c.json(
          { error: "wrapped_history_target_conflict" },
          409,
          noStoreHeaders(),
        );
      const marker = markers[index];
      if (marker) {
        if (
          marker.review_id !== reviewId ||
          marker.job_id !== entry.jobId ||
          marker.source_row_sha256 !== entry.sourceRowSha256 ||
          marker.plan_hash !== entry.planHash ||
          Number(marker.account_generation) !== entry.accountGeneration ||
          marker.status !== "applied"
        )
          return c.json(
            { error: "wrapped_history_apply_conflict" },
            409,
            noStoreHeaders(),
          );
        alreadyApplied += 1;
      }
    }
    const pending = entries.filter((_, index) => !markers[index]);
    const statements = pending.flatMap((entry) => {
      const values = sqlEntry(entry, reviewId, now).values;
      return [
        c.env.APP_DB.prepare(
          "INSERT INTO cf_wrapped_jobs (uid, year, job_id, request_fingerprint, source_fingerprint, account_generation, status, attempts, next_attempt_at, result_json, last_error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'completed', 0, ?, ?, NULL, ?, ?) ON CONFLICT(uid, year) DO NOTHING",
        ).bind(...values.slice(0, 6), now, values[6], values[7], values[8]),
        c.env.APP_DB.prepare(
          "INSERT INTO cf_wrapped_history_applies (uid, year, review_id, job_id, source_row_sha256, plan_hash, account_generation, status, applied_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)",
        ).bind(
          entry.uid,
          entry.year,
          reviewId,
          entry.jobId,
          entry.sourceRowSha256,
          entry.planHash,
          entry.accountGeneration,
          now,
          now,
        ),
      ];
    });
    if (pending.length)
      statements.push(
        c.env.APP_DB.prepare(
          "UPDATE cf_wrapped_history_review_batches SET status = 'applied', updated_at = ? WHERE review_id = ? AND status = 'approved'",
        ).bind(now, reviewId),
      );
    else
      await c.env.APP_DB.prepare(
        "UPDATE cf_wrapped_history_review_batches SET status = 'applied', updated_at = ? WHERE review_id = ? AND status = 'approved'",
      )
        .bind(now, reviewId)
        .run();
    if (statements.length) await c.env.APP_DB.batch(statements);
    return c.json(
      {
        status: "applied",
        review_id: reviewId,
        manifest_sha256: batch.manifest_sha256,
        entry_count: entries.length,
        applied_count: pending.length,
        already_applied_count: alreadyApplied,
      },
      200,
      noStoreHeaders(),
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (
      message.includes("account deletion fence") ||
      message.includes("authority changed")
    )
      return c.json(
        { error: "account_deletion_or_generation_fence" },
        409,
        noStoreHeaders(),
      );
    return c.json(
      { error: "wrapped_history_apply_unavailable" },
      503,
      noStoreHeaders(),
    );
  }
}

export function registerWrappedHistoryImportRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
): void {
  app.post(`${REVIEW_PATH}`, async (c) => {
    const blocked = gate(c);
    if (blocked) return blocked;
    return review(c);
  });
  app.post(`${REVIEW_PATH}/:reviewId/apply`, async (c) => {
    const blocked = gate(c);
    if (blocked) return blocked;
    return apply(c);
  });
}

export const wrappedHistoryImportConstants = {
  reviewPath: REVIEW_PATH,
  maxEntries: MAX_ENTRIES,
  maxBodyBytes: MAX_BODY_BYTES,
};
