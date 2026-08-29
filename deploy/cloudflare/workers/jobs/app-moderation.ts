import { Hono, type Context } from "hono";
import type { JobsEnv } from "./env";

const MAX_IDENTIFIER_LENGTH = 256;
const MAX_TESTER_APPS = 100;
const MAX_BODY_BYTES = 16_000;
const MAX_APP_PAYLOAD_BYTES = 500_000;
const MAX_UNAPPROVED_APPS = 500;

type JobsContext = Context<{ Bindings: JobsEnv }>;
type AppRow = {
  id: string;
  owner_uid: string | null;
  data_json: string;
  updated_at: number;
};

function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = new TextEncoder().encode(left);
  const rightBytes = new TextEncoder().encode(right);
  let difference = leftBytes.length ^ rightBytes.length;
  const maximum = Math.max(leftBytes.length, rightBytes.length);
  for (let index = 0; index < maximum; index += 1) {
    difference |= (leftBytes[index] || 0) ^ (rightBytes[index] || 0);
  }
  return difference === 0;
}

function adminAuthorized(c: JobsContext): boolean {
  const expected = c.env.APPS_ADMIN_KEY;
  const provided = c.req.header("secret-key");
  return Boolean(expected && provided && constantTimeEqual(provided, expected));
}

function requireAdmin(c: JobsContext): Response | null {
  return adminAuthorized(c)
    ? null
    : c.json({ detail: "You are not authorized to perform this action" }, 403);
}

function identifier(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized.length > 0 && normalized.length <= MAX_IDENTIFIER_LENGTH
    ? normalized
    : null;
}

async function jsonObject(
  c: JobsContext,
): Promise<Record<string, unknown> | null> {
  const raw = await c.req.text();
  if (!raw || new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES)
    return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function appPayload(
  row: Pick<AppRow, "id" | "data_json">,
): Record<string, unknown> {
  if (
    typeof row.data_json !== "string" ||
    new TextEncoder().encode(row.data_json).byteLength > MAX_APP_PAYLOAD_BYTES
  ) {
    throw new Error("invalid app payload");
  }
  const parsed = JSON.parse(row.data_json) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
    throw new Error("invalid app payload");
  const payload = parsed as Record<string, unknown>;
  if (payload.id != null && payload.id !== row.id)
    throw new Error("invalid app payload");
  return payload;
}

function privateApp(payload: Record<string, unknown>): boolean {
  const value = payload.private;
  return value === true || value === 1 || value === "true" || value === "1";
}

async function addTester(c: JobsContext): Promise<Response> {
  const body = await jsonObject(c);
  const uid = identifier(body?.uid);
  if (!uid) return c.json({ detail: "uid is required" }, 422);
  if (!Array.isArray(body?.apps) || body.apps.length === 0)
    return c.json({ detail: "apps is required" }, 422);
  const apps = [...new Set(body.apps.map(identifier))];
  if (apps.length > MAX_TESTER_APPS || apps.some((appId) => appId === null)) {
    return c.json({ detail: "invalid apps" }, 422);
  }
  const appIds = apps as string[];
  const placeholders = appIds.map(() => "?").join(", ");
  try {
    const existing = await c.env.APP_DB.prepare(
      `SELECT id FROM cf_app_catalog WHERE id IN (${placeholders})`,
    )
      .bind(...appIds)
      .all<{ id: string }>();
    if ((existing.results || []).length !== appIds.length)
      return c.json({ detail: "App not found" }, 404);
    const now = Math.floor(Date.now() / 1000);
    const statements = [
      c.env.APP_DB.prepare(
        "INSERT INTO cf_app_testers (uid, added_at, updated_at) VALUES (?, ?, ?) " +
          "ON CONFLICT(uid) DO UPDATE SET updated_at = excluded.updated_at",
      ).bind(uid, now, now),
      c.env.APP_DB.prepare(
        "DELETE FROM cf_app_tester_access WHERE uid = ?",
      ).bind(uid),
      ...appIds.map((appId) =>
        c.env.APP_DB.prepare(
          "INSERT INTO cf_app_tester_access (uid, app_id, added_at) VALUES (?, ?, ?)",
        ).bind(uid, appId, now),
      ),
    ];
    await c.env.APP_DB.batch(statements);
    return c.json({ status: "ok" });
  } catch {
    return c.json({ error: "app_tester_unavailable" }, 503);
  }
}

async function mutateTesterAccess(
  c: JobsContext,
  operation: "add" | "remove",
): Promise<Response> {
  const body = await jsonObject(c);
  const uid = identifier(body?.uid);
  const appId = identifier(body?.app_id);
  if (!uid) return c.json({ detail: "uid is required" }, 422);
  if (!appId) return c.json({ detail: "app_id is required" }, 422);
  try {
    const [tester, app] = await Promise.all([
      c.env.APP_DB.prepare("SELECT uid FROM cf_app_testers WHERE uid = ?")
        .bind(uid)
        .first<{ uid: string }>(),
      c.env.APP_DB.prepare("SELECT id FROM cf_app_catalog WHERE id = ?")
        .bind(appId)
        .first<{ id: string }>(),
    ]);
    if (!tester) return c.json({ detail: "Tester not found" }, 404);
    if (!app) return c.json({ detail: "App not found" }, 404);
    const now = Math.floor(Date.now() / 1000);
    if (operation === "add") {
      await c.env.APP_DB.prepare(
        "INSERT OR IGNORE INTO cf_app_tester_access (uid, app_id, added_at) VALUES (?, ?, ?)",
      )
        .bind(uid, appId, now)
        .run();
    } else {
      await c.env.APP_DB.prepare(
        "DELETE FROM cf_app_tester_access WHERE uid = ? AND app_id = ?",
      )
        .bind(uid, appId)
        .run();
    }
    await c.env.APP_DB.prepare(
      "UPDATE cf_app_testers SET updated_at = ? WHERE uid = ?",
    )
      .bind(now, uid)
      .run();
    return c.json({ status: "ok" });
  } catch {
    return c.json({ error: "app_tester_unavailable" }, 503);
  }
}

async function unapprovedApps(c: JobsContext): Promise<Response> {
  try {
    const result = await c.env.APP_DB.prepare(
      "SELECT id, owner_uid, approved, status, disabled, is_popular, installs, rating_avg, rating_count, data_json " +
        "FROM cf_app_catalog WHERE approved = 0 ORDER BY updated_at ASC, id ASC LIMIT ?",
    )
      .bind(MAX_UNAPPROVED_APPS)
      .all<Record<string, unknown>>();
    const apps: Record<string, unknown>[] = [];
    for (const rawRow of result.results || []) {
      const row = rawRow as unknown as AppRow;
      const payload = appPayload(row);
      if (privateApp(payload)) continue;
      apps.push({
        ...payload,
        id: row.id,
        uid: row.owner_uid,
        approved: false,
        status: rawRow.status,
        disabled: Boolean(rawRow.disabled),
        is_popular: Boolean(rawRow.is_popular),
        installs: Math.max(0, Number(rawRow.installs) || 0),
        rating_avg:
          rawRow.rating_avg == null ? null : Number(rawRow.rating_avg),
        rating_count: Math.max(0, Number(rawRow.rating_count) || 0),
      });
    }
    return c.json(apps);
  } catch {
    return c.json({ error: "app_catalog_unavailable" }, 503);
  }
}

async function setPopular(c: JobsContext, appId: string): Promise<Response> {
  const normalizedAppId = identifier(appId);
  const raw = c.req.query("value")?.trim().toLowerCase();
  if (!normalizedAppId) return c.json({ detail: "App not found" }, 404);
  if (raw !== "true" && raw !== "false" && raw !== "1" && raw !== "0")
    return c.json({ detail: "invalid value" }, 422);
  try {
    const result = await c.env.APP_DB.prepare(
      "UPDATE cf_app_catalog SET is_popular = ?, updated_at = MAX(updated_at + 1, ?) WHERE id = ?",
    )
      .bind(
        raw === "true" || raw === "1" ? 1 : 0,
        Math.floor(Date.now() / 1000),
        normalizedAppId,
      )
      .run();
    if (result.meta?.changes !== 1)
      return c.json({ detail: "App not found" }, 404);
    return c.json({ status: "ok" });
  } catch {
    return c.json({ error: "app_catalog_unavailable" }, 503);
  }
}

async function moderateApp(
  c: JobsContext,
  appId: string,
  approved: boolean,
): Promise<Response> {
  const normalizedAppId = identifier(appId);
  const requestedUid = identifier(c.req.query("uid"));
  if (!normalizedAppId) return c.json({ detail: "App not found" }, 404);
  if (!requestedUid) return c.json({ detail: "uid is required" }, 422);
  try {
    const row = await c.env.APP_DB.prepare(
      "SELECT id, owner_uid, data_json, updated_at FROM cf_app_catalog WHERE id = ?",
    )
      .bind(normalizedAppId)
      .first<AppRow>();
    if (!row) return c.json({ detail: "App not found" }, 404);
    if (!row.owner_uid || row.owner_uid !== requestedUid)
      return c.json({ detail: "App owner mismatch" }, 409);
    const payload = appPayload(row);
    const name =
      typeof payload.name === "string" && payload.name.trim()
        ? payload.name.trim().slice(0, 200)
        : normalizedAppId;
    const status = approved ? "approved" : "rejected";
    payload.approved = approved;
    payload.status = status;
    const now = Math.max(
      Math.floor(Date.now() / 1000),
      Number(row.updated_at || 0) + 1,
    );
    const notificationId = crypto.randomUUID();
    const sourceId = `${normalizedAppId}:${status}:${now}`;
    const title = approved ? "App Approved 🎉" : "App Rejected 😔";
    const body = approved
      ? `Your app ${name} has been approved and is now available for everyone to use 🥳`
      : `Your app ${name} has been rejected. Please make the necessary changes and resubmit for approval.`;
    const results = await c.env.APP_DB.batch([
      c.env.APP_DB.prepare(
        "UPDATE cf_app_catalog SET approved = ?, status = ?, data_json = ?, updated_at = ? " +
          "WHERE id = ? AND owner_uid = ? AND updated_at = ?",
      ).bind(
        approved ? 1 : 0,
        status,
        JSON.stringify(payload),
        now,
        normalizedAppId,
        requestedUid,
        row.updated_at,
      ),
      c.env.APP_DB.prepare(
        "INSERT INTO cf_notification_outbox " +
          "(notification_id, source_kind, source_id, uid, title, body, data_json, status, attempts, not_before, created_at, updated_at) " +
          "SELECT ?, 'app_moderation', ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ? " +
          "FROM cf_app_catalog WHERE id = ? AND owner_uid = ? AND updated_at = ?",
      ).bind(
        notificationId,
        sourceId,
        requestedUid,
        title,
        body,
        JSON.stringify({
          type: "app_moderation",
          app_id: normalizedAppId,
          status,
        }),
        now,
        now,
        now,
        normalizedAppId,
        requestedUid,
        now,
      ),
    ]);
    if (results[0]?.meta?.changes !== 1 || results[1]?.meta?.changes !== 1)
      return c.json({ error: "app_moderation_conflict" }, 409);
    return c.json({ status: "ok" });
  } catch {
    return c.json({ error: "app_moderation_unavailable" }, 503);
  }
}

export function registerAppModerationRoutes(app: Hono<{ Bindings: JobsEnv }>) {
  app.post("/v1/apps/tester", async (c) => {
    const denied = requireAdmin(c);
    return denied || addTester(c);
  });
  app.post("/v1/apps/tester/access", async (c) => {
    const denied = requireAdmin(c);
    return denied || mutateTesterAccess(c, "add");
  });
  app.delete("/v1/apps/tester/access", async (c) => {
    const denied = requireAdmin(c);
    return denied || mutateTesterAccess(c, "remove");
  });
  app.get("/v1/apps/public/unapproved", async (c) => {
    const denied = requireAdmin(c);
    return denied || unapprovedApps(c);
  });
  app.patch("/v1/apps/:appId/popular", async (c) => {
    const denied = requireAdmin(c);
    return denied || setPopular(c, c.req.param("appId"));
  });
  app.post("/v1/apps/:appId/approve", async (c) => {
    const denied = requireAdmin(c);
    return denied || moderateApp(c, c.req.param("appId"), true);
  });
  app.post("/v1/apps/:appId/reject", async (c) => {
    const denied = requireAdmin(c);
    return denied || moderateApp(c, c.req.param("appId"), false);
  });
}
