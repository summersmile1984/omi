import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SignedAuthContext } from "../workers/shared/auth-context";
import type { JobsEnv } from "../workers/jobs/env";
import {
  registerExternalAppOauthRoutes,
  type ExternalAppOauthDependencies,
} from "../workers/jobs/external-app-oauth-staging";

type SqlStatement = {
  execute(): { success: true; results: unknown[]; meta: { changes: number } };
};

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
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...(args as never[])) as
          T | undefined) ?? null,
      all: async <T>() => ({
        success: true as const,
        results: this.database.prepare(sql).all(...(args as never[])) as T[],
        meta: { changes: 0 },
      }),
      run: async () => build(args).execute(),
      execute: () => {
        if (/RETURNING\b/is.test(sql)) {
          return {
            success: true as const,
            results: this.database.prepare(sql).all(...(args as never[])),
            meta: { changes: 0 },
          };
        }
        const result = this.database.prepare(sql).run(...(args as never[]));
        return {
          success: true as const,
          results: [],
          meta: { changes: Number(result.changes) },
        };
      },
    });
    return build();
  }

  async batch(statements: SqlStatement[]) {
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
const NOW = 1_788_000_100;

function environment(
  options: {
    enabled?: boolean;
    uid?: string;
    app?: Record<string, unknown>;
    appId?: string;
  } = {},
) {
  const database = new SqliteD1();
  databases.push(database);
  const appId = options.appId || "oauth-app";
  const appOverrides = { ...options.app };
  const paid = appOverrides.is_paid;
  const privateApp = appOverrides.private;
  delete appOverrides.is_paid;
  delete appOverrides.private;
  database.database
    .prepare(
      `INSERT INTO cf_app_catalog
         (id, approved, status, disabled, is_popular, installs, rating_count,
          data_json, updated_at, owner_uid)
       VALUES (?, 1, 'approved', 0, 0, 0, 0, ?, ?, ?)`,
    )
    .run(
      appId,
      JSON.stringify({
        id: appId,
        name: "Consent Demo",
        capabilities: ["external_integration", "memories"],
        ...(paid === undefined ? {} : { is_paid: paid }),
        ...(privateApp === undefined ? {} : { private: privateApp }),
        external_integration: {
          app_home_url: "https://app.example.test/complete",
          ...appOverrides,
        },
      }),
      1_788_000_000,
      "owner-1",
    );
  const env = {
    APP_DB: database as unknown as D1Database,
    EXTERNAL_APP_OAUTH_STAGING_ENABLED:
      options.enabled === false ? "false" : "true",
  } as unknown as JobsEnv;
  return { database, env, uid: options.uid || "owner-1", appId };
}

function testApp(
  env: JobsEnv,
  dependencies: ExternalAppOauthDependencies,
  uid: string,
  authenticated = true,
) {
  const app = new Hono<{ Bindings: JobsEnv }>();
  const requestContext = async (c: {
    req: { header(name: string): string | undefined };
  }): Promise<SignedAuthContext | null> =>
    c.req.header("authorization") === "Bearer owner-session" && authenticated
      ? ({
          uid,
          authority: "better-auth",
          requestId: "oauth-test",
        } as SignedAuthContext)
      : null;
  registerExternalAppOauthRoutes(app, requestContext as never, dependencies);
  return {
    request: (pathValue: string, init: RequestInit = {}) => {
      const headers = new Headers(init.headers);
      if (authenticated) headers.set("authorization", "Bearer owner-session");
      return app.request(
        `https://jobs.test${pathValue}`,
        { ...init, headers },
        env,
      );
    },
  };
}

function csrfCookie(setCookie: string): string {
  return setCookie.split(";", 1)[0];
}

function hidden(html: string, name: string): string {
  const match = html.match(new RegExp(`name="${name}" value="([^"]+)"`));
  if (!match) throw new Error(`missing hidden ${name}`);
  return match[1];
}

function tokenRequest(
  app: ReturnType<typeof testApp>,
  appId: string,
  state: string,
  csrf: string,
  cookie = `${"omi_cf_oauth_csrf"}=${csrf}`,
) {
  const body = new URLSearchParams({ app_id: appId, state, csrf_token: csrf });
  return app.request("/v2/cf/oauth/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded", cookie },
    body,
  });
}

async function authorize(
  app: ReturnType<typeof testApp>,
  appId = "oauth-app",
  state = "client-state",
) {
  const response = await app.request(
    `/v2/cf/oauth/authorize?app_id=${appId}&state=${encodeURIComponent(state)}`,
  );
  const html = await response.text();
  const setCookie = response.headers.get("set-cookie");
  if (!setCookie) throw new Error("missing csrf cookie");
  return {
    response,
    state: hidden(html, "state"),
    csrf: hidden(html, "csrf_token"),
    cookie: csrfCookie(setCookie),
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  while (databases.length) databases.pop()?.close();
});

describe("namespaced external app OAuth staging seam", () => {
  it("requires Better Auth and remains fail-closed when disabled", async () => {
    const disabled = environment({ enabled: false });
    const disabledApp = testApp(disabled.env, {}, disabled.uid);
    const gated = await disabledApp.request(
      "/v2/cf/oauth/authorize?app_id=oauth-app",
    );
    expect(gated.status).toBe(404);
    await expect(gated.json()).resolves.toEqual({ error: "not_found" });

    const unauthenticated = environment();
    const unauthenticatedApp = testApp(
      unauthenticated.env,
      {},
      unauthenticated.uid,
      false,
    );
    const response = await unauthenticatedApp.request(
      "/v2/cf/oauth/authorize?app_id=oauth-app",
    );
    expect(response.status).toBe(401);
  });

  it("serves the exact legacy app-consent flow with Firebase form auth", async () => {
    const { env, database, appId } = environment();
    env.LEGACY_EXTERNAL_APP_OAUTH_STAGING_ENABLED = "true";
    env.FIREBASE_API_KEY = "AIza-test-key";
    env.FIREBASE_PROJECT_ID = "omi-test-project";
    env.FIREBASE_AUTH_DOMAIN = "omi-test-project.firebaseapp.com";
    env.INTERNAL_ASSERTION_SECRET = "internal-secret";
    env.AUTH = {
      fetch: async (request: Request) => {
        expect(new URL(request.url).pathname).toBe("/internal/verify-firebase");
        expect(request.headers.get("authorization")).toBe(
          `Bearer ${"f".repeat(300)}`,
        );
        return Response.json({
          uid: "legacy-user",
          authority: "firebase",
          requestId: "legacy-request",
        });
      },
    } as unknown as Fetcher;
    const app = new Hono<{ Bindings: JobsEnv }>();
    registerExternalAppOauthRoutes(
      app,
      async () => null,
      { now: () => NOW },
      { surface: "legacy" },
    );
    const authorizeResponse = await app.request(
      `https://jobs.test/v1/oauth/authorize?app_id=${appId}&state=client-state`,
      {},
      env,
    );
    expect(authorizeResponse.status).toBe(200);
    const html = await authorizeResponse.text();
    expect(html).toContain(
      "https://www.gstatic.com/firebasejs/9.6.1/firebase-app-compat.js",
    );
    expect(html).toContain('action="/v1/oauth/token"');
    expect(authorizeResponse.headers.get("content-security-policy")).toContain(
      "script-src 'unsafe-inline' https://www.gstatic.com",
    );
    const state = hidden(html, "state");
    const csrf = hidden(html, "csrf_token");
    const cookie = csrfCookie(
      authorizeResponse.headers.get("set-cookie") || "",
    );
    expect(cookie.startsWith("omi_oauth_csrf=")).toBe(true);

    const form = new FormData();
    form.set("firebase_id_token", "f".repeat(300));
    form.set("app_id", appId);
    form.set("state", state);
    form.set("csrf_token", csrf);
    const tokenResponse = await app.request(
      "https://jobs.test/v1/oauth/token",
      {
        method: "POST",
        headers: { cookie },
        body: form,
      },
      env,
    );
    expect(tokenResponse.status).toBe(200);
    await expect(tokenResponse.json()).resolves.toEqual({
      uid: "legacy-user",
      redirect_url: "https://app.example.test/complete",
      state: "client-state",
    });
    expect(
      database.database
        .prepare("SELECT uid, status FROM cf_external_app_oauth_transactions")
        .get(),
    ).toMatchObject({ uid: "legacy-user", status: "consumed" });
  });

  it("uses hash-only double-submit CSRF and consumes a transaction once", async () => {
    const { env, database, uid, appId } = environment();
    const app = testApp(env, { now: () => NOW }, uid);
    const start = await authorize(app, appId);
    expect(start.response.status).toBe(200);
    expect(start.response.headers.get("cache-control")).toBe("no-store");
    expect(start.response.headers.get("content-security-policy")).toContain(
      "form-action 'self'",
    );
    const row = database.database
      .prepare(
        "SELECT state_hash, csrf_hash, status, client_state FROM cf_external_app_oauth_transactions",
      )
      .get() as {
      state_hash: string;
      csrf_hash: string;
      status: string;
      client_state: string;
    };
    expect(row).toMatchObject({
      status: "pending",
      client_state: "client-state",
    });
    expect(row.state_hash).not.toContain(start.state);
    expect(row.csrf_hash).not.toContain(start.csrf);

    const success = await tokenRequest(
      app,
      appId,
      start.state,
      start.csrf,
      start.cookie,
    );
    expect(success.status).toBe(200);
    await expect(success.json()).resolves.toEqual({
      uid,
      redirect_url: "https://app.example.test/complete",
      state: "client-state",
    });
    expect(
      database.database
        .prepare("SELECT uid, app_id FROM cf_user_enabled_apps")
        .all(),
    ).toEqual([{ uid, app_id: appId }]);
    expect(
      database.database
        .prepare("SELECT status FROM cf_external_app_oauth_transactions")
        .get(),
    ).toEqual({ status: "consumed" });

    const replay = await tokenRequest(
      app,
      appId,
      start.state,
      start.csrf,
      start.cookie,
    );
    expect(replay.status).toBe(400);
    await expect(replay.json()).resolves.toEqual({
      error: "oauth_request_invalid",
    });
    const mismatch = await tokenRequest(
      app,
      appId,
      start.state,
      start.csrf,
      "omi_cf_oauth_csrf=wrong-cookie-secret-123456789012345678901234567890",
    );
    expect(mismatch.status).toBe(403);
    await expect(mismatch.json()).resolves.toEqual({ error: "csrf_invalid" });
  });

  it("rejects duplicate form fields and unsafe setup targets before external fetch", async () => {
    const { env, uid, appId } = environment({
      app: { setup_completed_url: "https://[::ffff:7f00:1]/setup" },
    });
    const fetchImpl = vi.fn(async () =>
      Response.json({ is_setup_completed: true }),
    );
    const app = testApp(env, { fetchImpl, now: () => NOW }, uid);
    const unsafe = await app.request(`/v2/cf/oauth/authorize?app_id=${appId}`);
    expect(unsafe.status).toBe(400);
    await expect(unsafe.json()).resolves.toEqual({
      error: "external_setup_target_unsafe",
    });
    expect(fetchImpl).not.toHaveBeenCalled();

    const normal = environment();
    const normalApp = testApp(normal.env, { now: () => NOW }, normal.uid);
    const started = await authorize(normalApp, normal.appId);
    const body = new URLSearchParams({
      app_id: normal.appId,
      state: started.state,
      csrf_token: started.csrf,
    });
    body.append("state", started.state);
    const duplicate = await normalApp.request("/v2/cf/oauth/token", {
      method: "POST",
      headers: {
        "content-type": "application/x-www-form-urlencoded",
        cookie: started.cookie,
      },
      body,
    });
    expect(duplicate.status).toBe(422);
    await expect(duplicate.json()).resolves.toEqual({
      error: "invalid_request",
    });
  });

  it("checks setup completion, paid entitlement, and catalog revision CAS", async () => {
    const fetchImpl = vi.fn(async () =>
      Response.json({ is_setup_completed: false }),
    );
    const fixture = environment({
      app: { setup_completed_url: "https://setup.example.test/status" },
    });
    const app = testApp(
      fixture.env,
      { fetchImpl, now: () => NOW },
      fixture.uid,
    );
    const first = await authorize(app, fixture.appId);
    const incomplete = await tokenRequest(
      app,
      fixture.appId,
      first.state,
      first.csrf,
      first.cookie,
    );
    expect(incomplete.status).toBe(400);
    await expect(incomplete.json()).resolves.toEqual({
      error: "external_setup_incomplete",
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [setupTarget, setupInit] = fetchImpl.mock.calls[0] as unknown as [
      RequestInfo | URL,
      RequestInit,
    ];
    expect(String(setupTarget)).toBe(
      "https://setup.example.test/status?uid=owner-1",
    );
    expect(setupInit).toMatchObject({ method: "GET", redirect: "error" });

    const paid = environment({
      app: { is_paid: true },
    });
    const paidApp = testApp(paid.env, { now: () => NOW }, paid.uid);
    const paidStart = await authorize(paidApp, paid.appId);
    const notEntitled = await tokenRequest(
      paidApp,
      paid.appId,
      paidStart.state,
      paidStart.csrf,
      paidStart.cookie,
    );
    expect(notEntitled.status).toBe(403);
    await expect(notEntitled.json()).resolves.toEqual({
      error: "external_app_not_entitled",
    });

    const changed = environment();
    const changedApp = testApp(changed.env, { now: () => NOW }, changed.uid);
    const changedStart = await authorize(changedApp, changed.appId);
    changed.database.database
      .prepare("UPDATE cf_app_catalog SET updated_at = ? WHERE id = ?")
      .run(NOW, changed.appId);
    const stale = await tokenRequest(
      changedApp,
      changed.appId,
      changedStart.state,
      changedStart.csrf,
      changedStart.cookie,
    );
    expect(stale.status).toBe(400);
    await expect(stale.json()).resolves.toEqual({
      error: "oauth_request_invalid",
    });
    expect(
      changed.database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_user_enabled_apps")
        .get(),
    ).toEqual({ count: 0 });
  });
});
