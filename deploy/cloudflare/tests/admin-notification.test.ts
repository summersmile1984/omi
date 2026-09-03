import { describe, expect, it } from "vitest";
import { Hono } from "hono";
import { registerAdminNotificationRoutes } from "../workers/jobs/admin-notification";
import type { JobsEnv } from "../workers/jobs/env";

function testApp() {
  const inserts: unknown[][] = [];
  const env = {
    ADMIN_KEY: "admin-secret",
    APP_DB: {
      prepare(sql: string) {
        return {
          bind(...args: unknown[]) {
            return {
              async run() {
                if (!sql.includes("INSERT INTO cf_notification_outbox")) {
                  throw new Error(`unexpected SQL: ${sql}`);
                }
                inserts.push(args);
                return { success: true, meta: { changes: 1 } };
              },
            };
          },
        };
      },
    },
  } as unknown as JobsEnv;
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerAdminNotificationRoutes(app);
  return { app, env, inserts };
}

describe("team notification outbox route", () => {
  it("fails closed when the team secret is absent or incorrect", async () => {
    const { app, env } = testApp();
    const missing = await app.fetch(
      new Request("https://jobs.test/v1/notification", {
        method: "POST",
        body: JSON.stringify({ uid: "user-1", title: "Title", body: "Body" }),
      }),
      env,
    );
    expect(missing.status).toBe(403);

    const wrong = await app.fetch(
      new Request("https://jobs.test/v1/notification", {
        method: "POST",
        headers: { "secret-key": "wrong" },
        body: JSON.stringify({ uid: "user-1", title: "Title", body: "Body" }),
      }),
      env,
    );
    expect(wrong.status).toBe(403);
  });

  it("validates the bounded payload and writes one durable outbox row", async () => {
    const { app, env, inserts } = testApp();
    const invalid = await app.fetch(
      new Request("https://jobs.test/v1/notification", {
        method: "POST",
        headers: { "secret-key": "admin-secret" },
        body: JSON.stringify({
          uid: "user-1",
          title: "Title",
          body: "Body",
          data: [],
        }),
      }),
      env,
    );
    expect(invalid.status).toBe(400);

    const response = await app.fetch(
      new Request("https://jobs.test/v1/notification", {
        method: "POST",
        headers: { "secret-key": "admin-secret" },
        body: JSON.stringify({
          uid: "user-1",
          title: "Title",
          body: "Body",
          data: { kind: "manual", count: 2 },
        }),
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "Ok" });
    expect(inserts).toHaveLength(1);
    expect(inserts[0]?.[1]).toMatch(/^admin:[0-9a-f-]{36}$/);
    expect(inserts[0]?.slice(2, 6)).toEqual([
      "user-1",
      "Title",
      "Body",
      '{"kind":"manual","count":2}',
    ]);
  });
});
