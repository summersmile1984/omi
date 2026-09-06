import { Hono, type Context } from "hono";
import {
  createSignedAuthContext,
  type AuthContext,
} from "../shared/auth-context";
import type { JobsEnv } from "./env";
import { recordFallback } from "../../../../runtime/shared/fallback.mjs";

export type ShareEmailBinding = {
  send(message: {
    from: { email: string; name: string };
    to: string[];
    subject: string;
    html: string;
    replyTo?: string;
  }): Promise<{ messageId: string }>;
};

const noStore = { "cache-control": "private, no-store" };
function unknownDelivery() {
  recordFallback({
    component: "other",
    from: "cloudflare_email",
    to: "conservative_no_action",
    reason: "dependency_unavailable",
    outcome: "degraded",
  });
}
const definitiveErrors = new Set([
  "E_VALIDATION_ERROR",
  "E_FIELD_MISSING",
  "E_TOO_MANY_RECIPIENTS",
  "E_SENDER_NOT_VERIFIED",
  "E_RECIPIENT_NOT_ALLOWED",
  "E_RECIPIENT_SUPPRESSED",
  "E_SENDER_DOMAIN_NOT_AVAILABLE",
  "E_CONTENT_TOO_LARGE",
  "E_RATE_LIMIT_EXCEEDED",
  "E_DAILY_LIMIT_EXCEEDED",
  "E_HEADER_NOT_ALLOWED",
  "E_HEADER_USE_API_FIELD",
  "E_HEADER_VALUE_INVALID",
  "E_HEADER_VALUE_TOO_LONG",
  "E_HEADER_NAME_INVALID",
  "E_HEADERS_TOO_LARGE",
  "E_HEADERS_TOO_MANY",
]);

async function core(env: JobsEnv, uid: string, path: string, body?: string) {
  const signed = await createSignedAuthContext(
    { uid, authority: "internal", requestId: crypto.randomUUID() },
    "api-core",
    "POST",
    path,
    env.INTERNAL_ASSERTION_SECRET
  );
  if (!signed || !env.API_CORE) throw new Error("mail authority unavailable");
  return env.API_CORE.fetch(
    new Request("https://api-core.internal" + path, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-omi-auth-context": signed.encoded,
        "x-omi-internal-signature": signed.signature,
      },
      ...(body ? { body } : {}),
      signal: AbortSignal.timeout(25000),
    })
  );
}

function publicResponse(response: Response) {
  return new Response(response.body, {
    status: response.status,
    headers: {
      ...noStore,
      "content-type": "application/json",
    },
  });
}

export function registerShareEmailRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: (
    c: Context<{ Bindings: JobsEnv }>
  ) => Promise<AuthContext | null>
) {
  app.post("/v1/conversations/:conversationId/share-email", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ detail: "unauthorized" }, 401, noStore);
    const sender = c.env.SHARE_EMAIL_FROM_ADDRESS;
    if (
      !c.env.SHARE_EMAIL ||
      !sender ||
      !/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(sender)
    )
      return c.json(
        { detail: "email sending is not configured" },
        503,
        noStore
      );
    let dispatched = false;
    try {
      const prefix = "/internal/share-email/";
      const prepared = await core(
        c.env,
        context.uid,
        prefix +
          "conversations/" +
          encodeURIComponent(c.req.param("conversationId")) +
          "/prepare",
        await c.req.text()
      );
      if (!prepared.ok) return publicResponse(prepared);
      const result = (await prepared.json()) as {
        dispatch_id?: string;
        already_sent?: string[];
        sent_to?: string[];
      };
      if (Array.isArray(result.sent_to))
        return c.json({ sent_to: result.sent_to }, 200, noStore);
      if (
        typeof result.dispatch_id !== "string" ||
        !Array.isArray(result.already_sent)
      )
        throw new Error("invalid mail preparation");
      const path =
        prefix + "dispatches/" + encodeURIComponent(result.dispatch_id);
      const claim = await core(c.env, context.uid, path + "/claim");
      if (!claim.ok) return publicResponse(claim);
      const payload = (await claim.json()) as {
        to: string[];
        subject: string;
        html: string;
        sender_name: string;
        brand_name: string;
        reply_to: string | null;
      };
      let phase: "sent" | "ambiguous" | "rejected" = "ambiguous";
      let messageId: string | undefined;
      let timeout: ReturnType<typeof setTimeout> | undefined;
      try {
        if (
          !Array.isArray(payload.to) ||
          !payload.to.length ||
          payload.to.length > 5 ||
          !payload.to.every((email) => typeof email === "string") ||
          ![
            payload.subject,
            payload.html,
            payload.sender_name,
            payload.brand_name,
          ].every((value) => typeof value === "string")
        ) {
          phase = "rejected";
        } else {
          // Marked dispatching in D1 before crossing the provider boundary.
          // A timeout cannot authorize a second send, including after a restart.
          dispatched = true;
          const response = await Promise.race([
            c.env.SHARE_EMAIL.send({
              from: {
                email: sender,
                name: `${payload.sender_name} via ${payload.brand_name}`,
              },
              to: payload.to,
              subject: payload.subject,
              html: payload.html,
              ...(payload.reply_to ? { replyTo: payload.reply_to } : {}),
            }),
            new Promise<never>((_, reject) => {
              timeout = setTimeout(
                () => reject(new Error("mail outcome unknown")),
                20000
              );
            }),
          ]);
          if (
            typeof response?.messageId !== "string" ||
            !response.messageId ||
            response.messageId.length > 512
          )
            throw new Error("mail outcome unknown");
          messageId = response.messageId;
          phase = "sent";
        }
      } catch (error) {
        const code = (error as { code?: unknown })?.code;
        if (typeof code === "string" && definitiveErrors.has(code))
          phase = "rejected";
      } finally {
        if (timeout !== undefined) clearTimeout(timeout);
      }
      const finalized = await core(
        c.env,
        context.uid,
        path + "/finish",
        JSON.stringify({ phase, message_id: messageId })
      );
      if (!finalized.ok) {
        await finalized.arrayBuffer();
        throw new Error("mail result persistence unavailable");
      }
      await finalized.arrayBuffer();
      if (phase === "ambiguous") {
        unknownDelivery();
        return c.json(
          { detail: "email delivery status unknown" },
          504,
          noStore
        );
      }
      if (phase === "rejected")
        return c.json(
          { detail: "email provider rejected the send" },
          502,
          noStore
        );
      return c.json(
        {
          sent_to: [...new Set([...result.already_sent, ...payload.to])].sort(),
        },
        200,
        noStore
      );
    } catch {
      if (dispatched) unknownDelivery();
      return c.json(
        {
          detail: dispatched
            ? "email delivery status unknown"
            : "share email temporarily unavailable",
        },
        dispatched ? 504 : 503,
        noStore
      );
    }
  });
}

export async function expireShareEmailDispatches(env: JobsEnv, now: number) {
  // One bounded sweep retires interrupted attempts; no sweep sends an email.
  const results = await env.APP_DB.batch([
    env.APP_DB.prepare(
      "UPDATE cf_share_email_dispatches SET phase='rejected',payload_json=NULL WHERE id IN " +
        "(SELECT id FROM cf_share_email_dispatches WHERE phase='prepared' AND expires_at<=? " +
        "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=cf_share_email_dispatches.uid) " +
        "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=cf_share_email_dispatches.uid) " +
        "ORDER BY expires_at LIMIT 100)"
    ).bind(now),
    env.APP_DB.prepare(
      "UPDATE cf_share_email_dispatches SET phase='ambiguous',payload_json=NULL WHERE id IN " +
        "(SELECT id FROM cf_share_email_dispatches WHERE phase='dispatching' AND expires_at<=? " +
        "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=cf_share_email_dispatches.uid) " +
        "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=cf_share_email_dispatches.uid) " +
        "ORDER BY expires_at LIMIT 100)"
    ).bind(now),
  ]);
  if (Number(results[1]?.meta?.changes) > 0) unknownDelivery();
}
