import { Hono } from "hono";
import { verifyRequestAuthContext } from "../shared/auth-context";
import {
  REALTIME_BOOTSTRAP_HEADER,
  REALTIME_BOOTSTRAP_SIGNATURE_HEADER,
  verifyRealtimeBootstrap,
} from "../shared/realtime-bootstrap";
import type { RealtimeEnv } from "./env";
import { RealtimeSession } from "./session";

const app = new Hono<{ Bindings: RealtimeEnv }>();

app.get("/health", (c) =>
  c.json({ status: "ok", service: "realtime", version: "cf-09" }),
);

app.all("/v2/voice-message/transcribe-stream", (c) =>
  routeSession(c.req.raw, c.env),
);
app.all("/v4/listen", (c) => routeSession(c.req.raw, c.env));
app.all("/v4/web/listen", (c) => routeSession(c.req.raw, c.env));
app.all("/v1/omni/relay", (c) => routeSession(c.req.raw, c.env));

async function routeSession(
  request: Request,
  env: RealtimeEnv,
): Promise<Response> {
  if (request.headers.get("upgrade")?.toLowerCase() !== "websocket") {
    return Response.json(
      { error: "websocket upgrade required" },
      { status: 426 },
    );
  }
  if (new URL(request.url).pathname === "/v4/web/listen") {
    const bootstrap = await verifyRealtimeBootstrap(
      request.headers.get(REALTIME_BOOTSTRAP_HEADER),
      request.headers.get(REALTIME_BOOTSTRAP_SIGNATURE_HEADER),
      env.INTERNAL_ASSERTION_SECRET,
    );
    if (!bootstrap) {
      return Response.json({ error: "unauthorized" }, { status: 401 });
    }
    const id = env.REALTIME_SESSIONS.idFromName(`web:${bootstrap.sessionId}`);
    return env.REALTIME_SESSIONS.get(id).fetch(request);
  }
  const context = await verifyRequestAuthContext(
    request,
    "realtime",
    env.INTERNAL_ASSERTION_SECRET,
  );
  if (!context) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  const requestedSession = request.headers.get("x-omi-session-id") || "default";
  const id = env.REALTIME_SESSIONS.idFromName(
    `${context.uid}:${requestedSession}`,
  );
  const stub = env.REALTIME_SESSIONS.get(id);
  return stub.fetch(request);
}

export { RealtimeSession };
export default app;
