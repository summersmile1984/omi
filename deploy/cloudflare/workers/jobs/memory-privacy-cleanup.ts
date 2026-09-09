import { createSignedAuthContext } from "../shared/auth-context";
import type { JobMessage, JobsEnv } from "./env";
import { retractMemoryVectors } from "./memory-vector-publication";

const KIND = "memory_privacy_cleanup";
type Target = { id: string; item_revision: number };

async function core(
  env: JobsEnv,
  uid: string,
  token: string,
  phase: "resume" | "finalize" | "scope",
) {
  const path = "/internal/memory-privacy/" + phase;
  const auth = await createSignedAuthContext(
    { uid, authority: "internal", requestId: crypto.randomUUID() },
    "api-core",
    "POST",
    path,
    env.INTERNAL_ASSERTION_SECRET,
  );
  if (!auth || !env.API_CORE)
    throw new Error("memory privacy authority unavailable");
  return env.API_CORE.fetch(
    new Request("https://api-core.internal" + path, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-omi-auth-context": auth.encoded,
        "x-omi-internal-signature": auth.signature,
      },
      body: JSON.stringify({ token }),
      signal: AbortSignal.timeout(25000),
    }),
  );
}

export async function cleanupMemoryPrivacy(
  env: JobsEnv,
  uid: string,
  token: string,
): Promise<boolean> {
  if (!uid || uid.length > 256 || !/^[0-9a-f]{64}$/.test(token)) return false;
  try {
    await env.APP_DB.prepare(
      "UPDATE cf_memory_privacy_deletions SET last_attempt_at = unixepoch() WHERE uid = ? AND token = ?",
    )
      .bind(uid, token)
      .run();
    // The Core owner renews/rechecks legal-hold authority before provider IO.
    // Queue messages cannot select arbitrary external IDs or memory revisions.
    const resumed = await core(env, uid, token, "resume");
    if (resumed.status === 204) return true;
    if (resumed.status !== 200) return false;
    const body = (await resumed.json()) as { targets?: Target[] };
    const targets = body.targets;
    if (
      !Array.isArray(targets) ||
      !targets.length ||
      targets.length > 100 ||
      targets.some(
        (row) =>
          !row ||
          typeof row.id !== "string" ||
          !row.id ||
          row.id.length > 256 ||
          !Number.isSafeInteger(row.item_revision) ||
          row.item_revision < 1,
      ) ||
      new Set(targets.map((row) => row.id)).size !== targets.length
    )
      return false;
    let ready = true;
    for (const target of targets) {
      if (
        !(await retractMemoryVectors(
          env,
          uid,
          target.id,
          target.item_revision,
          "delete",
        ))
      )
        ready = false;
    }
    if (!ready) return false;
    // The final transaction independently checks D1 artifact/mapping absence;
    // acceptance of a Vectorize delete request cannot substitute for that proof.
    return (await core(env, uid, token, "finalize")).status === 204;
  } catch {
    return false;
  }
}

export async function processMemoryPrivacyMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
) {
  const token = message.body.payload.token;
  if (
    message.body.kind !== KIND ||
    typeof token !== "string" ||
    !/^[0-9a-f]{64}$/.test(token) ||
    !message.body.uid
  ) {
    message.ack();
    return;
  }
  let completed = false;
  if (message.body.payload.scope === true) {
    try {
      completed =
        (await core(env, message.body.uid, token, "scope")).status === 204;
    } catch {
      completed = false;
    }
  } else completed = await cleanupMemoryPrivacy(env, message.body.uid, token);
  if (completed) message.ack();
  else message.retry({ delaySeconds: 10 });
}

export async function reconcileMemoryPrivacyDeletions(env: JobsEnv) {
  const pending = await env.APP_DB.prepare(
    "SELECT uid, token FROM cf_memory_privacy_deletions ORDER BY last_attempt_at, created_at, uid LIMIT 10",
  ).all<{ uid: string; token: string }>();
  for (const row of pending.results ?? [])
    await cleanupMemoryPrivacy(env, row.uid, row.token);
  const scopes = await env.APP_DB.prepare(
    "SELECT uid, token FROM cf_memory_privacy_scopes ORDER BY last_attempt_at, created_at, uid LIMIT 10",
  ).all<{ uid: string; token: string }>();
  for (const row of scopes.results ?? []) {
    try {
      await core(env, row.uid, row.token, "scope");
    } catch {
      /* Durable scope retries on the next invocation. */
    }
  }
}
