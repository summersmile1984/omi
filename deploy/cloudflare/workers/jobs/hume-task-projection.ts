import type { Context, Hono } from "hono";
import type { JobsEnv } from "./env";

const PROJECTION_PATH = "/internal/hume-task-projections/apply";
const MAX_BODY_BYTES = 16_000;
const HASH = /^[0-9a-f]{64}$/;
const IDENTIFIER = /^[^/\u0000\u0001-\u001f\u007f]{1,256}$/;
const TASK_ID = /^[^/\u0000\u0001-\u001f\u007f]{1,128}$/;
const REVIEW_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

type JobsContext = Context<{ Bindings: JobsEnv }>;

export type HumeTaskProjection = {
  request_id: string;
  task_id: string;
  action: "hume_mersure_user_expression";
  uid: string;
  conversation_id: string;
  account_generation: number;
  source_export_sha256: string;
  source_row_sha256: string;
  plan_hash: string;
  review_id: string;
};

type ProjectionPlan = {
  mode: "reviewed-apply";
  schema_version: 1;
  source: {
    kind: "firestore-task-export";
    export_sha256: string;
  };
  review_id: string;
  projection: Omit<HumeTaskProjection, "source_export_sha256" | "plan_hash" | "review_id"> & {
    source_row_sha256: string;
  };
  plan_hash: string;
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
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    difference |= (a[index] || 0) ^ (b[index] || 0);
  }
  return difference === 0;
}

function noStoreHeaders(): Record<string, string> {
  return { "cache-control": "no-store" };
}

function gate(c: JobsContext): Response | null {
  if (c.env.HUME_TASK_PROJECTION_STAGING_ENABLED !== "true") {
    return c.json(
      { error: "hume_task_projection_unavailable" },
      503,
      noStoreHeaders(),
    );
  }
  const expected = c.env.ADMIN_KEY;
  const provided = c.req.header("secret-key");
  if (!expected || !provided || !constantTimeEqual(expected, provided)) {
    return c.json({ error: "unauthorized" }, 403, noStoreHeaders());
  }
  return null;
}

async function readBody(c: JobsContext): Promise<unknown | null> {
  const declared = Number(c.req.header("content-length"));
  if (Number.isFinite(declared) && (declared < 1 || declared > MAX_BODY_BYTES))
    return null;
  const raw = await c.req.text();
  if (!raw || new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES)
    return null;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return null;
  }
}

function validIdentifier(value: unknown, pattern = IDENTIFIER): value is string {
  return typeof value === "string" && pattern.test(value);
}

function validGeneration(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function planWithoutHash(plan: ProjectionPlan) {
  return {
    mode: plan.mode,
    schema_version: plan.schema_version,
    source: plan.source,
    review_id: plan.review_id,
    projection: plan.projection,
  };
}

async function parsePlan(value: unknown): Promise<ProjectionPlan | null> {
  const plan = objectValue(value);
  const source = objectValue(plan?.source);
  const projection = objectValue(plan?.projection);
  if (
    !plan ||
    !source ||
    !projection ||
    plan.mode !== "reviewed-apply" ||
    plan.schema_version !== 1 ||
    source.kind !== "firestore-task-export" ||
    Object.keys(source).length !== 2 ||
    !HASH.test(String(source.export_sha256)) ||
    typeof plan.review_id !== "string" ||
    !REVIEW_ID.test(plan.review_id) ||
    !HASH.test(String(plan.plan_hash)) ||
    Object.keys(plan).sort().join(",") !==
      "mode,plan_hash,projection,review_id,schema_version,source"
  ) {
    return null;
  }
  if (
    Object.keys(projection).sort().join(",") !==
      "account_generation,action,conversation_id,request_id,source_row_sha256,task_id,uid" ||
    !validIdentifier(projection.request_id) ||
    !validIdentifier(projection.task_id, TASK_ID) ||
    projection.action !== "hume_mersure_user_expression" ||
    !validIdentifier(projection.uid) ||
    !validIdentifier(projection.conversation_id) ||
    !validGeneration(projection.account_generation) ||
    !HASH.test(String(projection.source_row_sha256))
  ) {
    return null;
  }
  const parsed = {
    mode: "reviewed-apply" as const,
    schema_version: 1 as const,
    source: {
      kind: "firestore-task-export" as const,
      export_sha256: source.export_sha256 as string,
    },
    review_id: plan.review_id,
    projection: {
      request_id: projection.request_id as string,
      task_id: projection.task_id as string,
      action: "hume_mersure_user_expression" as const,
      uid: projection.uid as string,
      conversation_id: projection.conversation_id as string,
      account_generation: projection.account_generation as number,
      source_row_sha256: projection.source_row_sha256 as string,
    },
    plan_hash: plan.plan_hash as string,
  };
  return (await sha256(stableJson(planWithoutHash(parsed)))) === parsed.plan_hash
    ? parsed
    : null;
}

function projectionRow(plan: ProjectionPlan): HumeTaskProjection {
  return {
    request_id: plan.projection.request_id,
    task_id: plan.projection.task_id,
    action: plan.projection.action,
    uid: plan.projection.uid,
    conversation_id: plan.projection.conversation_id,
    account_generation: plan.projection.account_generation,
    source_export_sha256: plan.source.export_sha256,
    source_row_sha256: plan.projection.source_row_sha256,
    plan_hash: plan.plan_hash,
    review_id: plan.review_id,
  };
}

async function currentProjection(env: JobsEnv, requestId: string) {
  return env.APP_DB.prepare(
    "SELECT request_id, task_id, action, uid, conversation_id, account_generation, " +
      "source_export_sha256, source_row_sha256, plan_hash, review_id " +
      "FROM cf_hume_task_projections WHERE request_id = ?",
  )
    .bind(requestId)
    .first<HumeTaskProjection>();
}

/**
 * Map a settled, identity-neutral result only through the attested task
 * projection. D1 triggers re-check deletion, generation, cutover and
 * conversation existence at the write boundary. Returning false is safe: the
 * result remains unmapped and can be retried after a reviewed projection is
 * applied.
 */
export async function mapHumeResultIfAttested(
  env: JobsEnv,
  requestId: string,
): Promise<boolean> {
  const projection = await currentProjection(env, requestId);
  if (!projection) return false;
  const result = await env.APP_DB.prepare(
    "SELECT event_id, mapping_status FROM cf_hume_webhook_results WHERE job_id = ?",
  )
    .bind(requestId)
    .first<{ event_id: string; mapping_status: string }>();
  if (!result || result.mapping_status !== "unmapped") return false;
  try {
    const updated = await env.APP_DB.prepare(
      "UPDATE cf_hume_webhook_results SET mapping_status = 'attested', mapped_uid = ?, " +
        "mapped_conversation_id = ?, mapped_account_generation = ? WHERE event_id = ? " +
        "AND mapping_status = 'unmapped' AND processing_status = 'completed'",
    )
      .bind(
        projection.uid,
        projection.conversation_id,
        projection.account_generation,
        result.event_id,
      )
      .run();
    return updated.meta?.changes === 1;
  } catch {
    return false;
  }
}

export function registerHumeTaskProjectionRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
): void {
  app.post(PROJECTION_PATH, async (c) => {
    const denied = gate(c);
    if (denied) return denied;
    const plan = await parsePlan(await readBody(c));
    if (!plan) return c.json({ error: "invalid_projection_plan" }, 422, noStoreHeaders());
    const row = projectionRow(plan);
    const existing = await currentProjection(c.env, row.request_id);
    if (existing) {
      const same = Object.keys(row).every(
        (key) => existing[key as keyof HumeTaskProjection] === row[key as keyof HumeTaskProjection],
      );
      if (!same) return c.json({ error: "projection_conflict" }, 409, noStoreHeaders());
      await mapHumeResultIfAttested(c.env, row.request_id);
      return c.json({ status: "already_applied", request_id: row.request_id }, 200, noStoreHeaders());
    }
    const now = Math.floor(Date.now() / 1_000);
    try {
      await c.env.APP_DB.prepare(
        "INSERT INTO cf_hume_task_projections " +
          "(request_id, task_id, action, uid, conversation_id, account_generation, " +
          "source_export_sha256, source_row_sha256, plan_hash, review_id, created_at, updated_at) " +
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      )
        .bind(
          row.request_id,
          row.task_id,
          row.action,
          row.uid,
          row.conversation_id,
          row.account_generation,
          row.source_export_sha256,
          row.source_row_sha256,
          row.plan_hash,
          row.review_id,
          now,
          now,
        )
        .run();
    } catch {
      const raced = await currentProjection(c.env, row.request_id);
      if (!raced) return c.json({ error: "projection_authority_changed" }, 409, noStoreHeaders());
      const same = Object.keys(row).every(
        (key) => raced[key as keyof HumeTaskProjection] === row[key as keyof HumeTaskProjection],
      );
      if (!same) return c.json({ error: "projection_conflict" }, 409, noStoreHeaders());
    }
    await mapHumeResultIfAttested(c.env, row.request_id);
    return c.json({ status: "applied", request_id: row.request_id }, 201, noStoreHeaders());
  });
}
