import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SignedAuthContext } from "../workers/shared/auth-context";
import type { JobsEnv } from "../workers/jobs/env";
import {
  cleanupExpiredTaskIntegrationOAuthStates,
  registerTaskIntegrationRoutes,
  type TaskIntegrationDependencies,
} from "../workers/jobs/task-integrations";

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

type ProviderCall = { url: string; init?: RequestInit };

function provider() {
  const calls: ProviderCall[] = [];
  const fetchImpl = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      calls.push({ url, init });
      if (
        url === "https://api.todoist.com/oauth/access_token" ||
        url === "https://app.asana.com/-/oauth_token" ||
        url === "https://oauth2.googleapis.com/token" ||
        url === "https://api.clickup.com/api/v2/oauth/token"
      ) {
        const refresh = String(init?.body).includes("refresh_token");
        return Response.json({
          access_token: refresh ? "refreshed-access" : `access-${calls.length}`,
          refresh_token: refresh ? "refreshed-refresh" : "initial-refresh",
          expires_in: 3_600,
        });
      }
      if (url === "https://app.asana.com/api/1.0/users/me") {
        return Response.json({ data: { gid: "asana-user" } });
      }
      if (url === "https://tasks.googleapis.com/tasks/v1/users/@me/lists") {
        return Response.json({ items: [{ id: "google-list", title: "Omi" }] });
      }
      if (url === "https://app.asana.com/api/1.0/workspaces") {
        return Response.json({ data: [{ gid: "workspace-1", name: "Omi" }] });
      }
      if (url.includes("/workspaces/workspace-1/projects")) {
        return Response.json({
          data: [{ gid: "project-1", name: "Cloudflare" }],
        });
      }
      if (url === "https://api.clickup.com/api/v2/team") {
        return Response.json({ teams: [{ id: "team-1", name: "Omi" }] });
      }
      if (url.includes("/team/team-1/space")) {
        return Response.json({ spaces: [{ id: "space-1", name: "Workers" }] });
      }
      if (url.includes("/space/space-1/list")) {
        return Response.json({ lists: [{ id: "list-1", name: "Ship" }] });
      }
      if (url === "https://api.todoist.com/api/v1/tasks") {
        return Response.json({ id: "todoist-task" }, { status: 201 });
      }
      if (url === "https://app.asana.com/api/1.0/tasks") {
        return Response.json({ data: { gid: "asana-task" } }, { status: 201 });
      }
      if (url.includes("tasks.googleapis.com/tasks/v1/lists/")) {
        return Response.json({ id: "google-task" }, { status: 201 });
      }
      if (url.includes("api.clickup.com/api/v2/list/")) {
        return Response.json({ id: "clickup-task" }, { status: 201 });
      }
      throw new Error(`unexpected provider URL ${url}`);
    },
  );
  return { calls, fetchImpl };
}

function environment(configured = true) {
  const database = new SqliteD1();
  databases.push(database);
  const external = provider();
  const env = {
    APP_DB: database as unknown as D1Database,
    PUBLIC_API_BASE_URL: "https://edge.example.test",
    TASK_INTEGRATION_TOKEN_ENCRYPTION_SECRET: "t".repeat(64),
    ...(configured
      ? {
          TODOIST_CLIENT_ID: "todoist-id",
          TODOIST_CLIENT_SECRET: "todoist-secret",
          ASANA_CLIENT_ID: "asana-id",
          ASANA_CLIENT_SECRET: "asana-secret",
          GOOGLE_TASKS_CLIENT_ID: "google-id",
          GOOGLE_TASKS_CLIENT_SECRET: "google-secret",
          CLICKUP_CLIENT_ID: "clickup-id",
          CLICKUP_CLIENT_SECRET: "clickup-secret",
        }
      : {}),
  } as unknown as JobsEnv;
  return { database, env, external };
}

function testApp(env: JobsEnv, dependencies: TaskIntegrationDependencies) {
  const app = new Hono<{ Bindings: JobsEnv }>();
  const requestContext = async (c: {
    req: { header(name: string): string | undefined };
  }): Promise<SignedAuthContext | null> =>
    c.req.header("authorization") === "Bearer task-session"
      ? ({
          uid: "task-user",
          authority: "better-auth",
          requestId: "task-request",
        } as SignedAuthContext)
      : null;
  registerTaskIntegrationRoutes(app, requestContext as never, dependencies);
  return {
    request: (path: string, init: RequestInit = {}, authenticated = true) => {
      const headers = new Headers(init.headers);
      if (authenticated) headers.set("authorization", "Bearer task-session");
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

describe("task integration routes", () => {
  it("requires auth and persists Apple Reminders plus the default app", async () => {
    const { env, external } = environment();
    const app = testApp(env, {
      fetchImpl: external.fetchImpl,
      now: () => 1000,
    });

    expect((await app.request("/v1/task-integrations", {}, false)).status).toBe(
      401,
    );
    expect(await (await app.request("/v1/task-integrations")).json()).toEqual({
      integrations: {},
      default_app: null,
    });

    const saved = await app.request("/v1/task-integrations/apple_reminders", {
      method: "PUT",
      body: JSON.stringify({ connected: true }),
    });
    expect(saved.status).toBe(200);
    const selected = await app.request("/v1/task-integrations/default", {
      method: "PUT",
      body: JSON.stringify({ app_key: "apple_reminders" }),
    });
    expect(await selected.json()).toEqual({ default_app: "apple_reminders" });
    expect(await (await app.request("/v1/task-integrations")).json()).toEqual({
      integrations: { apple_reminders: { connected: true } },
      default_app: "apple_reminders",
    });

    expect(
      (
        await app.request("/v1/task-integrations/apple_reminders", {
          method: "DELETE",
        })
      ).status,
    ).toBe(204);
    expect(
      await (await app.request("/v1/task-integrations/default")).json(),
    ).toEqual({ default_app: null });
  });

  it("encrypts tokens, returns only a sentinel, and preserves it on setup updates", async () => {
    const { database, env, external } = environment();
    const app = testApp(env, {
      fetchImpl: external.fetchImpl,
      now: () => 2000,
    });
    await app.request("/v1/task-integrations/asana", {
      method: "PUT",
      body: JSON.stringify({
        connected: true,
        access_token: "asana-access-secret",
        refresh_token: "asana-refresh-secret",
        workspace_gid: "workspace-old",
      }),
    });
    const stored = database.database
      .prepare(
        "SELECT access_token_enc, refresh_token_enc FROM cf_task_integrations WHERE uid = ? AND app_key = ?",
      )
      .get("task-user", "asana") as Record<string, string>;
    expect(stored.access_token_enc).not.toContain("asana-access-secret");
    expect(stored.refresh_token_enc).not.toContain("asana-refresh-secret");

    const first = (await (
      await app.request("/v1/task-integrations")
    ).json()) as Record<string, any>;
    expect(first.integrations.asana).toMatchObject({
      connected: true,
      access_token: "configured",
      workspace_gid: "workspace-old",
    });
    expect(first.integrations.asana.refresh_token).toBeUndefined();

    await app.request("/v1/task-integrations/asana", {
      method: "PUT",
      body: JSON.stringify({
        ...first.integrations.asana,
        workspace_gid: "workspace-new",
        project_gid: null,
      }),
    });
    const second = (await (
      await app.request("/v1/task-integrations")
    ).json()) as Record<string, any>;
    expect(second.integrations.asana).toMatchObject({
      access_token: "configured",
      workspace_gid: "workspace-new",
    });
    expect(second.integrations.asana.project_gid).toBeUndefined();
    expect(
      database.database
        .prepare(
          "SELECT access_token_enc FROM cf_task_integrations WHERE uid = ? AND app_key = ?",
        )
        .get("task-user", "asana"),
    ).toEqual({ access_token_enc: stored.access_token_enc });
  });

  it("creates single-use OAuth state and stores callback credentials", async () => {
    const { database, env, external } = environment();
    const app = testApp(env, {
      fetchImpl: external.fetchImpl,
      now: () => 3000,
    });
    const oauth = (await (
      await app.request("/v1/task-integrations/google_tasks/oauth-url")
    ).json()) as { auth_url: string };
    const authorizationUrl = new URL(oauth.auth_url);
    expect(authorizationUrl.origin).toBe("https://accounts.google.com");
    expect(authorizationUrl.searchParams.get("redirect_uri")).toBe(
      "https://edge.example.test/v2/integrations/google-tasks/callback",
    );
    const state = authorizationUrl.searchParams.get("state")!;
    const storedState = database.database
      .prepare("SELECT state_hash FROM cf_task_integration_oauth_states")
      .get() as { state_hash: string };
    expect(storedState.state_hash).toHaveLength(64);
    expect(storedState.state_hash).not.toBe(state);

    const callback = await app.request(
      `/v2/integrations/google-tasks/callback?code=provider-code&state=${encodeURIComponent(state)}`,
      {},
      false,
    );
    expect(callback.status).toBe(200);
    expect(await callback.text()).toContain("omi://google-tasks/callback");
    const connected = (await (
      await app.request("/v1/task-integrations")
    ).json()) as Record<string, any>;
    expect(connected.integrations.google_tasks).toMatchObject({
      connected: true,
      access_token: "configured",
      default_list_id: "google-list",
      default_list_title: "Omi",
    });
    expect(
      database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_task_integration_oauth_states",
        )
        .get(),
    ).toEqual({ count: 0 });

    const replay = await app.request(
      `/v2/integrations/google-tasks/callback?code=provider-code&state=${encodeURIComponent(state)}`,
      {},
      false,
    );
    expect(await replay.text()).toContain("invalid_state");
  });

  it("fails closed when OAuth configuration is absent", async () => {
    const { env, external } = environment(false);
    const app = testApp(env, {
      fetchImpl: external.fetchImpl,
      now: () => 4000,
    });
    const start = await app.request("/v1/task-integrations/todoist/oauth-url");
    expect(start.status).toBe(503);
    expect(await start.json()).toEqual({ detail: "todoist is not configured" });
    const callback = await app.request(
      "/v2/integrations/todoist/callback?code=x&state=y",
      {},
      false,
    );
    expect(await callback.text()).toContain("config_error");
  });

  it("removes expired OAuth state during maintenance", async () => {
    const { database, env } = environment();
    database.database
      .prepare(
        "INSERT INTO cf_task_integration_oauth_states (state_hash, uid, app_key, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
      )
      .run("a".repeat(64), "task-user", "todoist", 99, 1);
    await cleanupExpiredTaskIntegrationOAuthStates(env, 100);
    expect(
      database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_task_integration_oauth_states",
        )
        .get(),
    ).toEqual({ count: 0 });
  });

  it("uses the current provider APIs for tasks and setup resources", async () => {
    const { env, external } = environment();
    const app = testApp(env, {
      fetchImpl: external.fetchImpl,
      now: () => 5000,
    });
    const integrations = [
      ["todoist", {}],
      [
        "asana",
        {
          workspace_gid: "workspace-1",
          project_gid: "project-1",
          user_gid: "asana-user",
        },
      ],
      ["google_tasks", { default_list_id: "google-list" }],
      ["clickup", { list_id: "list-1" }],
    ] as const;
    for (const [providerName, configuration] of integrations) {
      expect(
        (
          await app.request(`/v1/task-integrations/${providerName}`, {
            method: "PUT",
            body: JSON.stringify({
              connected: true,
              access_token: `${providerName}-token`,
              ...configuration,
            }),
          })
        ).status,
      ).toBe(200);
      const response = await app.request(
        `/v1/task-integrations/${providerName}/tasks`,
        {
          method: "POST",
          body: JSON.stringify({
            title: "Ship Workers",
            description: "Finish the Cloudflare path",
            due_date: "2026-09-01T12:00:00Z",
          }),
        },
      );
      expect(response.status).toBe(200);
      expect(await response.json()).toMatchObject({ success: true });
    }

    expect(
      await (
        await app.request("/v1/task-integrations/asana/workspaces")
      ).json(),
    ).toEqual({ workspaces: [{ gid: "workspace-1", name: "Omi" }] });
    expect(
      await (
        await app.request("/v1/task-integrations/asana/projects/workspace-1")
      ).json(),
    ).toEqual({ projects: [{ gid: "project-1", name: "Cloudflare" }] });
    expect(
      await (await app.request("/v1/task-integrations/clickup/teams")).json(),
    ).toEqual({ teams: [{ id: "team-1", name: "Omi" }] });
    expect(
      await (
        await app.request("/v1/task-integrations/clickup/spaces/team-1")
      ).json(),
    ).toEqual({ spaces: [{ id: "space-1", name: "Workers" }] });
    expect(
      await (
        await app.request("/v1/task-integrations/clickup/lists/space-1")
      ).json(),
    ).toEqual({ lists: [{ id: "list-1", name: "Ship" }] });

    expect(external.calls.map(({ url }) => url)).toEqual(
      expect.arrayContaining([
        "https://api.todoist.com/api/v1/tasks",
        "https://app.asana.com/api/1.0/tasks",
        "https://tasks.googleapis.com/tasks/v1/lists/google-list/tasks",
        "https://api.clickup.com/api/v2/list/list-1/task",
      ]),
    );
    expect(
      external.calls.some(({ url }) => url.includes("/rest/v2/tasks")),
    ).toBe(false);
  });

  it("refreshes expiring Google credentials before calling the provider", async () => {
    const { env, external } = environment();
    const app = testApp(env, {
      fetchImpl: external.fetchImpl,
      now: () => 6000,
    });
    await app.request("/v1/task-integrations/google_tasks", {
      method: "PUT",
      body: JSON.stringify({
        connected: true,
        access_token: "stale-access",
        refresh_token: "refresh-token",
        default_list_id: "google-list",
      }),
    });
    const created = await app.request(
      "/v1/task-integrations/google_tasks/tasks",
      {
        method: "POST",
        body: JSON.stringify({ title: "Refresh first" }),
      },
    );
    expect(created.status).toBe(200);
    expect(await created.json()).toMatchObject({
      success: true,
      external_task_id: "google-task",
    });
    const taskCall = external.calls.find(({ url }) =>
      url.includes("/lists/google-list/tasks"),
    );
    expect(new Headers(taskCall?.init?.headers).get("authorization")).toBe(
      "Bearer refreshed-access",
    );
  });
});
