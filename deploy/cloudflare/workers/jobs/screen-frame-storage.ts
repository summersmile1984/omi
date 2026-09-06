import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../shared/auth-context";
import type { JobsEnv } from "./env";

/** The deletion owner can request erasure, but never receives a bucket binding. */
export async function screenFrameStorageState(
  env: Pick<JobsEnv, "SCREEN_FRAME_WRITER" | "INTERNAL_ASSERTION_SECRET">,
  uid: string,
  action: "cleanup" | "residual"
): Promise<{ empty: boolean; writes: number; objects_present: boolean }> {
  if (!env.SCREEN_FRAME_WRITER)
    throw new Error("screen frame storage unavailable");
  const method = action === "cleanup" ? "POST" : "GET";
  const path = `/internal/users/${encodeURIComponent(
    uid
  )}/screen-frames/${action}`;
  const signed = await createSignedAuthContext(
    { uid, authority: "internal", requestId: crypto.randomUUID() },
    "screen-frame-writer",
    method,
    path,
    env.INTERNAL_ASSERTION_SECRET
  );
  if (!signed) throw new Error("screen frame storage assertion unavailable");
  const response = await env.SCREEN_FRAME_WRITER.fetch(
    new Request(`https://screen-frame-writer.internal${path}`, {
      method,
      headers: {
        [AUTH_CONTEXT_HEADER]: signed.encoded,
        [AUTH_SIGNATURE_HEADER]: signed.signature,
      },
      signal: AbortSignal.timeout(15000),
    })
  );
  if (!response.ok) throw new Error("screen frame storage unavailable");
  const body = await response.text();
  if (body.length > 4096) throw new Error("invalid screen frame residual");
  const value = JSON.parse(body);
  if (
    !value ||
    value.uid !== uid ||
    !Number.isSafeInteger(value.writes) ||
    value.writes < 0 ||
    typeof value.objects_present !== "boolean" ||
    value.empty !== (value.writes === 0 && !value.objects_present)
  )
    throw new Error("invalid screen frame residual");
  return {
    empty: value.empty,
    writes: value.writes,
    objects_present: value.objects_present,
  };
}
