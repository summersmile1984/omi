/** D1 owns recurrence receipts; Queue and Cron deliver recoverable wake-up hints. */
import type { Message } from "@cloudflare/workers-types";
import { createSignedAuthContext } from "../shared/auth-context";
import type { JobMessage, JobsEnv } from "./env";

const PROCESSOR_PATH = "/internal/task-intelligence/recurrence";
const RETRY_SECONDS = 10;
type Receipt = {uid: string; receipt_id: string; account_generation: number};
const current = "account_generation=COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid=r.uid),0) " +
  "AND NOT EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=r.uid) " +
  "AND NOT EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=r.uid)";
const messageFor = (receipt: Receipt): JobMessage => ({uid:receipt.uid,jobId:receipt.receipt_id,kind:"task_recurrence",payload:{}});

export async function processTaskRecurrenceMessage(message: Message<JobMessage>, env: JobsEnv): Promise<void> {
  const {uid, jobId, kind} = message.body;
  if (kind !== "task_recurrence" || typeof uid !== "string" || !uid || uid.length > 256 || typeof jobId !== "string" || !jobId || jobId.length > 128) {
    message.ack(); return;
  }
  const receipt = await env.APP_DB.prepare("SELECT uid,receipt_id,account_generation FROM cf_task_recurrence_inbox r WHERE uid=? AND receipt_id=? AND status='pending' AND " + current)
    .bind(uid,jobId).first<Receipt>();
  if (!receipt) { message.ack(); return; }
  if (!env.API_CORE) throw new Error("recurrence processor unavailable");
  const signed = await createSignedAuthContext({uid,authority:"internal",requestId:jobId},"api-core","POST",PROCESSOR_PATH,env.INTERNAL_ASSERTION_SECRET);
  if (!signed) throw new Error("recurrence assertion unavailable");
  const response = await env.API_CORE.fetch(new Request(`https://api-core.internal${PROCESSOR_PATH}`,{
    method:"POST",headers:{"content-type":"application/json","x-omi-auth-context":signed.encoded,"x-omi-internal-signature":signed.signature},
    body:JSON.stringify({receipt_id:jobId,account_generation:receipt.account_generation}),
  }));
  if (response.ok) {
    const result = await response.json() as {status?:unknown};
    if (result.status === "completed") { message.ack(); return; }
  } else {
    await response.body?.cancel();
    if ([400,404,409].includes(response.status)) { message.ack(); return; }
  }
  message.retry({delaySeconds:RETRY_SECONDS});
}

export async function reconcileTaskRecurrence(env: JobsEnv): Promise<void> {
  const result = await env.APP_DB.prepare("SELECT uid,receipt_id,account_generation FROM cf_task_recurrence_inbox r WHERE status='pending' AND " + current + " ORDER BY updated_at,uid,receipt_id LIMIT 50").all<Receipt>();
  let failed = false;
  for (const receipt of result.results ?? []) {
    try { await env.JOBS.send(messageFor(receipt)); } catch { failed = true; }
  }
  if (failed) throw new Error("recurrence queue unavailable");
}
