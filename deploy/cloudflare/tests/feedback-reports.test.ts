import { describe, expect, it, vi } from "vitest";
import { Hono } from "hono";
import {
  registerFeedbackReportRoutes,
  ensureDailyFeedbackReport,
} from "../workers/jobs/feedback-reports";
import { verifyRequestAuthContext } from "../workers/shared/auth-context";
import type { JobsEnv } from "../workers/jobs/env";
import edge from "../workers/edge/index";

function fixture() {
  const calls: Request[] = [];
  const state = {
    ready: false,
    status: 200,
    unavailable: false,
    dates: [] as unknown[],
  };
  const env = {
    ADMIN_KEY: "feedback-admin-secret",
    INTERNAL_ASSERTION_SECRET: "feedback-internal-secret",
    APP_DB: {
      prepare() {
        return {
          bind(day: unknown) {
            state.dates.push(day);
            return {
              async first() {
                return state.ready ? { ready: 1 } : null;
              },
            };
          },
        };
      },
    },
    API_CORE: {
      async fetch(request: Request) {
        calls.push(request);
        if (state.unavailable) throw new Error("PRIVATE provider failure");
        return Response.json(
          { dates: ["2026-09-05"] },
          { status: state.status }
        );
      },
    },
  } as unknown as JobsEnv;
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerFeedbackReportRoutes(app);
  return { env, app, calls, state };
}

describe("feedback report admin boundary", () => {
  it("routes public Edge requests through the Jobs admin gate without accepting a user session", async () => {
    const { app, env, calls } = fixture();
    const log = vi.spyOn(console, "info").mockImplementation(() => {});
    const edgeEnv = {
      JOBS: { fetch: (request: Request) => app.fetch(request, env) },
    };
    try {
      for (const [method, suffix] of [
        ["GET", "/reports"],
        ["GET", "/reports/2026-09-05"],
        ["GET", "/events/event/context"],
        ["POST", "/reports/2026-09-05/generate"],
        ["POST", "/reports/generate-yesterday"],
      ]) {
        const denied = await edge.fetch(
          new Request(`https://edge/v1/admin/feedback${suffix}`, {
            method,
            headers: { authorization: "Bearer user-session" },
          }),
          edgeEnv as never
        );
        expect(denied.status).toBe(422);
        const allowed = await edge.fetch(
          new Request(`https://edge/v1/admin/feedback${suffix}`, {
            method,
            headers: {
              "X-Admin-Key": "feedback-admin-secret",
              "x-omi-auth-context": "forged",
            },
          }),
          edgeEnv as never
        );
        expect(allowed.status).toBe(200);
        expect(calls.at(-1)?.headers.get("x-omi-auth-context")).not.toBe(
          "forged"
        );
      }
    } finally {
      log.mockRestore();
    }
  });
  it("requires the admin secret even with a user bearer or forged internal assertion", async () => {
    const { app, env, calls } = fixture();
    for (const [key, status] of [
      [undefined, 422],
      ["wrong", 403],
      ["", 403],
    ] as const) {
      const headers = new Headers({
        authorization: "Bearer user-token",
        "x-omi-auth-context": "forged",
      });
      if (key !== undefined) headers.set("X-Admin-Key", key);
      expect(
        (
          await app.fetch(
            new Request("https://jobs/v1/admin/feedback/reports", { headers }),
            env
          )
        ).status
      ).toBe(status);
    }
    env.ADMIN_KEY = undefined;
    expect(
      (
        await app.fetch(
          new Request("https://jobs/v1/admin/feedback/reports", {
            headers: { "X-Admin-Key": "feedback-admin-secret" },
          }),
          env
        )
      ).status
    ).toBe(403);
    expect(calls).toHaveLength(0);
  });

  it("preserves all five route methods and query arguments behind fresh scoped service assertions", async () => {
    const { app, env, calls } = fixture();
    const log = vi.spyOn(console, "info").mockImplementation(() => {});
    try {
      const routes = [
        ["GET", "/reports?limit=90"],
        ["GET", "/reports/2026-9-5"],
        ["GET", "/events/event-1/context?report_date=2026-9-5"],
        ["POST", "/reports/2026-9-5/generate"],
        ["POST", "/reports/generate-yesterday"],
      ];
      for (const [method, suffix] of routes) {
        const response = await app.fetch(
          new Request(`https://jobs/v1/admin/feedback${suffix}`, {
            method,
            headers: {
              "X-Admin-Key": "feedback-admin-secret",
              "X-Admin-User": "PRIVATE-admin-uid",
              authorization: "Bearer private-token",
              cookie: "PRIVATE-cookie",
              "x-omi-auth-context": "forged",
              "x-omi-internal-signature": "forged",
            },
          }),
          env
        );
        expect(response.status).toBe(200);
        expect(response.headers.get("cache-control")).toBe("no-store");
        const forwarded = calls.at(-1)!;
        expect(forwarded.url).toBe(
          `https://api-core.internal/internal/feedback${suffix}`
        );
        expect(forwarded.method).toBe(method);
        for (const name of [
          "authorization",
          "cookie",
          "x-admin-key",
          "x-admin-user",
        ])
          expect(forwarded.headers.has(name)).toBe(false);
        const context = await verifyRequestAuthContext(
          forwarded,
          "api-core",
          env.INTERNAL_ASSERTION_SECRET
        );
        expect(context?.authority).toBe("internal");
        expect(context?.uid).toMatch(
          /^feedback-admin:[0-9a-f]{8}\/[0-9a-f]{16}$/
        );
        expect(
          await verifyRequestAuthContext(
            new Request("https://core/internal/feedback/other", {
              method,
              headers: forwarded.headers,
            }),
            "api-core",
            env.INTERNAL_ASSERTION_SECRET
          )
        ).toBeNull();
      }
      expect(log).toHaveBeenCalledTimes(5);
      expect(JSON.stringify(log.mock.calls)).not.toContain("PRIVATE");
      expect(JSON.stringify(log.mock.calls)).not.toContain(
        "feedback-admin-secret"
      );
    } finally {
      log.mockRestore();
    }
  });

  it("keeps legacy key-only administrators attributable and propagates unavailable status", async () => {
    const { app, env, calls, state } = fixture();
    const log = vi.spyOn(console, "info").mockImplementation(() => {});
    try {
      const request = () =>
        new Request("https://jobs/v1/admin/feedback/reports", {
          headers: { "X-Admin-Key": "feedback-admin-secret" },
        });
      state.status = 409;
      expect((await app.fetch(request(), env)).status).toBe(409);
      expect(
        (
          await verifyRequestAuthContext(
            calls[0]!,
            "api-core",
            env.INTERNAL_ASSERTION_SECRET
          )
        )?.uid
      ).toMatch(/\/unattributed$/);
      state.unavailable = true;
      const failed = await app.fetch(request(), env);
      expect(failed.status).toBe(503);
      expect(await failed.text()).not.toContain("PRIVATE");
    } finally {
      log.mockRestore();
    }
  });

  it("runs after 01:30 UTC, skips completed dates, and retries failures in the existing scheduled lane", async () => {
    const { env, calls, state } = fixture();
    await ensureDailyFeedbackReport(
      env,
      Date.parse("2026-09-06T01:29:59Z") / 1000
    );
    expect(calls).toHaveLength(0);
    await ensureDailyFeedbackReport(
      env,
      Date.parse("2026-09-06T01:30:00Z") / 1000
    );
    expect(calls).toHaveLength(1);
    expect(state.dates).toEqual(["2026-09-05"]);
    expect(
      (
        await verifyRequestAuthContext(
          calls[0]!,
          "api-core",
          env.INTERNAL_ASSERTION_SECRET
        )
      )?.uid
    ).toBe("feedback-scheduler");
    state.ready = true;
    await ensureDailyFeedbackReport(
      env,
      Date.parse("2026-09-06T01:35:00Z") / 1000
    );
    expect(calls).toHaveLength(1);
    state.ready = false;
    state.status = 409;
    await ensureDailyFeedbackReport(
      env,
      Date.parse("2026-09-06T01:40:00Z") / 1000
    );
    state.status = 503;
    await expect(
      ensureDailyFeedbackReport(env, Date.parse("2026-09-06T01:45:00Z") / 1000)
    ).rejects.toThrow("Daily feedback report unavailable");
  });
});
