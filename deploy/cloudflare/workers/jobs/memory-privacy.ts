import type { JobsEnv } from "./env";

/** Account erasure uses the shared residual inventory; normal expiry runs here. */
export async function cleanupExpiredMemoryPrivacyReceipts(
  env: JobsEnv,
  now: number,
) {
  await env.APP_DB.prepare(
    "DELETE FROM cf_memory_privacy_receipts WHERE expires_at <= ?",
  )
    .bind(now)
    .run();
}
