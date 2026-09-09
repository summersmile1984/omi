/** Canonical Core owns leases/receipts; Jobs owns credentialed external I/O. */
import type { Message } from "@cloudflare/workers-types";
import { createSignedAuthContext } from "../shared/auth-context";
import type { JobMessage, JobsEnv } from "./env";
import {
  candidateIntegrationTarget,
  createProviderTask,
  type TaskIntegrationDependencies,
} from "./task-integrations";
import { sendAppleRemindersSync } from "./firebase-messaging";

const PATH = "/internal/candidates/integrations";
type CoreResult = {
  status?: string;
  scheduled?: boolean;
  task?: {
    id: string;
    description: string;
    due_at: string | null;
    exported: boolean;
  };
  push?: { tag: string; data: Record<string, string> };
};
type Identity = { uid: string; outbox_id: string; account_generation: number };

async function core(
  env: JobsEnv,
  identity: Identity,
  body: Record<string, unknown>,
): Promise<CoreResult> {
  if (!env.API_CORE) throw new Error("integration processor unavailable");
  const signed = await createSignedAuthContext(
    { uid: identity.uid, authority: "internal", requestId: identity.outbox_id },
    "api-core",
    "POST",
    PATH,
    env.INTERNAL_ASSERTION_SECRET,
  );
  if (!signed) throw new Error("integration assertion unavailable");
  const response = await env.API_CORE.fetch(
    new Request(`https://api-core.internal${PATH}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-omi-auth-context": signed.encoded,
        "x-omi-internal-signature": signed.signature,
      },
      body: JSON.stringify({
        outbox_id: identity.outbox_id,
        account_generation: identity.account_generation,
        ...body,
      }),
    }),
  );
  if (response.status === 409) {
    await response.body?.cancel();
    return { status: "obsolete" };
  }
  if (!response.ok) {
    await response.body?.cancel();
    throw new Error("integration processor unavailable");
  }
  return (await response.json()) as CoreResult;
}

export async function processCandidateIntegrationMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
  dependencies?: TaskIntegrationDependencies,
): Promise<void> {
  const { uid, jobId, kind, payload } = message.body;
  const generation = payload?.account_generation,
    token = payload?.lease_token;
  if (
    kind !== "candidate_integration" ||
    typeof uid !== "string" ||
    !uid ||
    uid.length > 256 ||
    typeof jobId !== "string" ||
    !jobId ||
    jobId.length > 128 ||
    typeof generation !== "number" ||
    !Number.isSafeInteger(generation) ||
    generation < 0 ||
    typeof token !== "string" ||
    !/^[a-f0-9]{32}$/.test(token)
  ) {
    message.ack();
    return;
  }
  const identity = { uid, outbox_id: jobId, account_generation: generation };
  const platform = await candidateIntegrationTarget(env, uid);
  const lease = { lease_token: token, platform };
  const prepared = await core(env, identity, { action: "prepare", ...lease });
  if (prepared.status === "obsolete") {
    message.ack();
    return;
  }
  if (
    prepared.status !== "task_missing" &&
    (prepared.status !== "ready" || !prepared.task)
  )
    throw new Error("invalid integration preparation");
  let succeeded = false,
    externalId: string | null = null;
  if (prepared.status === "ready") {
    try {
      if (!platform) succeeded = true;
      else if (platform === "apple_reminders") {
        if (!prepared.push) throw new Error("missing reminder push");
        succeeded = await sendAppleRemindersSync(env, uid, prepared.push);
      } else if (prepared.task!.exported) succeeded = true;
      else {
        const result = await createProviderTask(
          env,
          uid,
          platform,
          {
            title: prepared.task!.description,
            description: null,
            dueTimestamp: prepared.task!.due_at
              ? Date.parse(prepared.task!.due_at)
              : null,
          },
          dependencies,
        );
        succeeded = result.success;
        if ("external_task_id" in result)
          externalId = result.external_task_id ?? null;
      }
    } catch {
      succeeded = false;
    }
  }
  const settled = await core(env, identity, {
    action: "settle",
    ...lease,
    succeeded,
    external_id: externalId,
  });
  if (
    !["completed", "failed", "dead_letter", "obsolete"].includes(
      settled.status ?? "",
    )
  )
    throw new Error("invalid integration settlement");
  // A durable failed receipt is retried by the original application backoff,
  // independently of the Queue delivery retry/DLQ budget.
  message.ack();
}

export async function reconcileCandidateIntegrations(
  env: JobsEnv,
  now = Date.now(),
): Promise<void> {
  const result = await env.APP_DB.prepare(
    "SELECT uid,outbox_id,account_generation FROM cf_candidate_integration_outbox r WHERE status IN ('pending','failed','processing') " +
      "AND account_generation=COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid=r.uid),0) " +
      "AND NOT EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=r.uid) AND NOT EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=r.uid) " +
      "AND (julianday(json_extract(record_json,'$.available_at')) IS NULL OR julianday(json_extract(record_json,'$.available_at'))<=julianday(?)) " +
      "AND (status!='processing' OR julianday(json_extract(record_json,'$.claimed_at')) IS NULL OR julianday(json_extract(record_json,'$.claimed_at'))<=julianday(?)) " +
      "ORDER BY json_extract(record_json,'$.updated_at'),uid,outbox_id LIMIT 50",
  )
    .bind(new Date(now).toISOString(), new Date(now - 300_000).toISOString())
    .all<Identity>();
  let failed = false;
  for (const identity of result.results ?? []) {
    try {
      await core(env, identity, { action: "schedule" });
    } catch {
      failed = true;
    }
  }
  if (failed) throw new Error("integration scheduling unavailable");
}
