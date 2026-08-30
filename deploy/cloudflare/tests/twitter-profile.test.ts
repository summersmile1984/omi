import { Hono } from "hono";
import { describe, expect, it, vi } from "vitest";
import type { SignedAuthContext } from "../workers/shared/auth-context";
import type { JobsEnv } from "../workers/jobs/env";
import { registerTwitterProfileRoutes } from "../workers/jobs/twitter-profile";

const authContext: SignedAuthContext = {
  uid: "uid-1",
  authority: "better-auth",
  requestId: "request-1",
  version: 1,
  audience: "jobs",
  assertionId: "assertion-1",
  issuedAt: 1,
  expiresAt: 2,
  method: "GET",
  path: "/v1/personas/twitter/profile",
};

function environment(options: {
  configured?: boolean;
  rows?: Array<Record<string, unknown>>;
  provider?: (request: RequestInfo | URL) => Response | Promise<Response>;
} = {}) {
  const calls: Request[] = [];
  const rows = options.rows || [];
  const db = {
    prepare(sql: string) {
      return {
        bind(...args: unknown[]) {
          return {
            async all() {
              if (sql.includes("owner_uid")) return { results: rows };
              return { results: [] };
            },
            async first() {
              return sql.includes("twitter.username") ? rows[0] || null : null;
            },
          };
        },
      };
    },
  };
  type Fetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
  const fetchImpl = vi.fn<Fetch>(async (input) =>
    options.provider ? await options.provider(input) : Response.json({}),
  );
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerTwitterProfileRoutes(
    app,
    async () => authContext,
    { fetchImpl },
  );
  const env = {
    APP_DB: db,
    RAPID_API_HOST: options.configured === false ? undefined : "twitter-api.example.test",
    RAPID_API_KEY: options.configured === false ? undefined : "rapid-key",
  } as never;
  return { app, env, calls, fetchImpl };
}

describe("Twitter profile route", () => {
  it("requires the Better Auth request context", async () => {
    const app = new Hono<{ Bindings: JobsEnv }>();
    registerTwitterProfileRoutes(app, async () => null);
    const response = await app.request("https://jobs.test/v1/personas/twitter/profile?handle=omi", {}, {} as never);
    expect(response.status).toBe(401);
  });

  it("returns a normalized RapidAPI profile and matching persona", async () => {
    const { app, env, fetchImpl } = environment({
      rows: [
        {
          id: "persona-1",
          data_json: JSON.stringify({ capabilities: ["persona"], username: "alice" }),
        },
      ],
      provider: (request) => {
        const url =
          typeof request === "string"
            ? request
            : request instanceof Request
              ? request.url
              : request.toString();
        expect(new URL(url).searchParams.get("screenname")).toBe("omi");
        return Response.json({
          name: "Omi",
          profile: "omi",
          rest_id: "42",
          avatar: "https://cdn.test/avatar_normal.jpg",
          desc: "  Worker profile  ",
          friends: "12.8",
          sub_count: 7,
        });
      },
    });
    const response = await app.request(
      "https://jobs.test/v1/personas/twitter/profile?handle=%40omi",
      {},
      env,
    );
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      name: "Omi",
      profile: "omi",
      rest_id: "42",
      avatar: "https://cdn.test/avatar.jpg",
      desc: "Worker profile",
      friends: 12,
      sub_count: 7,
      id: "",
      status: "error",
      persona_id: "persona-1",
      persona_username: "alice",
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("fails closed when RapidAPI credentials are missing or provider payload is invalid", async () => {
    const missing = environment({ configured: false });
    const missingResponse = await missing.app.request(
      "https://jobs.test/v1/personas/twitter/profile?handle=omi",
      {},
      missing.env,
    );
    expect(missingResponse.status).toBe(503);

    const invalid = environment({ provider: () => Response.json({ status: "error" }) });
    const invalidResponse = await invalid.app.request(
      "https://jobs.test/v1/personas/twitter/profile?handle=omi",
      {},
      invalid.env,
    );
    expect(invalidResponse.status).toBe(502);
  });
});
