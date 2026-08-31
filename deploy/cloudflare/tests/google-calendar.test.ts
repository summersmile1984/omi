import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SignedAuthContext } from "../workers/shared/auth-context";
import type { JobsEnv } from "../workers/jobs/env";
import {
  cleanupExpiredGoogleCalendarOAuthStates,
  registerGoogleCalendarRoutes,
  type GoogleCalendarDependencies,
} from "../workers/jobs/google-calendar";

type BoundStatement = {
  sql: string;
  args: unknown[];
  execute(): { success: true; results: unknown[]; meta: { changes: number } };
};

function sqliteValue(value: unknown) {
  return value as never;
}

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    this.database.exec("PRAGMA foreign_keys = ON");
    const directory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/app",
    );
    for (const filename of readdirSync(directory)
      .filter((value) => value.endsWith(".sql"))
      .sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string) {
    const build = (args: unknown[] = []) => ({
      sql,
      args,
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...args.map(sqliteValue)) as
          T | undefined) ?? null,
      all: async <T>() => ({
        success: true as const,
        results: this.database
          .prepare(sql)
          .all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0 },
      }),
      run: async () => build(args).execute(),
      execute: () => {
        const statement = this.database.prepare(sql);
        if (/^(SELECT|DELETE\b.*RETURNING\b)/is.test(sql.trimStart())) {
          return {
            success: true as const,
            results: statement.all(...args.map(sqliteValue)),
            meta: { changes: 0 },
          };
        }
        const result = statement.run(...args.map(sqliteValue));
        return {
          success: true as const,
          results: [],
          meta: { changes: Number(result.changes) },
        };
      },
    });
    return build();
  }

  async batch(statements: BoundStatement[]) {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const results = statements.map((statement) => statement.execute());
      this.database.exec("COMMIT");
      return results;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  close() {
    this.database.close();
  }
}

const databases: SqliteD1[] = [];

function environment(configured = true, sharedConfigured = false) {
  const database = new SqliteD1();
  databases.push(database);
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fetchImpl = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      calls.push({ url, init });
      if (url === "https://oauth2.googleapis.com/token") {
        const body = new URLSearchParams(String(init?.body));
        if (body.get("grant_type") === "refresh_token") {
          return Response.json({
            access_token: "calendar-access-refreshed",
            expires_in: 3_600,
            scope: "https://www.googleapis.com/auth/calendar",
          });
        }
        return Response.json({
          access_token: "calendar-access-initial",
          refresh_token: "calendar-refresh",
          expires_in: 100,
          scope: "https://www.googleapis.com/auth/calendar",
        });
      }
      if (
        url.startsWith(
          "https://www.googleapis.com/calendar/v3/calendars/primary/events?",
        )
      ) {
        return Response.json({
          items: [
            {
              id: "timed-event",
              summary: "Workers review",
              start: { dateTime: "2026-09-01T09:00:00+08:00" },
              end: { dateTime: "2026-09-01T10:00:00+08:00" },
              attendees: [
                { email: "owner@example.test", self: true },
                { email: "guest@example.test", displayName: "Guest" },
              ],
              htmlLink: "https://calendar.google.com/event?eid=timed",
            },
            {
              id: "all-day-event",
              start: { date: "2026-09-02" },
              end: { date: "2026-09-03" },
            },
            { id: "invalid-event", start: {}, end: {} },
          ],
        });
      }
      if (
        url ===
        "https://www.googleapis.com/calendar/v3/calendars/primary/events/link-event?fields=id%2Csummary%2Cstart%2Cend%2Cattendees%28email%2CdisplayName%2Cself%29%2ChtmlLink%2Cdescription"
      ) {
        return Response.json({
          id: "link-event",
          summary: "Linked event",
          start: { dateTime: "2026-09-01T09:00:00+08:00" },
          end: { dateTime: "2026-09-01T10:00:00+08:00" },
          attendees: [{ email: "guest@example.test", displayName: "Guest" }],
          htmlLink: "https://calendar.google.com/event?eid=link",
          description: "Existing details",
        });
      }
      if (
        url ===
        "https://www.googleapis.com/calendar/v3/calendars/primary/events"
      ) {
        if (init?.method !== "POST") {
          return Response.json({ id: "calendar-event" });
        }
        return Response.json({
          id: "created-event",
          summary: "Created event",
          htmlLink: "https://calendar.google.com/event?eid=created",
        });
      }
      if (
        init?.method === "PATCH" &&
        url.startsWith(
          "https://www.googleapis.com/calendar/v3/calendars/primary/events/",
        )
      ) {
        return Response.json({ id: url.split("/").at(-1) });
      }
      if (
        url ===
        "https://www.googleapis.com/calendar/v3/calendars/primary/events/link-event"
      ) {
        return Response.json({ id: "link-event" });
      }
      throw new Error(`unexpected Google URL ${url}`);
    },
  );
  const env = {
    APP_DB: database as unknown as D1Database,
    PUBLIC_API_BASE_URL: "https://edge.example.test",
    GOOGLE_CALENDAR_TOKEN_ENCRYPTION_SECRET: "c".repeat(64),
    ...(configured
      ? {
          GOOGLE_CALENDAR_CLIENT_ID: "calendar-client-id",
          GOOGLE_CALENDAR_CLIENT_SECRET: "calendar-client-secret",
        }
      : {}),
    ...(sharedConfigured
      ? {
          GOOGLE_CLIENT_ID: "shared-google-client-id",
          GOOGLE_CLIENT_SECRET: "shared-google-client-secret",
        }
      : {}),
  } as unknown as JobsEnv;
  return { database, env, calls, fetchImpl };
}

function seedConversation(
  database: SqliteD1,
  conversationId: string,
  values: {
    createdAt?: number;
    startedAt?: number | null;
    finishedAt?: number | null;
    locked?: boolean;
  } = {},
) {
  database.database
    .prepare(
      "INSERT INTO cf_conversations (uid, id, created_at, updated_at, started_at, finished_at, is_locked) VALUES (?, ?, ?, ?, ?, ?, ?)",
    )
    .run(
      "calendar-user",
      conversationId,
      values.createdAt ?? 1_000,
      values.createdAt ?? 1_000,
      values.startedAt ?? null,
      values.finishedAt ?? null,
      values.locked ? 1 : 0,
    );
}

function testApp(env: JobsEnv, dependencies: GoogleCalendarDependencies) {
  const app = new Hono<{ Bindings: JobsEnv }>();
  const requestContext = async (c: {
    req: { header(name: string): string | undefined };
  }): Promise<SignedAuthContext | null> =>
    c.req.header("authorization") === "Bearer calendar-session"
      ? ({
          uid: "calendar-user",
          authority: "better-auth",
          requestId: "calendar-request",
        } as SignedAuthContext)
      : null;
  registerGoogleCalendarRoutes(app, requestContext as never, dependencies);
  return {
    request: (path: string, init: RequestInit = {}, authenticated = true) => {
      const headers = new Headers(init.headers);
      if (authenticated) {
        headers.set("authorization", "Bearer calendar-session");
      }
      if (init.body && !headers.has("content-type")) {
        headers.set("content-type", "application/json");
      }
      return app.request(`https://jobs.test${path}`, { ...init, headers }, env);
    },
  };
}

afterEach(() => {
  while (databases.length) databases.pop()?.close();
});

describe("Google Calendar Worker routes", () => {
  it("requires auth and stores only encrypted manual credentials", async () => {
    const { database, env, fetchImpl } = environment();
    const app = testApp(env, { fetchImpl, now: () => 1_000 });

    expect(
      (await app.request("/v1/integrations/google_calendar", {}, false)).status,
    ).toBe(401);
    expect(
      await (await app.request("/v1/integrations/google_calendar")).json(),
    ).toEqual({ connected: false, app_key: "google_calendar" });

    const saved = await app.request("/v1/integrations/google_calendar", {
      method: "PUT",
      body: JSON.stringify({
        connected: true,
        access_token: "manual-calendar-access",
        refresh_token: "manual-calendar-refresh",
      }),
    });
    expect(saved.status).toBe(200);
    expect(
      await (await app.request("/v1/integrations/google_calendar")).json(),
    ).toEqual({ connected: true, app_key: "google_calendar" });
    expect(
      database.database
        .prepare(
          "SELECT connected, onboarding_skipped, reauth_required, has_access_token, reauth_reason FROM cf_user_calendar_onboarding WHERE uid = ?",
        )
        .get("calendar-user"),
    ).toEqual({
      connected: 1,
      onboarding_skipped: 0,
      reauth_required: 0,
      has_access_token: 1,
      reauth_reason: null,
    });

    const row = database.database
      .prepare(
        "SELECT access_token_enc, refresh_token_enc FROM cf_google_calendar_integrations WHERE uid = ?",
      )
      .get("calendar-user") as {
      access_token_enc: string;
      refresh_token_enc: string;
    };
    expect(row.access_token_enc).toMatch(/^v1\./);
    expect(row.refresh_token_enc).toMatch(/^v1\./);
    expect(JSON.stringify(row)).not.toContain("manual-calendar");

    expect(
      (
        await app.request("/v1/integrations/google_calendar", {
          method: "DELETE",
        })
      ).status,
    ).toBe(204);
    expect(
      database.database
        .prepare(
          "SELECT connected, reauth_required, has_access_token, reauth_reason FROM cf_user_calendar_onboarding WHERE uid = ?",
        )
        .get("calendar-user"),
    ).toEqual({
      connected: 0,
      reauth_required: 0,
      has_access_token: 0,
      reauth_reason: null,
    });
    expect(
      (
        await app.request("/v1/integrations/google_calendar", {
          method: "DELETE",
        })
      ).status,
    ).toBe(404);
  });

  it("deletes the Google Calendar grant for the legacy Gmail alias", async () => {
    const { database, env, fetchImpl } = environment();
    const app = testApp(env, { fetchImpl, now: () => 1_000 });
    const saved = await app.request("/v1/integrations/google_calendar", {
      method: "PUT",
      body: JSON.stringify({
        connected: true,
        access_token: "manual-calendar-access",
      }),
    });
    expect(saved.status).toBe(200);

    const deleted = await app.request("/v1/integrations/gmail", {
      method: "DELETE",
    });
    expect(deleted.status).toBe(204);
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_google_calendar_integrations WHERE uid = ?")
        .get("calendar-user"),
    ).toEqual({ count: 0 });
    expect((await app.request("/v1/integrations/whoop", { method: "DELETE" })).status).toBe(404);
  });

  it("uses hashed single-use OAuth state and the Calendar-only scope", async () => {
    const { database, env, calls, fetchImpl } = environment();
    const app = testApp(env, { fetchImpl, now: () => 1_000 });

    const oauth = await app.request(
      "/v1/integrations/google_calendar/oauth-url",
    );
    expect(oauth.status).toBe(200);
    const authUrl = new URL(
      (await oauth.json<{ auth_url: string }>()).auth_url,
    );
    expect(authUrl.origin + authUrl.pathname).toBe(
      "https://accounts.google.com/o/oauth2/v2/auth",
    );
    expect(authUrl.searchParams.get("scope")).toBe(
      "https://www.googleapis.com/auth/calendar",
    );
    expect(authUrl.searchParams.get("redirect_uri")).toBe(
      "https://edge.example.test/v2/integrations/google-calendar/callback",
    );
    const state = authUrl.searchParams.get("state");
    expect(state).toBeTruthy();
    const stored = database.database
      .prepare("SELECT state_hash FROM cf_google_calendar_oauth_states")
      .get() as { state_hash: string };
    expect(stored.state_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(stored.state_hash).not.toBe(state);

    const callback = await app.request(
      `/v2/integrations/google-calendar/callback?code=calendar-code&state=${encodeURIComponent(state!)}`,
      {},
      false,
    );
    expect(callback.status).toBe(200);
    expect(await callback.text()).toContain("Authentication successful");
    expect(calls[0].url).toBe("https://oauth2.googleapis.com/token");
    expect(String(calls[0].init?.body)).toContain(
      "grant_type=authorization_code",
    );
    expect(
      database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_google_calendar_oauth_states",
        )
        .get(),
    ).toEqual({ count: 0 });
    const row = database.database
      .prepare(
        "SELECT connected, access_token_enc, refresh_token_enc FROM cf_google_calendar_integrations WHERE uid = ?",
      )
      .get("calendar-user") as Record<string, unknown>;
    expect(row.connected).toBe(1);
    expect(JSON.stringify(row)).not.toContain("calendar-access-initial");
    expect(JSON.stringify(row)).not.toContain("calendar-refresh");
    expect(
      database.database
        .prepare(
          "SELECT connected, reauth_required, has_access_token, reauth_reason FROM cf_user_calendar_onboarding WHERE uid = ?",
        )
        .get("calendar-user"),
    ).toEqual({
      connected: 1,
      reauth_required: 0,
      has_access_token: 1,
      reauth_reason: null,
    });

    const replay = await app.request(
      `/v2/integrations/google-calendar/callback?code=calendar-code&state=${encodeURIComponent(state!)}`,
      {},
      false,
    );
    expect(await replay.text()).toContain("invalid or expired");
    expect(calls).toHaveLength(1);
  });

  it("reuses the shared Better Auth Google OAuth client when no Calendar override exists", async () => {
    const { env } = environment(false, true);
    const app = testApp(env, { now: () => 1_000 });

    const response = await app.request(
      "/v1/integrations/google_calendar/oauth-url",
    );
    expect(response.status).toBe(200);
    const payload = await response.json<{ auth_url: string }>();
    const authUrl = new URL(payload.auth_url);
    expect(authUrl.searchParams.get("client_id")).toBe(
      "shared-google-client-id",
    );
    expect(authUrl.searchParams.get("redirect_uri")).toBe(
      "https://edge.example.test/v2/integrations/google-calendar/callback",
    );
  });

  it("keeps Google-derived integration OAuth aliases on the Worker grant", async () => {
    const { database, env, fetchImpl } = environment();
    const app = testApp(env, { fetchImpl, now: () => 1_000 });

    const gmail = await app.request("/v1/integrations/gmail/oauth-url");
    expect(gmail.status).toBe(200);
    const authUrl = new URL(
      (await gmail.json<{ auth_url: string }>()).auth_url,
    );
    expect(authUrl.searchParams.get("scope")).toBe(
      "https://www.googleapis.com/auth/calendar",
    );
    expect(
      database.database
        .prepare("SELECT uid, expires_at FROM cf_google_calendar_oauth_states")
        .get(),
    ).toEqual({ uid: "calendar-user", expires_at: 1_600 });

    expect((await app.request("/v1/integrations/whoop/oauth-url")).status).toBe(
      400,
    );
    expect(
      (await app.request("/v1/integrations/gmail/oauth-url", {}, false)).status,
    ).toBe(401);
  });

  it("refreshes an expired token and returns normalized timed/all-day events", async () => {
    const { env, calls, fetchImpl } = environment();
    let now = 1_000;
    const app = testApp(env, { fetchImpl, now: () => now });
    const authUrl = new URL(
      (
        await (
          await app.request("/v1/integrations/google_calendar/oauth-url")
        ).json<{ auth_url: string }>()
      ).auth_url,
    );
    const state = authUrl.searchParams.get("state");
    await app.request(
      `/v2/integrations/google-calendar/callback?code=calendar-code&state=${encodeURIComponent(state!)}`,
      {},
      false,
    );

    now = 2_000;
    const response = await app.request(
      "/v1/calendar/google/events?time_min=2026-09-01T00%3A00%3A00Z&time_max=2026-09-04T00%3A00%3A00Z&q=Workers&max_results=20",
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual([
      {
        event_id: "timed-event",
        title: "Workers review",
        attendees: ["Guest"],
        attendee_emails: ["guest@example.test"],
        start_time: "2026-09-01T01:00:00.000Z",
        end_time: "2026-09-01T02:00:00.000Z",
        html_link: "https://calendar.google.com/event?eid=timed",
      },
      {
        event_id: "all-day-event",
        title: "Untitled Event",
        attendees: [],
        attendee_emails: [],
        start_time: "2026-09-02T00:00:00.000Z",
        end_time: "2026-09-02T23:59:59.000Z",
        html_link: null,
      },
    ]);
    expect(calls.map((call) => call.url)).toEqual([
      "https://oauth2.googleapis.com/token",
      "https://oauth2.googleapis.com/token",
      expect.stringContaining(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events?",
      ),
    ]);
    const calendarCall = calls.at(-1)!;
    expect(new Headers(calendarCall.init?.headers).get("authorization")).toBe(
      "Bearer calendar-access-refreshed",
    );
    const calendarUrl = new URL(calendarCall.url);
    expect(calendarUrl.searchParams.get("q")).toBe("Workers");
    expect(calendarUrl.searchParams.get("maxResults")).toBe("20");
  });

  it("links a selected event in D1 and best-effort patches the provider description", async () => {
    const { database, env, calls, fetchImpl } = environment();
    const app = testApp(env, { fetchImpl, now: () => 1_000 });
    seedConversation(database, "conversation-link");
    await app.request("/v1/integrations/google_calendar", {
      method: "PUT",
      body: JSON.stringify({
        connected: true,
        access_token: "calendar-access",
        refresh_token: "calendar-refresh",
      }),
    });

    expect(
      (
        await app.request(
          "/v1/conversations/conversation-link/calendar-event",
          { method: "POST", body: JSON.stringify({ event_id: "link-event" }) },
        )
      ).status,
    ).toBe(200);
    const response = await app.request(
      "/v1/conversations/conversation-link/calendar-event",
      { method: "POST", body: JSON.stringify({ event_id: "link-event" }) },
    );
    expect(await response.json()).toMatchObject({
      event_id: "link-event",
      title: "Linked event",
      attendees: ["Guest"],
      attendee_emails: ["guest@example.test"],
      html_link: "https://calendar.google.com/event?eid=link",
    });
    const stored = database.database
      .prepare(
        "SELECT calendar_event_json FROM cf_conversations WHERE uid = ? AND id = ?",
      )
      .get("calendar-user", "conversation-link") as {
      calendar_event_json: string;
    };
    expect(JSON.parse(stored.calendar_event_json)).toMatchObject({
      event_id: "link-event",
      title: "Linked event",
    });
    expect(
      calls.some(
        (call) =>
          call.url.includes("/events/link-event?") &&
          new Headers(call.init?.headers).get("authorization") ===
            "Bearer calendar-access-refreshed",
      ),
    ).toBe(true);
    const patchCall = calls.find(
      (call) =>
        call.init?.method === "PATCH" &&
        call.url.endsWith("/events/link-event"),
    );
    expect(patchCall).toBeTruthy();
    expect(String(patchCall?.init?.body)).toContain(
      "https://h.omi.me/conversations/conversation-link",
    );

    expect(
      (
        await app.request("/v1/conversations/missing-calendar/calendar-event", {
          method: "POST",
          body: JSON.stringify({ event_id: "link-event" }),
        })
      ).status,
    ).toBe(404);
    seedConversation(database, "conversation-locked", { locked: true });
    expect(
      (
        await app.request(
          "/v1/conversations/conversation-locked/calendar-event",
          { method: "POST", body: JSON.stringify({ event_id: "link-event" }) },
        )
      ).status,
    ).toBe(402);
  });

  it("auto-links the best overlapping event and persists the normalized link", async () => {
    const { database, env, fetchImpl } = environment();
    const app = testApp(env, { fetchImpl, now: () => 1_000 });
    const startedAt = Math.floor(
      Date.parse("2026-09-01T09:15:00+08:00") / 1_000,
    );
    const finishedAt = Math.floor(
      Date.parse("2026-09-01T09:45:00+08:00") / 1_000,
    );
    seedConversation(database, "conversation-auto", {
      startedAt,
      finishedAt,
    });
    await app.request("/v1/integrations/google_calendar", {
      method: "PUT",
      body: JSON.stringify({
        connected: true,
        access_token: "calendar-access",
        refresh_token: null,
      }),
    });

    const response = await app.request(
      "/v1/conversations/conversation-auto/calendar-event/auto-link",
      { method: "POST" },
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({
      event_id: "timed-event",
      title: "Workers review",
      start_time: "2026-09-01T01:00:00.000Z",
      end_time: "2026-09-01T02:00:00.000Z",
    });
    const stored = database.database
      .prepare(
        "SELECT calendar_event_json FROM cf_conversations WHERE uid = ? AND id = ?",
      )
      .get("calendar-user", "conversation-auto") as {
      calendar_event_json: string;
    };
    expect(JSON.parse(stored.calendar_event_json).event_id).toBe("timed-event");
  });

  it("marks onboarding as reconnect-required when a stored credential is rejected", async () => {
    const { database, env, fetchImpl } = environment();
    const app = testApp(env, { fetchImpl, now: () => 1_000 });
    await app.request("/v1/integrations/google_calendar", {
      method: "PUT",
      body: JSON.stringify({
        connected: true,
        access_token: "calendar-access",
        refresh_token: null,
      }),
    });
    fetchImpl.mockImplementationOnce(
      async () => new Response(null, { status: 401 }),
    );

    const response = await app.request("/v1/calendar/google/events");
    expect(response.status).toBe(401);
    expect(
      database.database
        .prepare(
          "SELECT connected, access_token_enc, token_expires_at FROM cf_google_calendar_integrations WHERE uid = ?",
        )
        .get("calendar-user"),
    ).toEqual({
      connected: 0,
      access_token_enc: null,
      token_expires_at: null,
    });
    expect(
      database.database
        .prepare(
          "SELECT connected, reauth_required, has_access_token, reauth_reason FROM cf_user_calendar_onboarding WHERE uid = ?",
        )
        .get("calendar-user"),
    ).toEqual({
      connected: 0,
      reauth_required: 1,
      has_access_token: 0,
      reauth_reason: "token_expired",
    });
  });

  it("creates a Calendar event through the legacy-compatible tool envelope", async () => {
    const { env, calls, fetchImpl } = environment();
    const app = testApp(env, { fetchImpl, now: () => 1_000 });
    await app.request("/v1/integrations/google_calendar", {
      method: "PUT",
      body: JSON.stringify({
        connected: true,
        access_token: "calendar-access",
        refresh_token: null,
      }),
    });
    const response = await app.request("/v1/tools/calendar-events", {
      method: "POST",
      body: JSON.stringify({
        title: "Created event",
        start_time: "2026-09-01T09:00:00+08:00",
        end_time: "2026-09-01T10:00:00+08:00",
        description: "Discuss Workers",
        location: "Online",
        attendees: "guest@example.test",
      }),
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      tool_name: "create_calendar_event",
      result_text: expect.stringContaining(
        "✅ Successfully created calendar event: Created event",
      ),
      is_error: false,
    });
    const createCall = calls.find(
      (call) =>
        call.init?.method === "POST" &&
        call.url ===
          "https://www.googleapis.com/calendar/v3/calendars/primary/events",
    );
    expect(createCall).toBeTruthy();
    const body = JSON.parse(String(createCall?.init?.body));
    expect(body.start).toEqual({
      dateTime: "2026-09-01T01:00:00.000Z",
      timeZone: "UTC",
    });
    expect(body.attendees).toEqual([{ email: "guest@example.test" }]);

    const invalid = await app.request("/v1/tools/calendar-events", {
      method: "POST",
      body: JSON.stringify({
        title: "No timezone",
        start_time: "2026-09-01T09:00:00",
        end_time: "2026-09-01T10:00:00+08:00",
      }),
    });
    expect(invalid.status).toBe(422);
  });

  it("fails closed without OAuth credentials and validates event queries", async () => {
    const missing = environment(false);
    const missingApp = testApp(missing.env, {
      fetchImpl: missing.fetchImpl,
      now: () => 1_000,
    });
    expect(
      (await missingApp.request("/v1/integrations/google_calendar/oauth-url"))
        .status,
    ).toBe(503);
    const callback = await missingApp.request(
      "/v2/integrations/google-calendar/callback?code=x&state=y",
      {},
      false,
    );
    expect(await callback.text()).toContain("not configured");

    const configured = environment();
    const app = testApp(configured.env, {
      fetchImpl: configured.fetchImpl,
      now: () => 1_000,
    });
    expect(
      (
        await app.request(
          "/v1/calendar/google/events?time_min=invalid&max_results=20",
        )
      ).status,
    ).toBe(400);
    expect(
      (await app.request("/v1/calendar/google/events?max_results=101")).status,
    ).toBe(400);
  });

  it("cleans expired OAuth states", async () => {
    const { database, env, fetchImpl } = environment();
    const app = testApp(env, { fetchImpl, now: () => 1_000 });
    await app.request("/v1/integrations/google_calendar/oauth-url");
    expect(
      database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_google_calendar_oauth_states",
        )
        .get(),
    ).toEqual({ count: 1 });
    await cleanupExpiredGoogleCalendarOAuthStates(env, 1_601);
    expect(
      database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_google_calendar_oauth_states",
        )
        .get(),
    ).toEqual({ count: 0 });
  });
});
