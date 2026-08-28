import { describe, expect, it, vi } from "vitest";
import edge from "../workers/edge/index";
import { decodeAuthContext } from "../workers/shared/auth-context";
import { verifyRealtimeTicket } from "../workers/shared/realtime-ticket";

const rawService = (
  handler: (request: Request) => Promise<Response> | Response,
) => ({ fetch: handler }) as Fetcher;

const service = (handler: (request: Request) => Promise<Response> | Response) =>
  rawService((request) => {
    if (new URL(request.url).pathname === "/v1/account/cutover/control") {
      return Response.json({
        state: "new",
        client_action: "none",
        product_traffic_allowed: true,
        migration: { destination_backend_bound: true },
      });
    }
    return handler(request);
  });

const rateLimits = (
  handler: (request: Request) => Promise<Response> | Response,
  names?: string[],
) =>
  ({
    idFromName(name: string) {
      names?.push(name);
      return name;
    },
    get() {
      return { fetch: handler };
    },
  }) as unknown as DurableObjectNamespace;

const allowRateLimit = () =>
  Response.json({
    allowed: true,
    limit: 300,
    remaining: 299,
    retryAfter: 0,
    resetAt: Date.now() + 3_600_000,
  });

describe("edge gateway", () => {
  it("serves a versioned health response", async () => {
    const response = await edge.fetch(
      new Request("https://edge.test/health"),
      {} as never,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      status: "ok",
      service: "edge",
      version: "cf-01",
    });
  });

  it("reports dependency readiness only through service bindings", async () => {
    const paths: Record<string, string> = {};
    const dependency = (name: string, status = 200) =>
      rawService((request) => {
        paths[name] = new URL(request.url).pathname;
        return new Response(null, { status });
      });
    const env = {
      AUTH: dependency("auth"),
      API_CORE: dependency("api-core"),
      API_AI: dependency("api-ai"),
      REALTIME: dependency("realtime"),
      JOBS: dependency("jobs"),
      RATE_LIMITS: rateLimits((request) => {
        paths["rate-limit"] = new URL(request.url).pathname;
        return Response.json({ status: "ok" });
      }),
    };
    const ready = await edge.fetch(
      new Request("https://edge.test/ready"),
      env as never,
    );
    expect(ready.status).toBe(200);
    expect(await ready.json()).toEqual({
      status: "ready",
      service: "edge",
      dependencies: {
        auth: 200,
        "api-core": 200,
        "api-ai": 200,
        realtime: 200,
        jobs: 200,
        "rate-limit": 200,
      },
    });
    expect(paths).toEqual({
      auth: "/ready",
      "api-core": "/health",
      "api-ai": "/health",
      realtime: "/health",
      jobs: "/health",
      "rate-limit": "/health",
    });

    env.API_AI = dependency("api-ai", 503);
    const degraded = await edge.fetch(
      new Request("https://edge.test/ready"),
      env as never,
    );
    expect(degraded.status).toBe(503);
    expect(await degraded.json()).toMatchObject({
      status: "degraded",
      dependencies: { "api-ai": 503 },
    });
  });

  it("forwards the canonical Better Auth path and browser credentials", async () => {
    let forwarded: Request | undefined;
    const response = await edge.fetch(
      new Request(
        "https://edge.test/api/better-auth/callback/google?code=opaque",
        {
          headers: {
            authorization: "Bearer session",
            cookie: "__Secure-better-auth.state=opaque",
          },
        },
      ),
      {
        AUTH: rawService((request) => {
          forwarded = request;
          return new Response(null, {
            status: 302,
            headers: { location: "https://edge.test/conversations" },
          });
        }),
      } as never,
    );

    expect(response.status).toBe(302);
    expect(new URL(forwarded?.url || "https://invalid.test").pathname).toBe(
      "/api/better-auth/callback/google",
    );
    expect(forwarded?.headers.get("authorization")).toBe("Bearer session");
    expect(forwarded?.headers.get("cookie")).toBe(
      "__Secure-better-auth.state=opaque",
    );
  });

  it("routes static app catalog metadata through the public core worker", async () => {
    const paths: string[] = [];
    const env = {
      API_CORE: service((request) => {
        paths.push(new URL(request.url).pathname);
        return Response.json([]);
      }),
    };
    for (const path of [
      "/v1/app-categories",
      "/v1/app/proactive-notification-scopes",
      "/v1/app-capabilities",
      "/v1/app/payment-plans",
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`),
        env as never,
      );
      expect(response.status).toBe(200);
    }
    expect(paths).toEqual([
      "/v1/app-categories",
      "/v1/app/proactive-notification-scopes",
      "/v1/app-capabilities",
      "/v1/app/payment-plans",
    ]);
  });

  it("keeps task previews public and signs the Better Auth display name for share creation", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({
            uid: "user-1",
            authority: "better-auth",
            displayName: "Alice",
          });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ status: "ok" });
      }),
    };

    const preview = await edge.fetch(
      new Request("https://edge.test/v1/action-items/shared/public-token"),
      env as never,
    );
    const share = await edge.fetch(
      new Request("https://edge.test/v1/action-items/share", {
        method: "POST",
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );

    expect(preview.status).toBe(200);
    expect(share.status).toBe(200);
    expect(
      coreRequests.map((request) => new URL(request.url).pathname),
    ).toEqual([
      "/v1/action-items/shared/public-token",
      "/v1/action-items/share",
    ]);
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeNull();
    expect(
      decodeAuthContext(coreRequests[1].headers.get("x-omi-auth-context")),
    ).toMatchObject({ uid: "user-1", displayName: "Alice" });
  });

  it("keeps approved app catalog public while guarding authenticated app reads", async () => {
    const corePaths: string[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        corePaths.push(new URL(request.url).pathname);
        return Response.json([]);
      }),
    };
    const approved = await edge.fetch(
      new Request("https://edge.test/v1/approved-apps"),
      env as never,
    );
    const popularUnauthenticated = await edge.fetch(
      new Request("https://edge.test/v1/apps/popular"),
      env as never,
    );
    const popular = await edge.fetch(
      new Request("https://edge.test/v1/apps/popular", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );
    const detailUnauthenticated = await edge.fetch(
      new Request("https://edge.test/v1/apps/summary-app"),
      env as never,
    );
    const detail = await edge.fetch(
      new Request("https://edge.test/v1/apps/summary-app", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );

    expect(approved.status).toBe(200);
    expect(popularUnauthenticated.status).toBe(401);
    expect(popular.status).toBe(200);
    expect(detailUnauthenticated.status).toBe(401);
    expect(detail.status).toBe(200);
    expect(corePaths).toEqual([
      "/v1/approved-apps",
      "/v1/apps/popular",
      "/v1/apps/summary-app",
    ]);
  });

  it("routes app installation state through the authenticated core worker", async () => {
    const corePaths: string[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        corePaths.push(`${request.method} ${new URL(request.url).pathname}`);
        return Response.json({ status: "ok" });
      }),
    };
    const auth = { authorization: "Bearer opaque-session" };
    expect(
      (
        await edge.fetch(
          new Request("https://edge.test/v1/apps/enabled", { headers: auth }),
          env as never,
        )
      ).status,
    ).toBe(200);
    expect(
      (
        await edge.fetch(
          new Request("https://edge.test/v1/apps/enable?app_id=free-app", {
            method: "POST",
            headers: auth,
          }),
          env as never,
        )
      ).status,
    ).toBe(200);
    expect(
      (
        await edge.fetch(
          new Request("https://edge.test/v1/apps/disable?app_id=free-app", {
            method: "POST",
            headers: auth,
          }),
          env as never,
        )
      ).status,
    ).toBe(200);
    expect(corePaths).toEqual([
      "GET /v1/apps/enabled",
      "POST /v1/apps/enable",
      "POST /v1/apps/disable",
    ]);
  });

  it("routes the v2 public app catalog through the core worker", async () => {
    let forwardedPath = "";
    const env = {
      API_CORE: service((request) => {
        forwardedPath = new URL(request.url).pathname;
        return Response.json({ groups: [] });
      }),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v2/apps?limit=20"),
      env as never,
    );
    expect(response.status).toBe(200);
    expect(forwardedPath).toBe("/v2/apps");
  });

  it("routes grouped capability app catalog reads through the core worker", async () => {
    let forwardedPath = "";
    const env = {
      API_CORE: service((request) => {
        forwardedPath = new URL(request.url).pathname;
        return Response.json({ groups: [] });
      }),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v2/apps/capability/chat/grouped"),
      env as never,
    );
    expect(response.status).toBe(200);
    expect(forwardedPath).toBe("/v2/apps/capability/chat/grouped");
  });

  it("guards and routes authenticated app catalog search", async () => {
    let forwardedPath = "";
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        forwardedPath = new URL(request.url).pathname;
        return Response.json({ data: [], pagination: {}, filters: {} });
      }),
    };
    expect(
      (
        await edge.fetch(
          new Request("https://edge.test/v2/apps/search"),
          env as never,
        )
      ).status,
    ).toBe(401);
    const response = await edge.fetch(
      new Request("https://edge.test/v2/apps/search?q=chat", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );
    expect(response.status).toBe(200);
    expect(forwardedPath).toBe("/v2/apps/search");
  });

  it("exchanges an httpOnly session cookie for a short-lived realtime ticket", async () => {
    let verifyRequest: Request | undefined;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        verifyRequest = request;
        return Response.json({ uid: "cookie-user", authority: "better-auth" });
      }),
      API_CORE: service(() =>
        Response.json({ error: "wrong owner" }, { status: 500 }),
      ),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/realtime/web-ticket", {
        method: "POST",
        headers: {
          cookie: "__Secure-better-auth.session_token=cookie-session",
        },
      }),
      env as never,
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(verifyRequest?.headers.get("cookie")).toBe(
      "__Secure-better-auth.session_token=cookie-session",
    );
    const body = (await response.json()) as { ticket: string };
    expect(
      await verifyRealtimeTicket(body.ticket, "test-secret"),
    ).toMatchObject({
      uid: "cookie-user",
      authority: "better-auth",
    });
  });

  it("strips caller auth headers before forwarding verified context", async () => {
    let forwarded: Headers | undefined;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      ALLOWED_ORIGINS: "https://edge.test",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        forwarded = request.headers;
        return Response.json({ status: "ok" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/cf/probe", {
        headers: {
          authorization: "Bearer opaque-session",
          "x-omi-uid": "attacker",
          "x-omi-auth-context": "attacker-context",
        },
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(forwarded?.get("x-omi-uid")).toBeNull();
    expect(forwarded?.get("x-omi-auth-context")).toBeTruthy();
    expect(forwarded?.get("x-omi-internal-signature")).toBeTruthy();
    expect(forwarded?.get("authorization")).toBeNull();
    expect(forwarded?.get("cookie")).toBeNull();
  });

  it("keeps the users profile read beside Better Auth", async () => {
    let profileRequest: Request | undefined;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        profileRequest = request;
        return Response.json({ uid: "user-1", email: "user@example.test" });
      }),
      API_CORE: service(() =>
        Response.json({ error: "wrong owner" }, { status: 500 }),
      ),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/users/profile", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      uid: "user-1",
      email: "user@example.test",
    });
    expect(profileRequest && new URL(profileRequest.url).pathname).toBe(
      "/internal/profile",
    );
    expect(profileRequest?.headers.get("x-omi-auth-context")).toBeTruthy();
    expect(
      profileRequest?.headers.get("x-omi-internal-signature"),
    ).toBeTruthy();
  });

  it("routes developer webhook configuration to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ status: "ok" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request(
        "https://edge.test/v1/users/developer/webhook/realtime_transcript/enable",
        {
          method: "POST",
          headers: { authorization: "Bearer opaque-session" },
        },
      ),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/users/developer/webhook/realtime_transcript/enable",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes conversation projection reads to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json([]);
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/cf/conversations?limit=10", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe("/v1/cf/conversations");
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes conversation search and deletion to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const rateLimitNames: string[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ status: "Ok" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
      RATE_LIMITS: rateLimits(allowRateLimit, rateLimitNames),
    };
    const auth = { authorization: "Bearer opaque-session" };
    expect(
      (
        await edge.fetch(
          new Request("https://edge.test/v1/conversations/search", {
            method: "POST",
            headers: { ...auth, "content-type": "application/json" },
            body: JSON.stringify({ query: "roadmap" }),
          }),
          env,
        )
      ).status,
    ).toBe(200);
    expect(
      (
        await edge.fetch(
          new Request("https://edge.test/v1/conversations/conversation-1", {
            method: "DELETE",
            headers: auth,
          }),
          env,
        )
      ).status,
    ).toBe(200);
    expect(
      coreRequests.map(
        (request) => `${request.method} ${new URL(request.url).pathname}`,
      ),
    ).toEqual([
      "POST /v1/conversations/search",
      "DELETE /v1/conversations/conversation-1",
    ]);
    expect(
      coreRequests.every((request) =>
        Boolean(request.headers.get("x-omi-auth-context")),
      ),
    ).toBe(true);
    expect(rateLimitNames).toEqual(["conversations:search:user-1"]);
  });

  it("routes canonical conversation photos to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json([]);
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/conversations/conversation-1/photos", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/conversations/conversation-1/photos",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes the canonical memory CRUD surface to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const rateLimitNames: string[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ status: "ok" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
      RATE_LIMITS: rateLimits(allowRateLimit, rateLimitNames),
    };
    const authorization = { authorization: "Bearer opaque-session" };
    const cases = [
      ["GET", "/v3/memories?limit=25&offset=0"],
      ["POST", "/v3/memories"],
      ["PATCH", "/v3/memories/memory-1?value=edited"],
      ["PATCH", "/v3/memories/memory-1/visibility?value=public"],
      ["POST", "/v3/memories/memory-1/review?value=true"],
      ["DELETE", "/v3/memories/batch"],
      ["DELETE", "/v3/memories/memory-1"],
      ["DELETE", "/v3/memories"],
    ] as const;

    for (const [method, path] of cases) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method,
          headers: authorization,
        }),
        env,
      );
      expect(response.status, `${method} ${path}`).toBe(200);
    }

    expect(
      coreRequests.map(
        (request) => `${request.method} ${new URL(request.url).pathname}`,
      ),
    ).toEqual([
      "GET /v3/memories",
      "POST /v3/memories",
      "PATCH /v3/memories/memory-1",
      "PATCH /v3/memories/memory-1/visibility",
      "POST /v3/memories/memory-1/review",
      "DELETE /v3/memories/batch",
      "DELETE /v3/memories/memory-1",
      "DELETE /v3/memories",
    ]);
    expect(
      coreRequests.every((request) =>
        request.headers.has("x-omi-auth-context"),
      ),
    ).toBe(true);
    expect(rateLimitNames).toEqual([
      "memories:create:user-1",
      "memories:modify:user-1",
      "memories:modify:user-1",
      "memories:modify:user-1",
      "memories:delete_batch:user-1",
      "memories:delete:user-1",
      "memories:delete_all:user-1",
    ]);
  });

  it("routes canonical conversation segment text edits to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ status: "Ok" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request(
        "https://edge.test/v1/conversations/conversation-1/segments/text",
        {
          method: "PATCH",
          headers: {
            authorization: "Bearer opaque-session",
            "content-type": "application/json",
          },
          body: JSON.stringify({ segment_id: "segment-1", text: "edited" }),
        },
      ),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/conversations/conversation-1/segments/text",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes canonical conversation summary edits to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ status: "Ok" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/conversations/conversation-1/summary", {
        method: "PATCH",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "application/json",
        },
        body: JSON.stringify({ content: "edited summary" }),
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/conversations/conversation-1/summary",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes calendar-event unlinking to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ status: "Ok" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request(
        "https://edge.test/v1/conversations/conversation-1/calendar-event",
        {
          method: "DELETE",
          headers: { authorization: "Bearer opaque-session" },
        },
      ),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/conversations/conversation-1/calendar-event",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes canonical conversation recording existence to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ has_recording: false });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request(
        "https://edge.test/v1/conversations/conversation-1/recording",
        {
          headers: { authorization: "Bearer opaque-session" },
        },
      ),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/conversations/conversation-1/recording",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes canonical conversation transcripts to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({
          deepgram: [],
          soniox: [],
          speechmatics: [],
          whisperx: [],
        });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request(
        "https://edge.test/v1/conversations/conversation-1/transcripts",
        {
          headers: { authorization: "Bearer opaque-session" },
        },
      ),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/conversations/conversation-1/transcripts",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes canonical conversation analytics to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({
          conversation_id: "conversation-1",
          speakers: [],
        });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request(
        "https://edge.test/v1/conversations/conversation-1/analytics",
        {
          headers: { authorization: "Bearer opaque-session" },
        },
      ),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/conversations/conversation-1/analytics",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes canonical conversation event updates to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ status: "Ok" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/conversations/conversation-1/events", {
        method: "PATCH",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "application/json",
        },
        body: JSON.stringify({ events_idx: [0], values: [true] }),
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/conversations/conversation-1/events",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes canonical conversation action-item updates to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ status: "Ok" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request(
        "https://edge.test/v1/conversations/conversation-1/action-items",
        {
          method: "PATCH",
          headers: {
            authorization: "Bearer opaque-session",
            "content-type": "application/json",
          },
          body: JSON.stringify({ items_idx: [0], values: [true] }),
        },
      ),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/conversations/conversation-1/action-items",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes canonical conversation action-item description updates to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ status: "Ok" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request(
        "https://edge.test/v1/conversations/conversation-1/action-items/0",
        {
          method: "PATCH",
          headers: {
            authorization: "Bearer opaque-session",
            "content-type": "application/json",
          },
          body: JSON.stringify({
            old_description: "task",
            description: "renamed",
          }),
        },
      ),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/conversations/conversation-1/action-items/0",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes canonical conversation action-item deletes to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ status: "Ok" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request(
        "https://edge.test/v1/conversations/conversation-1/action-items",
        {
          method: "DELETE",
          headers: {
            authorization: "Bearer opaque-session",
            "content-type": "application/json",
          },
          body: JSON.stringify({ description: "task", completed: false }),
        },
      ),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/conversations/conversation-1/action-items",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes the prerecorded STT contract to the API worker", async () => {
    let aiPath = "";
    const rateLimitNames: string[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() =>
        Response.json({ error: "wrong owner" }, { status: 500 }),
      ),
      API_AI: service((request) => {
        aiPath = new URL(request.url).pathname;
        return Response.json(
          { error: "transcription provider is not configured" },
          { status: 503 },
        );
      }),
      REALTIME: service(() => Response.json({ status: "ok" })),
      RATE_LIMITS: rateLimits(allowRateLimit, rateLimitNames),
    };
    const unauthenticated = await edge.fetch(
      new Request("https://edge.test/v1/stt/transcribe", {
        method: "POST",
        body: "audio",
      }),
      env,
    );
    expect(unauthenticated.status).toBe(401);
    expect(aiPath).toBe("");
    const response = await edge.fetch(
      new Request("https://edge.test/v1/stt/transcribe", {
        method: "POST",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "multipart/form-data",
        },
        body: "audio",
      }),
      env,
    );
    expect(response.status).toBe(503);
    expect(aiPath).toBe("/v1/stt/transcribe");
    expect(rateLimitNames).toEqual(["stt:transcribe:user-1"]);
  });

  it("routes account cutover control to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: rawService((request) => {
        coreRequests.push(request);
        return Response.json({ state: "legacy" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/account/cutover/control", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/account/cutover/control",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("admits Cloudflare product traffic only for a bound new account", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: rawService((request) => {
        coreRequests.push(request);
        if (new URL(request.url).pathname === "/v1/account/cutover/control") {
          return Response.json({
            state: "legacy",
            client_action: "none",
            product_traffic_allowed: true,
            migration: { destination_backend_bound: false },
          });
        }
        return Response.json({ status: "unexpected" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };

    const response = await edge.fetch(
      new Request("https://edge.test/v3/memories", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );

    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({
      error: "account data plane not active",
      state: "legacy",
    });
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/account/cutover/control",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("fails product traffic closed when account control is unavailable", async () => {
    let memoryCalls = 0;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: rawService((request) => {
        if (new URL(request.url).pathname === "/v1/account/cutover/control") {
          return Response.json({ error: "unavailable" }, { status: 503 });
        }
        memoryCalls += 1;
        return Response.json([]);
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };

    const response = await edge.fetch(
      new Request("https://edge.test/v3/memories", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );

    expect(response.status).toBe(503);
    expect(memoryCalls).toBe(0);
  });

  it("keeps authenticated profile bootstrap reachable outside the product data plane", async () => {
    const authPaths: string[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        authPaths.push(new URL(request.url).pathname);
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ uid: "user-1", email: "user@example.test" });
      }),
    };

    const response = await edge.fetch(
      new Request("https://edge.test/v1/users/profile", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );

    expect(response.status).toBe(200);
    expect(authPaths).toEqual(["/internal/verify", "/internal/profile"]);
  });

  it("routes async native transcription bodies to the jobs worker and rewrites status reads", async () => {
    const jobRequests: Request[] = [];
    const rateLimitNames: string[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() =>
        Response.json({ error: "wrong owner" }, { status: 500 }),
      ),
      API_AI: service(() =>
        Response.json({ error: "wrong owner" }, { status: 500 }),
      ),
      REALTIME: service(() => Response.json({ status: "ok" })),
      JOBS: service(async (request) => {
        jobRequests.push(request);
        return Response.json(
          { status: request.method === "POST" ? "queued" : "completed" },
          { status: 202 },
        );
      }),
      RATE_LIMITS: rateLimits(allowRateLimit, rateLimitNames),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/stt/transcribe-async", {
        method: "POST",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "audio/wav",
          "idempotency-key": "capture-1",
        },
        body: new Uint8Array([1, 2, 3]),
      }),
      env,
    );
    expect(response.status).toBe(202);
    expect(jobRequests).toHaveLength(1);
    expect(new URL(jobRequests[0].url).pathname).toBe(
      "/v1/cf/transcription-jobs",
    );
    expect(jobRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
    expect(jobRequests[0].headers.get("idempotency-key")).toBe("capture-1");
    expect(new Uint8Array(await jobRequests[0].arrayBuffer())).toEqual(
      new Uint8Array([1, 2, 3]),
    );

    const status = await edge.fetch(
      new Request("https://edge.test/v1/stt/transcribe-async/job-1", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );
    expect(status.status).toBe(202);
    expect(new URL(jobRequests[1].url).pathname).toBe(
      "/v1/cf/transcription-jobs/job-1",
    );
    expect(rateLimitNames).toEqual(["stt:transcribe:user-1"]);
  });

  it("routes the translation contract to the API AI worker", async () => {
    let aiPath = "";
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() =>
        Response.json({ error: "wrong owner" }, { status: 500 }),
      ),
      API_AI: service((request) => {
        aiPath = new URL(request.url).pathname;
        return Response.json({ translations: [] });
      }),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/translate", {
        method: "POST",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "application/json",
        },
        body: JSON.stringify({
          contents: ["hello"],
          target_language_code: "zh",
        }),
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(aiPath).toBe("/v1/translate");
  });

  it("routes realtime mint and usage contracts to the API AI worker", async () => {
    const aiPaths: string[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() =>
        Response.json({ error: "wrong owner" }, { status: 500 }),
      ),
      API_AI: service(async (request) => {
        aiPaths.push(new URL(request.url).pathname);
        return Response.json({ provider: "openai", token: "ephemeral" });
      }),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    for (const path of ["/v2/realtime/session", "/v2/realtime/usage"]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method: "POST",
          headers: {
            authorization: "Bearer opaque-session",
            "content-type": "application/json",
          },
          body: JSON.stringify({ provider: "openai" }),
        }),
        env,
      );
      expect(response.status).toBe(200);
    }
    expect(aiPaths).toEqual(["/v2/realtime/session", "/v2/realtime/usage"]);
  });

  it("routes the native Workers AI TTS contract to the API AI worker", async () => {
    let aiPath = "";
    const rateLimitNames: string[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() =>
        Response.json({ error: "wrong owner" }, { status: 500 }),
      ),
      API_AI: service((request) => {
        aiPath = new URL(request.url).pathname;
        return new Response("audio", {
          status: 200,
          headers: { "content-type": "audio/mpeg" },
        });
      }),
      REALTIME: service(() => Response.json({ status: "ok" })),
      RATE_LIMITS: rateLimits(
        () =>
          Response.json({
            allowed: true,
            limit: 300,
            remaining: 299,
            retryAfter: 0,
            resetAt: Date.now() + 3_600_000,
          }),
        rateLimitNames,
      ),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/tts/synthesize-workers-ai", {
        method: "POST",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "application/json",
        },
        body: JSON.stringify({ text: "hello" }),
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(aiPath).toBe("/v1/tts/synthesize-workers-ai");
    expect(rateLimitNames).toEqual(["tts:synthesize:user-1"]);
  });

  it("returns 429 before invoking TTS when the Durable Object limit is exhausted", async () => {
    let aiCalls = 0;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() => Response.json({ status: "ok" })),
      API_AI: service(() => {
        aiCalls += 1;
        return Response.json({ status: "wrong" });
      }),
      RATE_LIMITS: rateLimits(() =>
        Response.json({
          allowed: false,
          limit: 300,
          remaining: 0,
          retryAfter: 42,
          resetAt: Date.now() + 42_000,
        }),
      ),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/tts/synthesize", {
        method: "POST",
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );

    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toBe("42");
    expect(response.headers.get("x-ratelimit-limit")).toBe("300");
    expect(aiCalls).toBe(0);
  });

  it("records a bounded fallback and preserves legacy fail-open behavior when the limiter is unavailable", async () => {
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() => Response.json({ status: "ok" })),
      API_AI: service(() => Response.json({ status: "ok" })),
      RATE_LIMITS: rateLimits(() => {
        throw new Error("simulated Durable Object outage");
      }),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/tts/synthesize-workers-ai", {
        method: "POST",
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );

    expect(response.status).toBe(200);
    expect(JSON.parse(String(warning.mock.calls[0]?.[0]))).toMatchObject({
      event: "fallback",
      component: "rate_limit",
      from: "durable_object",
      to: "unlimited",
      reason: "dependency_unavailable",
      outcome: "degraded",
    });
    expect(String(warning.mock.calls[0]?.[0])).not.toContain("user-1");
    warning.mockRestore();
  });

  it("keeps every realtime contract on the realtime binding", async () => {
    const realtimePaths: string[] = [];
    const realtimeRequests: Request[] = [];
    let authCalls = 0;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        authCalls += 1;
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() =>
        Response.json({ error: "wrong owner" }, { status: 500 }),
      ),
      API_AI: service(() =>
        Response.json({ error: "wrong owner" }, { status: 500 }),
      ),
      REALTIME: service((request) => {
        realtimeRequests.push(request);
        realtimePaths.push(new URL(request.url).pathname);
        return Response.json({ status: "ok" });
      }),
    };
    for (const path of [
      "/v2/voice-message/transcribe-stream",
      "/v4/listen",
      "/v1/omni/relay",
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          headers: { authorization: "Bearer opaque-session" },
        }),
        env,
      );
      expect(response.status).toBe(200);
    }
    const webResponse = await edge.fetch(
      new Request("https://edge.test/v4/web/listen", {
        headers: { upgrade: "websocket" },
      }),
      env,
    );
    expect(webResponse.status).toBe(200);
    expect(realtimePaths).toEqual([
      "/v2/voice-message/transcribe-stream",
      "/v4/listen",
      "/v1/omni/relay",
      "/v4/web/listen",
    ]);
    expect(authCalls).toBe(3);
    const webRequest = realtimeRequests.at(-1);
    expect(webRequest?.headers.get("authorization")).toBeNull();
    expect(webRequest?.headers.get("x-omi-realtime-bootstrap")).toBeTruthy();
    expect(
      webRequest?.headers.get("x-omi-realtime-bootstrap-signature"),
    ).toBeTruthy();
  });

  it("requires a websocket upgrade before issuing a web realtime bootstrap", async () => {
    let realtimeCalls = 0;
    const response = await edge.fetch(
      new Request("https://edge.test/v4/web/listen"),
      {
        INTERNAL_ASSERTION_SECRET: "test-secret",
        REALTIME: service(() => {
          realtimeCalls += 1;
          return Response.json({ status: "unexpected" });
        }),
      } as never,
    );
    expect(response.status).toBe(426);
    expect(realtimeCalls).toBe(0);
  });

  it("does not send an unmigrated authenticated route to the partial API worker", async () => {
    let coreCalls = 0;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() => {
        coreCalls += 1;
        return Response.json({ status: "unexpected" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/users/unmigrated", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "route not migrated" });
    expect(coreCalls).toBe(0);
  });

  it("falls an unmigrated route back to legacy when configured", async () => {
    let legacyPath = "";
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      LEGACY_BACKEND_URL: "https://legacy.example.test",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() => Response.json({ status: "unexpected" })),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (request) => {
      legacyPath = new URL(request instanceof Request ? request.url : request)
        .pathname;
      return Response.json({ owner: "legacy" });
    };
    try {
      const response = await edge.fetch(
        new Request("https://edge.test/v1/users/unmigrated", {
          headers: { authorization: "Bearer opaque-session" },
        }),
        env,
      );
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({ owner: "legacy" });
      expect(legacyPath).toBe("/v1/users/unmigrated");
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("keeps destructive recording deletion on the legacy fallback", async () => {
    let legacyPath = "";
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      LEGACY_BACKEND_URL: "https://legacy.example.test",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() => Response.json({ status: "unexpected" })),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (request) => {
      legacyPath = new URL(request instanceof Request ? request.url : request)
        .pathname;
      return Response.json({ owner: "legacy" });
    };
    try {
      const response = await edge.fetch(
        new Request("https://edge.test/v1/users/store-recording-permission", {
          method: "DELETE",
          headers: { authorization: "Bearer opaque-session" },
        }),
        env,
      );
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({ owner: "legacy" });
      expect(legacyPath).toBe("/v1/users/store-recording-permission");
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
