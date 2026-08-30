import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SignedAuthContext } from "../workers/shared/auth-context";
import type { JobsEnv } from "../workers/jobs/env";
import {
  reconcileXConnections,
  registerXConnectorRoutes,
  type XConnectorDependencies,
} from "../workers/jobs/x-connector";

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
        if (/^SELECT\b/i.test(sql.trimStart())) {
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

type ProviderMode = "oauth" | "rapidapi-fallback";

function provider(mode: ProviderMode = "oauth") {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  let tokenRequests = 0;
  const fetchImpl = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      calls.push({ url, init });
      if (url === "https://api.x.com/2/oauth2/token") {
        tokenRequests += 1;
        const grant = new URLSearchParams(String(init?.body)).get("grant_type");
        return Response.json({
          access_token:
            grant === "refresh_token" ? "access-refreshed" : "access-initial",
          refresh_token:
            grant === "refresh_token" ? "refresh-new" : "refresh-initial",
          expires_in: 7_200,
          scope: "tweet.read users.read bookmark.read offline.access",
        });
      }
      const parsed = new URL(url);
      if (parsed.pathname === "/2/users/me") {
        return Response.json({
          data: { id: "x-user-1", username: "omi_cloud" },
        });
      }
      if (parsed.pathname === "/2/users/x-user-1/tweets") {
        if (mode === "rapidapi-fallback") {
          return Response.json(
            { error: "provider unavailable" },
            { status: 503 },
          );
        }
        return Response.json({
          data: [
            {
              id: "1900000000000000001",
              text: "Shipping the Omi Cloudflare Worker",
              created_at: "2026-08-30T01:00:00Z",
              lang: "en",
              public_metrics: { like_count: 7 },
            },
          ],
          meta: {},
        });
      }
      if (parsed.pathname === "/2/users/x-user-1/bookmarks") {
        return Response.json({
          data: [
            {
              id: "1900000000000000002",
              text: "D1 batch transaction notes",
              created_at: "2026-08-30T00:00:00Z",
              lang: "en",
            },
          ],
          meta: {},
        });
      }
      if (parsed.hostname === "twitter-api.example.test") {
        return Response.json({
          timeline: [
            {
              tweet_id: "1900000000000000003",
              text: "Fallback timeline post",
              created_at: "2026-08-29T23:00:00Z",
            },
          ],
        });
      }
      throw new Error(`unexpected provider URL ${url}`);
    },
  );
  return {
    calls,
    fetchImpl,
    tokenRequests: () => tokenRequests,
  };
}

function environment(
  options: { configured?: boolean; mode?: ProviderMode } = {},
) {
  const database = new SqliteD1();
  databases.push(database);
  const configured = options.configured !== false;
  const external = provider(options.mode);
  const ai = {
    run: vi.fn(async () => ({
      response: {
        memories: ["Uses Cloudflare Workers for the Omi service"],
      },
    })),
  };
  const env = {
    APP_DB: database as unknown as D1Database,
    AI: ai,
    ...(configured
      ? {
          X_OAUTH_CLIENT_ID: "x-client-id",
          X_OAUTH_CLIENT_SECRET: "x-client-secret",
          X_OAUTH_REDIRECT_URI: "https://edge.example.test/v1/x/oauth/callback",
          X_TOKEN_ENCRYPTION_SECRET: "e".repeat(64),
          RAPID_API_HOST: "twitter-api.example.test",
          RAPID_API_KEY: "rapid-key",
        }
      : {}),
  } as unknown as JobsEnv;
  return { database, env, ai, external };
}

function testApp(
  env: JobsEnv,
  dependencies: XConnectorDependencies,
  waits: Promise<unknown>[],
) {
  const app = new Hono<{ Bindings: JobsEnv }>();
  const requestContext = async (c: {
    req: { header(name: string): string | undefined };
  }): Promise<SignedAuthContext | null> =>
    c.req.header("authorization") === "Bearer x-test-session"
      ? ({
          uid: "x-user",
          authority: "better-auth",
          requestId: "x-request",
        } as SignedAuthContext)
      : null;
  registerXConnectorRoutes(app, requestContext as never, dependencies);
  const execution = {
    waitUntil(promise: Promise<unknown>) {
      waits.push(promise);
    },
    passThroughOnException() {},
    props: {},
  } as unknown as ExecutionContext;
  return {
    request: (path: string, init: RequestInit = {}, authenticated = true) => {
      const headers = new Headers(init.headers);
      if (authenticated) headers.set("authorization", "Bearer x-test-session");
      return app.fetch(
        new Request(`https://jobs.test${path}`, { ...init, headers }),
        env,
        execution,
      );
    },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  for (const database of databases.splice(0)) database.close();
});

describe("Cloudflare X connector", () => {
  it("runs encrypted PKCE OAuth, syncs D1 posts/memories, refreshes, lists, and disconnects", async () => {
    const fixedNow = 1_788_000_000;
    const state = environment();
    const waits: Promise<unknown>[] = [];
    const app = testApp(
      state.env,
      { fetchImpl: state.external.fetchImpl, now: () => fixedNow },
      waits,
    );

    const oauth = await app.request(
      "/v1/x/oauth-url?success_redirect_url=omi-computer-dev%3A%2F%2Fx%2Fcallback",
    );
    expect(oauth.status).toBe(200);
    const oauthBody = (await oauth.json()) as {
      success: boolean;
      auth_url: string;
    };
    expect(oauthBody.success).toBe(true);
    const authorizeUrl = new URL(oauthBody.auth_url);
    expect(authorizeUrl.origin).toBe("https://x.com");
    expect(authorizeUrl.searchParams.get("code_challenge_method")).toBe("S256");
    const oauthState = authorizeUrl.searchParams.get("state");
    expect(oauthState).toBeTruthy();
    const storedState = state.database.database
      .prepare("SELECT state_hash, verifier_enc FROM cf_x_oauth_states")
      .get() as { state_hash: string; verifier_enc: string };
    expect(storedState.state_hash).toHaveLength(64);
    expect(storedState.state_hash).not.toBe(oauthState);
    expect(storedState.verifier_enc).toMatch(/^v1\./);
    expect(storedState.verifier_enc).not.toContain(String(oauthState));

    const callback = await app.request(
      `/v1/x/oauth/callback?code=provider-code&state=${encodeURIComponent(String(oauthState))}`,
      {},
      false,
    );
    expect(callback.status).toBe(200);
    expect(callback.headers.get("cache-control")).toBe("no-store");
    expect(callback.headers.get("content-security-policy")).toContain(
      "default-src 'none'",
    );
    const callbackHtml = await callback.text();
    expect(callbackHtml).toContain("status=success");
    expect(callbackHtml).not.toContain("access-initial");
    expect(waits).toHaveLength(1);
    await Promise.all(waits);

    expect(
      state.database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_x_oauth_states")
        .get(),
    ).toEqual({ count: 0 });
    const connection = state.database.database
      .prepare(
        "SELECT connected, access_token_enc, refresh_token_enc, syncing, post_count, memory_count, last_sync_source " +
          "FROM cf_x_connections WHERE uid = 'x-user'",
      )
      .get() as Record<string, unknown>;
    expect(connection).toMatchObject({
      connected: 1,
      syncing: 0,
      post_count: 2,
      memory_count: 1,
      last_sync_source: "oauth",
    });
    expect(connection.access_token_enc).toMatch(/^v1\./);
    expect(connection.refresh_token_enc).toMatch(/^v1\./);
    expect(String(connection.access_token_enc)).not.toContain("access-initial");
    expect(String(connection.refresh_token_enc)).not.toContain(
      "refresh-initial",
    );

    const status = await app.request("/v1/x/connection-status");
    expect(await status.json()).toMatchObject({
      success: true,
      connected: true,
      handle: "omi_cloud",
      post_count: 2,
      memory_count: 1,
      syncing: false,
      last_sync_source: "oauth",
    });
    const posts = await app.request("/v1/x/posts?limit=10");
    const postBody = (await posts.json()) as {
      posts: Record<string, unknown>[];
    };
    expect(postBody.posts.map((post) => post.kind)).toEqual([
      "tweet",
      "bookmark",
    ]);
    expect(postBody.posts[0]).toMatchObject({
      id: "1900000000000000001",
      metrics: { like_count: 7 },
      memory_extraction_status: "completed",
    });
    const bookmarks = await app.request("/v1/x/posts?kind=bookmark&limit=1");
    expect(
      ((await bookmarks.json()) as { posts: unknown[] }).posts,
    ).toHaveLength(1);
    expect((await app.request("/v1/x/posts?kind=invalid")).status).toBe(400);
    expect((await app.request("/v1/x/posts?limit=501")).status).toBe(422);

    expect(
      state.database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_memories WHERE uid = 'x-user' AND app_id = 'x'",
        )
        .get(),
    ).toEqual({ count: 1 });
    expect(
      state.database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_usage_sources WHERE uid = 'x-user' AND source_kind = 'memory'",
        )
        .get(),
    ).toEqual({ count: 1 });
    expect(
      state.database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_vector_projection_outbox WHERE uid = 'x-user'",
        )
        .get(),
    ).toEqual({ count: 3 });

    await expect(
      reconcileXConnections(state.env, fixedNow + 6 * 60 * 60 + 1, {
        fetchImpl: state.external.fetchImpl,
      }),
    ).resolves.toBe(1);
    expect(state.external.tokenRequests()).toBe(2);
    const refreshed = state.database.database
      .prepare(
        "SELECT access_token_enc, refresh_token_enc FROM cf_x_connections WHERE uid = 'x-user'",
      )
      .get() as Record<string, string>;
    expect(refreshed.access_token_enc).not.toContain("access-refreshed");
    expect(refreshed.refresh_token_enc).not.toContain("refresh-new");
    await expect(
      reconcileXConnections(state.env, fixedNow + 6 * 60 * 60 + 1, {
        fetchImpl: state.external.fetchImpl,
      }),
    ).resolves.toBe(0);

    const repeatedSync = await app.request("/v1/x/sync", { method: "POST" });
    expect(await repeatedSync.json()).toMatchObject({
      success: true,
      source: "oauth",
      new_posts: 0,
      memories_created: 0,
    });

    const disconnected = await app.request("/v1/x/disconnect", {
      method: "POST",
    });
    expect(await disconnected.json()).toEqual({ success: true });
    expect(
      state.database.database
        .prepare(
          "SELECT connected, access_token_enc, refresh_token_enc, syncing FROM cf_x_connections WHERE uid = 'x-user'",
        )
        .get(),
    ).toEqual({
      connected: 0,
      access_token_enc: null,
      refresh_token_enc: null,
      syncing: 0,
    });
    expect(
      (
        (await (await app.request("/v1/x/posts")).json()) as {
          posts: unknown[];
        }
      ).posts,
    ).toHaveLength(2);
    expect(
      await (await app.request("/v1/x/sync", { method: "POST" })).json(),
    ).toMatchObject({ success: false, error: "not_connected" });

    const replay = await app.request(
      `/v1/x/oauth/callback?code=provider-code&state=${encodeURIComponent(String(oauthState))}`,
      {},
      false,
    );
    expect(await replay.text()).toContain("invalid_state");
  });

  it("falls back to the external RapidAPI timeline without exposing provider failures", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const fixedNow = 1_788_000_000;
    const state = environment({ mode: "rapidapi-fallback" });
    const waits: Promise<unknown>[] = [];
    const app = testApp(
      state.env,
      { fetchImpl: state.external.fetchImpl, now: () => fixedNow },
      waits,
    );
    const oauth = (await (await app.request("/v1/x/oauth-url")).json()) as {
      auth_url: string;
    };
    const oauthState = new URL(oauth.auth_url).searchParams.get("state");
    await app.request(
      `/v1/x/oauth/callback?code=provider-code&state=${encodeURIComponent(String(oauthState))}`,
      {},
      false,
    );
    await Promise.all(waits);

    const status = await (await app.request("/v1/x/connection-status")).json();
    expect(status).toMatchObject({
      connected: true,
      last_sync_source: "rapidapi",
      post_count: 1,
      memory_count: 1,
    });
    expect(
      state.external.calls.some(
        (call) => new URL(call.url).hostname === "twitter-api.example.test",
      ),
    ).toBe(true);
    expect(console.warn).toHaveBeenCalledWith(
      expect.stringContaining('"to":"rapidapi_timeline"'),
    );
  });

  it("fails closed when credentials or auth are absent and rejects untrusted deep links", async () => {
    const state = environment({ configured: false });
    const waits: Promise<unknown>[] = [];
    const app = testApp(state.env, {}, waits);
    expect(await (await app.request("/v1/x/oauth-url")).json()).toEqual({
      success: false,
      error: "x_oauth_not_configured",
    });
    expect(
      (
        await app.request(
          "/v1/x/oauth-url?success_redirect_url=https%3A%2F%2Fevil.example%2Fcallback",
        )
      ).status,
    ).toBe(200);
    expect(
      await (
        await app.request(
          "/v1/x/oauth-url?success_redirect_url=https%3A%2F%2Fevil.example%2Fcallback",
        )
      ).json(),
    ).toEqual({ success: false, error: "x_oauth_not_configured" });
    for (const [method, path] of [
      ["GET", "/v1/x/oauth-url"],
      ["GET", "/v1/x/connection-status"],
      ["GET", "/v1/x/posts"],
      ["POST", "/v1/x/sync"],
      ["POST", "/v1/x/disconnect"],
    ] as const) {
      expect((await app.request(path, { method }, false)).status).toBe(401);
    }
    const missing = await app.request("/v1/x/oauth/callback", {}, false);
    expect(missing.headers.get("content-security-policy")).toContain(
      "frame-ancestors 'none'",
    );
    expect(await missing.text()).toContain("missing_code");

    const configured = environment();
    const configuredApp = testApp(
      configured.env,
      { fetchImpl: configured.external.fetchImpl },
      [],
    );
    expect(
      await (
        await configuredApp.request(
          "/v1/x/oauth-url?success_redirect_url=https%3A%2F%2Fevil.example%2Fcallback",
        )
      ).json(),
    ).toEqual({ success: false, error: "invalid_success_redirect_url" });
    expect(
      configured.database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_x_oauth_states")
        .get(),
    ).toEqual({ count: 0 });
  });
});
