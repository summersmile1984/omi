import { Hono } from "hono";
import { decodeAuthContext, verifyAuthContextSignature } from "../shared/auth-context";
import type { JobMessage, JobsEnv } from "./env";

const app = new Hono<{ Bindings: JobsEnv }>();
const MAX_PAYLOAD_BYTES = 16_000;

app.get("/health", (c) => c.json({ status: "ok", service: "jobs", version: "cf-06" }));

app.post("/v1/cf/jobs", async (c) => {
  const encodedContext = c.req.header("x-omi-auth-context") ?? null;
  const context = decodeAuthContext(encodedContext);
  if (
    !context ||
    !(await verifyAuthContextSignature(
      encodedContext || "",
      c.req.header("x-omi-internal-signature") ?? null,
      c.env.INTERNAL_ASSERTION_SECRET,
    ))
  ) {
    return c.json({ error: "unauthorized" }, 401);
  }

  let body: { jobId?: unknown; kind?: unknown; payload?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "invalid JSON" }, 400);
  }
  const jobId = typeof body.jobId === "string" ? body.jobId.trim() : "";
  const kind = body.kind === "probe" ? body.kind : "";
  const payload = body.payload && typeof body.payload === "object" && !Array.isArray(body.payload) ? body.payload : {};
  if (!jobId || jobId.length > 128 || !kind) return c.json({ error: "invalid job" }, 400);
  const payloadJson = JSON.stringify(payload);
  if (payloadJson.length > MAX_PAYLOAD_BYTES) return c.json({ error: "job payload too large" }, 413);

  const now = Math.floor(Date.now() / 1000);
  const inserted = await c.env.APP_DB.prepare(
    "INSERT INTO cf_jobs (job_id, uid, kind, payload_json, status, attempts, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?) "
      + "ON CONFLICT(job_id) DO NOTHING",
  )
    .bind(jobId, context.uid, kind, payloadJson, now, now)
    .run();
  if (inserted.meta?.changes !== 1) {
    const existing = await c.env.APP_DB.prepare("SELECT status FROM cf_jobs WHERE job_id = ? AND uid = ?")
      .bind(jobId, context.uid)
      .first<{ status: string }>();
    if (!existing) return c.json({ error: "job id conflict" }, 409);
    return c.json({ status: "already_queued", jobId, state: existing.status });
  }
  const message: JobMessage = { jobId, uid: context.uid, kind, payload: payload as Record<string, unknown> };
  try {
    await c.env.JOBS.send(message);
  } catch {
    await c.env.APP_DB.prepare("UPDATE cf_jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE job_id = ?")
      .bind("queue unavailable", now, jobId)
      .run();
    return c.json({ error: "queue unavailable" }, 503);
  }
  return c.json({ status: "queued", jobId }, 202);
});

app.get("/v1/cf/jobs/:jobId", async (c) => {
  const encodedContext = c.req.header("x-omi-auth-context") ?? null;
  const context = decodeAuthContext(encodedContext);
  if (
    !context ||
    !(await verifyAuthContextSignature(
      encodedContext || "",
      c.req.header("x-omi-internal-signature") ?? null,
      c.env.INTERNAL_ASSERTION_SECRET,
    ))
  ) {
    return c.json({ error: "unauthorized" }, 401);
  }

  const jobId = c.req.param("jobId").trim();
  if (!jobId || jobId.length > 128) return c.json({ error: "invalid job id" }, 400);
  const row = await c.env.APP_DB.prepare(
    "SELECT job_id, kind, status, attempts, last_error, created_at, updated_at FROM cf_jobs WHERE job_id = ? AND uid = ?",
  )
    .bind(jobId, context.uid)
    .first<{
      job_id: string;
      kind: string;
      status: string;
      attempts: number;
      last_error: string | null;
      created_at: number;
      updated_at: number;
    }>();
  if (!row) return c.json({ error: "job not found" }, 404);
  return c.json({
    jobId: row.job_id,
    kind: row.kind,
    status: row.status,
    attempts: row.attempts,
    lastError: row.last_error,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  });
});

export default {
  fetch: app.fetch,
  async queue(batch: MessageBatch<JobMessage>, env: JobsEnv): Promise<void> {
    for (const message of batch.messages) {
      const row = await env.APP_DB.prepare("SELECT status, kind FROM cf_jobs WHERE job_id = ?")
        .bind(message.body.jobId)
        .first<{ status: string; kind: string }>();
      if (!row || row.status === "completed") {
        message.ack();
        continue;
      }
      const now = Math.floor(Date.now() / 1000);
      await env.APP_DB.prepare("UPDATE cf_jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE job_id = ?")
        .bind(now, message.body.jobId)
        .run();
      if (row.kind !== "probe" || message.body.kind !== "probe") {
        await env.APP_DB.prepare("UPDATE cf_jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE job_id = ?")
          .bind("unsupported job kind", now, message.body.jobId)
          .run();
        message.ack();
        continue;
      }
      await env.APP_DB.prepare("UPDATE cf_jobs SET status = 'completed', updated_at = ? WHERE job_id = ?")
        .bind(now, message.body.jobId)
        .run();
      message.ack();
    }
  },
};
