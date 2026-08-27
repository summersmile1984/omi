import { Hono } from "hono";
import { decodeAuthContext, verifyAuthContextSignature } from "../shared/auth-context";
import type { RealtimeEnv } from "./env";
import { RealtimeSession } from "./session";

const app = new Hono<{ Bindings: RealtimeEnv }>();

app.get("/health", (c) => c.json({ status: "ok", service: "realtime", version: "cf-08" }));

app.all("/v2/voice-message/transcribe-stream", (c) => routeSession(c.req.raw, c.env));
app.all("/v4/listen", (c) => routeSession(c.req.raw, c.env));
app.all("/v4/web/listen", (c) => routeSession(c.req.raw, c.env));
app.all("/v1/omni/relay", (c) => routeSession(c.req.raw, c.env));

async function routeSession(request: Request, env: RealtimeEnv): Promise<Response> {
  const encodedContext = request.headers.get("x-omi-auth-context");
  const context = decodeAuthContext(encodedContext);
  const signatureValid = await verifyAuthContextSignature(
    encodedContext || "",
    request.headers.get("x-omi-internal-signature"),
    env.INTERNAL_ASSERTION_SECRET,
  );
  if (!context || !signatureValid) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  if (request.headers.get("upgrade")?.toLowerCase() !== "websocket") {
    return Response.json({ error: "websocket upgrade required" }, { status: 426 });
  }
  const requestedSession = request.headers.get("x-omi-session-id") || "default";
  const id = env.REALTIME_SESSIONS.idFromName(`${context.uid}:${requestedSession}`);
  const stub = env.REALTIME_SESSIONS.get(id);
  return stub.fetch(request);
}

export { RealtimeSession };
export default app;
