import { Hono, type Context } from "hono";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../shared/auth-context";
import type { JobsEnv } from "./env";

const publicPrefix = "/v1/admin/feedback";
const privatePrefix = "/internal/feedback";
const noStore = {
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
};

async function digest(value: string): Promise<Uint8Array> {
  return new Uint8Array(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value))
  );
}

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
    ""
  );
}

async function forward(
  env: JobsEnv,
  actor: string,
  method: string,
  path: string,
  search = ""
): Promise<Response> {
  const assertion = await createSignedAuthContext(
    { uid: actor, authority: "internal", requestId: crypto.randomUUID() },
    "api-core",
    method,
    path,
    env.INTERNAL_ASSERTION_SECRET
  );
  if (!assertion || !env.API_CORE)
    throw new Error("Feedback service unavailable");
  // Caller cookies, user bearer tokens, admin secrets and assertions never pass.
  const response = await env.API_CORE.fetch(
    new Request(`https://api-core.internal${path}${search}`, {
      method,
      headers: {
        [AUTH_CONTEXT_HEADER]: assertion.encoded,
        [AUTH_SIGNATURE_HEADER]: assertion.signature,
      },
    })
  );
  return new Response(response.body, {
    status: response.status,
    headers: { ...noStore, "content-type": "application/json" },
  });
}

async function adminRequest(
  c: Context<{ Bindings: JobsEnv }>
): Promise<Response> {
  const provided = c.req.header("X-Admin-Key");
  if (provided === undefined) {
    return c.json(
      {
        detail: [
          {
            type: "missing",
            loc: ["header", "X-Admin-Key"],
            msg: "Field required",
          },
        ],
      },
      422,
      noStore
    );
  }
  const expected = c.env.ADMIN_KEY;
  if (!expected || provided.length > 4096)
    return c.json({ detail: "Invalid admin key" }, 403, noStore);
  const [actualHash, expectedHash] = await Promise.all([
    digest(provided),
    digest(expected),
  ]);
  let difference = 0;
  for (let index = 0; index < actualHash.length; index++)
    difference |= actualHash[index]! ^ expectedHash[index]!;
  if (difference !== 0)
    return c.json({ detail: "Invalid admin key" }, 403, noStore);
  const user = c.req.header("X-Admin-User");
  const actor = `feedback-admin:${hex(expectedHash).slice(0, 8)}/${
    user ? hex(await digest(user.slice(0, 64))).slice(0, 16) : "unattributed"
  }`;
  const url = new URL(c.req.url);
  const path = privatePrefix + url.pathname.slice(publicPrefix.length);
  // A stable actor/resource pseudonym makes authorized reads attributable
  // without logging the key, administrator uid, request URL or conversation text.
  console.info(
    JSON.stringify({
      event: "feedback_admin",
      actor,
      method: c.req.method,
      resource: hex(await digest(path + url.search)).slice(0, 16),
    })
  );
  try {
    return await forward(c.env, actor, c.req.method, path, url.search);
  } catch {
    return c.json({ detail: "Feedback reports unavailable" }, 503, noStore);
  }
}

export function registerFeedbackReportRoutes(
  app: Hono<{ Bindings: JobsEnv }>
): void {
  app.get(`${publicPrefix}/reports`, adminRequest);
  app.get(`${publicPrefix}/reports/:report_date`, adminRequest);
  app.get(`${publicPrefix}/events/:event_id/context`, adminRequest);
  app.post(`${publicPrefix}/reports/:report_date/generate`, adminRequest);
  app.post(`${publicPrefix}/reports/generate-yesterday`, adminRequest);
}

export async function ensureDailyFeedbackReport(
  env: JobsEnv,
  now: number
): Promise<void> {
  const utc = new Date(now * 1000);
  if (utc.getUTCHours() * 60 + utc.getUTCMinutes() < 90) return;
  const day = new Date((now - 86400) * 1000).toISOString().slice(0, 10);
  const existing = await env.APP_DB.prepare(
    "SELECT 1 AS ready FROM cf_feedback_reports WHERE date = ? AND metadata_json IS NOT NULL"
  )
    .bind(day)
    .first();
  if (existing) return;
  const response = await forward(
    env,
    "feedback-scheduler",
    "POST",
    `${privatePrefix}/ensure-yesterday`
  );
  await response.arrayBuffer();
  // An active, leased generator owns this day; the ordinary five-minute lane
  // observes completion or retries after lease expiry on its next invocation.
  if (!response.ok && response.status !== 409)
    throw new Error("Daily feedback report unavailable");
}
