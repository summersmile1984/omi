import { Hono } from "hono";
import { verifyRequestAuthContext } from "../shared/auth-context";
import {
  bytes,
  CONTENT_PURPOSE,
  objectKeys,
  validUid,
  verifyApproval,
  verifyToken,
} from "./approval";
import { cleanup, residual, writeApproved, type WriterEnv } from "./storage";

const app = new Hono<{ Bindings: WriterEnv }>();

function signingSecret(env: WriterEnv): string {
  if (
    typeof env.SCREEN_FRAME_SIGNING_SECRET !== "string" ||
    env.SCREEN_FRAME_SIGNING_SECRET.length < 32 ||
    !env.INTERNAL_ASSERTION_SECRET ||
    env.SCREEN_FRAME_SIGNING_SECRET === env.INTERNAL_ASSERTION_SECRET
  )
    throw new Error("isolated screen frame signing unavailable");
  return env.SCREEN_FRAME_SIGNING_SECRET;
}
app.use("*", async (c, next) => {
  await next();
  c.header("cache-control", "no-store");
  c.header("x-content-type-options", "nosniff");
});

async function boundedJson(request: Request): Promise<Record<string, unknown>> {
  const reader = request.body?.getReader();
  if (!reader) throw new Error("missing body");
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 16 * 1024 * 1024) {
        await reader.cancel();
        throw new Error("body too large");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  const value: unknown = JSON.parse(
    new TextDecoder("utf-8", { fatal: true }).decode(body)
  );
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("invalid body");
  return value as Record<string, unknown>;
}

app.get("/health", (c) =>
  c.json({ service: "screen-frame-writer", status: "ok" })
);
app.get("/ready", async (c) => {
  try {
    await c.env.APP_DB.prepare(
      "SELECT 1 FROM cf_screen_frame_writes LIMIT 1"
    ).first();
    await c.env.SCREEN_FRAMES.head("readiness-probe");
    signingSecret(c.env);
    return c.json({ service: "screen-frame-writer", status: "ready" });
  } catch {
    return c.json({ error: "screen_frame_writer_unavailable" }, 503);
  }
});

app.post("/internal/screen-frames/write", async (c) => {
  let p, jpeg, thumbnail;
  try {
    const body = await boundedJson(c.req.raw);
    p = await verifyApproval(
      body.approval,
      signingSecret(c.env),
      Math.floor(Date.now() / 1000)
    );
    jpeg = bytes(body.jpeg_base64, 8 * 1024 * 1024);
    thumbnail = bytes(body.thumbnail_base64, 1024 * 1024);
  } catch {
    return c.json({ error: "invalid_screen_frame_approval" }, 403);
  }
  try {
    await writeApproved(c.env, p, jpeg, thumbnail);
    return c.json({ frame_id: p.jti }, 201);
  } catch {
    return c.json({ error: "screen_frame_write_unavailable" }, 503);
  }
});

app.all("/internal/users/:uid/screen-frames/:action", async (c) => {
  const uid = c.req.param("uid"),
    action = c.req.param("action");
  const identity = await verifyRequestAuthContext(
    c.req.raw,
    "screen-frame-writer",
    c.env.INTERNAL_ASSERTION_SECRET
  );
  if (
    !validUid(uid) ||
    identity?.uid !== uid ||
    identity.authority !== "internal"
  )
    return c.json({ error: "unauthorized" }, 401);
  try {
    if (action === "cleanup" && c.req.method === "POST") {
      await cleanup(c.env, uid);
      return c.json(await residual(c.env, uid));
    }
    if (action === "residual" && c.req.method === "GET")
      return c.json(await residual(c.env, uid));
    return c.json({ error: "not_found" }, 404);
  } catch {
    return c.json({ error: "screen_frame_cleanup_unavailable" }, 503);
  }
});

// This capability is separate from the one-use writer approval. Every read
// checks the live subject, account setting and (for shared URLs) sharing state.
app.get("/v1/screen-frame-content", async (c) => {
  try {
    const p = await verifyToken(
      c.req.query("token"),
      signingSecret(c.env),
      CONTENT_PURPOSE
    );
    if (
      !validUid(p.uid) ||
      typeof p.frame_id !== "string" ||
      !["content", "thumbnail"].includes(String(p.variant)) ||
      !["owner", "shared"].includes(String(p.access)) ||
      !Number.isSafeInteger(p.expires_at) ||
      Number(p.expires_at) <= Date.now() / 1000 ||
      Number(p.expires_at) > Date.now() / 1000 + 3605
    )
      throw new Error("invalid content capability");
    const visibility = c.env.APP_DB.prepare(
      `SELECT 1 FROM cf_screen_frame_writes w
       JOIN cf_screen_frame_sets s ON s.uid = w.uid AND s.conversation_id = w.conversation_id
       JOIN cf_conversations c ON c.uid = w.uid AND c.id = w.conversation_id
       WHERE w.uid = ? AND w.jti = ? AND w.phase = 'committed'
         AND EXISTS (SELECT 1 FROM json_each(s.frames_json) WHERE json_extract(value, '$.id') = w.jti)
         AND COALESCE((SELECT enabled FROM cf_screen_frame_settings WHERE uid = w.uid), 1) = 1
         AND (? = 'owner' OR (s.sharing_enabled = 1 AND c.visibility NOT IN ('private', '')))
         AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = w.uid)
         AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = w.uid)`
    ).bind(p.uid, p.frame_id, p.access);
    if (!(await visibility.first())) return c.body(null, 404);
    const keys = objectKeys(p.uid, p.frame_id);
    const data = await c.env.SCREEN_FRAMES.get(
      keys[p.variant === "thumbnail" ? 1 : 0]
    );
    if (!data || !(await visibility.first())) return c.body(null, 404);
    return new Response(data.body, {
      headers: {
        "content-type": "image/jpeg",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
      },
    });
  } catch {
    return c.body(null, 404);
  }
});

export { app };
export default {
  fetch: app.fetch,
  async scheduled(
    _controller: ScheduledController,
    env: WriterEnv
  ): Promise<void> {
    await cleanup(env);
  },
};
