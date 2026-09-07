/** Queue deliveries wake one durable account scan; D1 owns all business retries. */
import type { Message } from "@cloudflare/workers-types";
import { createSignedAuthContext } from "../shared/auth-context";
import type { JobMessage, JobsEnv } from "./env";

const PROCESSOR_PATH = "/internal/memory/consolidation";
const RETRY_SECONDS = 10;
const DISPATCH_LIMIT = 50;

type Work = { uid: string; account_generation: number; next_attempt_at: number; lease_until: number | null };

function messageFor(work: Work): JobMessage {
  return { uid: work.uid, jobId: `memory-consolidation:${work.account_generation}`, kind: "memory_consolidate", payload: {} };
}

export async function processMemoryConsolidationMessage(message: Message<JobMessage>, env: JobsEnv): Promise<void> {
  const { uid, jobId, kind } = message.body;
  if (kind !== "memory_consolidate" || typeof uid !== "string" || !uid || uid.length > 256 || typeof jobId !== "string") {
    message.ack(); return;
  }
  const match = /^memory-consolidation:(0|[1-9][0-9]*)$/.exec(jobId);
  const generation = match ? Number(match[1]) : NaN;
  if (!Number.isSafeInteger(generation)) { message.ack(); return; }
  const work = await env.APP_DB.prepare(
    "SELECT uid,account_generation,next_attempt_at,lease_until FROM cf_memory_consolidation_dispatch " +
    "WHERE uid=? AND account_generation=? AND wake_sequence>handled_sequence " +
    "AND account_generation=COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid=?),0) " +
    "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=?) " +
    "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=?)"
  ).bind(uid,generation,uid,uid,uid).first<Work>();
  if (!work) { message.ack(); return; }
  const now = Math.floor(Date.now()/1000);
  let delay = Math.max(work.next_attempt_at, work.lease_until ?? 0) - now;
  if (delay <= 0) {
    if (!env.API_CORE) throw new Error("consolidation processor unavailable");
    const signed = await createSignedAuthContext(
      { uid, authority: "internal", requestId: jobId }, "api-core", "POST", PROCESSOR_PATH, env.INTERNAL_ASSERTION_SECRET
    );
    if (!signed) throw new Error("consolidation assertion unavailable");
    const response = await env.API_CORE.fetch(new Request(`https://api-core.internal${PROCESSOR_PATH}`, {
      method: "POST", headers: { "content-type": "application/json", "x-omi-auth-context": signed.encoded, "x-omi-internal-signature": signed.signature },
      body: JSON.stringify({ account_generation: generation }),
    }));
    if (!response.ok) {
      await response.body?.cancel();
      if ([400,404,409].includes(response.status)) { message.ack(); return; }
      message.retry({ delaySeconds: RETRY_SECONDS }); return;
    }
    const result = await response.json() as { pending?: unknown; retry_after_seconds?: unknown };
    if (typeof result.pending !== "boolean" || typeof result.retry_after_seconds !== "number" || !Number.isSafeInteger(result.retry_after_seconds) || result.retry_after_seconds < 0 || result.retry_after_seconds > 900) {
      message.retry({ delaySeconds: RETRY_SECONDS }); return;
    }
    if (!result.pending) { message.ack(); return; }
    delay = result.retry_after_seconds;
  }
  // Normal continuation is a new delivery, not an exhausted Queue retry.
  // If sending fails, leave this delivery unacknowledged; cron also rediscovers D1.
  await env.JOBS.send(messageFor(work), { delaySeconds: Math.max(1, Math.min(900, delay)) });
  message.ack();
}

export async function reconcileMemoryConsolidation(env: JobsEnv, now: number): Promise<void> {
  const result = await env.APP_DB.prepare(
    "SELECT uid,account_generation,next_attempt_at,lease_until FROM cf_memory_consolidation_dispatch d " +
    "WHERE wake_sequence>handled_sequence AND next_attempt_at<=? AND (lease_until IS NULL OR lease_until<=?) " +
    "AND account_generation=COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid=d.uid),0) " +
    "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=d.uid) " +
    "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=d.uid) " +
    "ORDER BY next_attempt_at,uid,account_generation LIMIT ?"
  ).bind(now,now,DISPATCH_LIMIT).all<Work>();
  const failures: unknown[] = [];
  for (const work of result.results ?? []) {
    try { await env.JOBS.send(messageFor(work)); } catch (error) { failures.push(error); }
  }
  if (failures.length) throw new Error("consolidation queue unavailable");
}
