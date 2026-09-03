import { Hono, type Context } from "hono";
import type { JobsEnv } from "./env";

const MAX_BODY_BYTES = 64_000;
const MAX_UID_LENGTH = 256;
const MAX_TITLE_LENGTH = 200;
const MAX_MESSAGE_LENGTH = 2_000;
const MAX_DATA_BYTES = 16_000;

type JobsContext = Context<{ Bindings: JobsEnv }>;

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
  const expected = c.env.ADMIN_KEY;
  const provided = c.req.header("secret-key");
  return Boolean(expected && provided && constantTimeEqual(provided, expected));
}

function boundedText(value: unknown, maximum: number): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized.length > 0 && normalized.length <= maximum
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
    const parsed: unknown = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function notificationData(value: unknown): string | null {
  if (value === undefined) return "{}";
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const encoded = JSON.stringify(value);
  if (!encoded || new TextEncoder().encode(encoded).byteLength > MAX_DATA_BYTES)
    return null;
  return encoded;
}

/** Register the legacy team notification contract on the durable FCM outbox owner. */
export function registerAdminNotificationRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
): void {
  app.post("/v1/notification", async (c) => {
    if (!adminAuthorized(c)) {
      return c.json(
        { detail: "You are not authorized to perform this action" },
        403,
      );
    }
    const payload = await jsonObject(c);
    const uid = boundedText(payload?.uid, MAX_UID_LENGTH);
    const title = boundedText(payload?.title, MAX_TITLE_LENGTH);
    const body = boundedText(payload?.body, MAX_MESSAGE_LENGTH);
    const data = notificationData(payload?.data);
    if (!uid || !title || !body || data === null) {
      return c.json({ detail: "Invalid notification" }, 400);
    }

    const now = Math.floor(Date.now() / 1_000);
    const notificationId = crypto.randomUUID();
    try {
      await c.env.APP_DB.prepare(
        "INSERT INTO cf_notification_outbox " +
          "(notification_id, source_kind, source_id, uid, title, body, data_json, status, attempts, " +
          "not_before, created_at, updated_at) VALUES (?, 'integration', ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
      )
        .bind(
          notificationId,
          `admin:${notificationId}`,
          uid,
          title,
          body,
          data,
          now,
          now,
          now,
        )
        .run();
    } catch {
      return c.json({ detail: "Notification unavailable" }, 503);
    }
    return c.json({ status: "Ok" });
  });
}
