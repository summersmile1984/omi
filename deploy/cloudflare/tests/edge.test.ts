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

  it("forwards the public root health payload to API Core", async () => {
    const requests: Request[] = [];
    const env = {
      API_CORE: service((request) => {
        requests.push(request);
        return Response.json({
          status: "ok",
          service: "api-core",
          version: "cf-02",
        });
      }),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/"),
      env as never,
    );
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      status: "ok",
      service: "api-core",
      version: "cf-02",
    });
    expect(requests).toHaveLength(1);
    expect(new URL(requests[0].url).pathname).toBe("/");
    expect(requests[0].headers.get("authorization")).toBeNull();
    expect(requests[0].headers.get("x-omi-auth-context")).toBeNull();
  });

  it("serves public legacy compatibility routes without dependencies", async () => {
    const v1Health = await edge.fetch(
      new Request("https://edge.test/v1/health"),
      {} as never,
    );
    expect(v1Health.status).toBe(200);
    expect(v1Health.headers.get("content-type")).toBe(
      "application/json; charset=UTF-8",
    );
    await expect(v1Health.json()).resolves.toEqual({ status: "ok" });

    const v1HealthHead = await edge.fetch(
      new Request("https://edge.test/v1/health", { method: "HEAD" }),
      {} as never,
    );
    expect(v1HealthHead.status).toBe(200);
    expect(v1HealthHead.headers.get("content-type")).toBe(
      "application/json; charset=UTF-8",
    );
    await expect(v1HealthHead.text()).resolves.toBe("");

    const appleAssociation = await edge.fetch(
      new Request(
        "https://edge.test/.well-known/apple-developer-domain-association.txt",
      ),
      {} as never,
    );
    expect(appleAssociation.status).toBe(200);
    expect(appleAssociation.headers.get("content-type")).toBe(
      "text/plain; charset=UTF-8",
    );
    await expect(appleAssociation.text()).resolves.toBe("");

    const openAiChallenge = await edge.fetch(
      new Request("https://edge.test/.well-known/openai-apps-challenge"),
      {} as never,
    );
    expect(openAiChallenge.status).toBe(200);
    expect(openAiChallenge.headers.get("content-type")).toBe(
      "text/plain; charset=UTF-8",
    );
    await expect(openAiChallenge.text()).resolves.toBe(
      "ZsVB_wpc4R35_tHloCZCokY6H2fBkKyBJrz-4MtXjYE",
    );
  });

  it("serves retired Agent VM tombstones without authentication or dependencies", async () => {
    const calls: string[] = [];
    const env = {
      AUTH: rawService((request) => {
        calls.push(`auth:${new URL(request.url).pathname}`);
        return Response.json({ uid: "unexpected" });
      }),
      API_CORE: rawService((request) => {
        calls.push(`core:${new URL(request.url).pathname}`);
        return Response.json({ error: "unexpected" }, { status: 500 });
      }),
      API_AI: rawService((request) => {
        calls.push(`ai:${new URL(request.url).pathname}`);
        return Response.json({ error: "unexpected" }, { status: 500 });
      }),
      REALTIME: rawService((request) => {
        calls.push(`realtime:${new URL(request.url).pathname}`);
        return Response.json({ error: "unexpected" }, { status: 500 });
      }),
      JOBS: rawService((request) => {
        calls.push(`jobs:${new URL(request.url).pathname}`);
        return Response.json({ error: "unexpected" }, { status: 500 });
      }),
    };

    const status = await edge.fetch(
      new Request("https://edge.test/v2/agent/status"),
      env,
    );
    expect(status.status).toBe(200);
    expect(status.headers.get("content-type")).toBe(
      "application/json; charset=UTF-8",
    );
    await expect(status.text()).resolves.toBe("null");

    for (const path of ["/v2/agent/provision", "/v2/agent/vm/stop-self"]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, { method: "POST" }),
        env,
      );
      expect(response.status).toBe(410);
      await expect(response.json()).resolves.toEqual({
        detail:
          "The cloud Agent VM has been retired and can no longer be provisioned.",
      });
    }

    expect(calls).toEqual([]);
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
      jobs: "/ready",
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

  it("keeps authenticated deletion reachable without consulting product cutover", async () => {
    let forwarded: Request | undefined;
    let productCutoverCalls = 0;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: rawService((request) => {
        expect(new URL(request.url).pathname).toBe("/internal/verify");
        return Response.json({
          uid: "deletion-user",
          authority: "better-auth",
        });
      }),
      API_CORE: rawService(() => {
        productCutoverCalls += 1;
        return Response.json({ error: "account_deleting" }, { status: 409 });
      }),
      JOBS: rawService((request) => {
        forwarded = request;
        return Response.json({
          status: "ok",
          message: "Account deletion started",
        });
      }),
    };
    const body = JSON.stringify({ reason: "privacy_concerns" });
    const response = await edge.fetch(
      new Request("https://edge.test/v1/users/delete-account", {
        method: "DELETE",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "application/json",
          "x-omi-auth-context": "attacker-context",
        },
        body,
      }),
      env as never,
    );

    expect(response.status).toBe(200);
    expect(productCutoverCalls).toBe(0);
    expect(forwarded?.method).toBe("DELETE");
    expect(new URL(forwarded?.url || "https://invalid.test").pathname).toBe(
      "/v1/users/delete-account",
    );
    expect(forwarded?.headers.get("authorization")).toBeNull();
    expect(
      decodeAuthContext(forwarded?.headers.get("x-omi-auth-context") ?? null),
    ).toMatchObject({ uid: "deletion-user", authority: "better-auth" });
    await expect(forwarded?.text()).resolves.toBe(body);

    const unauthenticated = await edge.fetch(
      new Request("https://edge.test/v1/users/delete-account", {
        method: "DELETE",
      }),
      env as never,
    );
    expect(unauthenticated.status).toBe(401);
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

  it("serves the root OAuth discovery alias from the Auth Worker", async () => {
    const methods: string[] = [];
    const paths: string[] = [];
    const env = {
      AUTH: rawService((request) => {
        methods.push(request.method);
        paths.push(new URL(request.url).pathname);
        return Response.json(
          {
            issuer: "https://web.test/api/better-auth",
            token_endpoint: "https://web.test/api/better-auth/oauth2/token",
          },
          { headers: { "cache-control": "no-store" } },
        );
      }),
    };

    const getResponse = await edge.fetch(
      new Request("https://edge.test/.well-known/oauth-authorization-server"),
      env as never,
    );
    expect(getResponse.status).toBe(200);
    expect(await getResponse.json()).toMatchObject({
      issuer: "https://web.test/api/better-auth",
    });

    const headResponse = await edge.fetch(
      new Request("https://edge.test/.well-known/oauth-authorization-server", {
        method: "HEAD",
      }),
      env as never,
    );
    expect(headResponse.status).toBe(200);
    expect(await headResponse.text()).toBe("");
    expect(methods).toEqual(["GET", "GET"]);
    expect(paths).toEqual([
      "/api/better-auth/.well-known/oauth-authorization-server",
      "/api/better-auth/.well-known/oauth-authorization-server",
    ]);
  });

  it("aliases legacy MCP OAuth authorize and token paths to Better Auth", async () => {
    const requests: Array<{ request: Request; body: string }> = [];
    const env = {
      AUTH: rawService(async (request) => {
        requests.push({ request, body: await request.text() });
        return Response.json({ ok: true });
      }),
    };

    const authorizeGet = await edge.fetch(
      new Request(
        "https://edge.test/authorize?client_id=client-1&redirect_uri=https%3A%2F%2Fclient.test%2Fcallback",
        {
          headers: {
            authorization: "Basic client-secret",
            cookie: "better-auth.session=opaque",
            "x-omi-auth-context": "untrusted",
          },
        },
      ),
      env as never,
    );
    const authorizePost = await edge.fetch(
      new Request("https://edge.test/authorize", {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: "client_id=client-1&selected=true",
      }),
      env as never,
    );
    const tokenPost = await edge.fetch(
      new Request("https://edge.test/token", {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: "grant_type=authorization_code&code=opaque",
      }),
      env as never,
    );

    expect([authorizeGet.status, authorizePost.status, tokenPost.status]).toEqual([
      200,
      200,
      200,
    ]);
    expect(requests.map(({ request }) => `${request.method} ${new URL(request.url).pathname}`)).toEqual([
      "GET /api/better-auth/oauth2/authorize",
      "POST /api/better-auth/oauth2/authorize",
      "POST /api/better-auth/oauth2/token",
    ]);
    expect(new URL(requests[0].request.url).searchParams.get("client_id")).toBe("client-1");
    expect(requests[0].request.headers.get("authorization")).toBe("Basic client-secret");
    expect(requests[0].request.headers.get("cookie")).toBe("better-auth.session=opaque");
    expect(requests[0].request.headers.get("x-omi-auth-context")).toBeNull();
    expect(requests[1].body).toBe("client_id=client-1&selected=true");
    expect(requests[2].body).toBe("grant_type=authorization_code&code=opaque");
  });

  it("keeps MCP grant management beside Better Auth and outside product cutover", async () => {
    const grantRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: rawService((request) => {
        const path = new URL(request.url).pathname;
        if (path === "/internal/verify") {
          return Response.json({ uid: "grant-user", authority: "better-auth" });
        }
        grantRequests.push(request);
        return request.method === "DELETE"
          ? new Response(null, { status: 204 })
          : Response.json({ grants: [{ id: "grant-1" }] });
      }),
      API_CORE: rawService(() => {
        throw new Error("MCP grants must not consult product cutover");
      }),
    };

    const list = await edge.fetch(
      new Request("https://edge.test/v1/mcp/oauth/grants", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );
    expect(list.status).toBe(200);
    expect(await list.json()).toEqual({ grants: [{ id: "grant-1" }] });

    const revoke = await edge.fetch(
      new Request("https://edge.test/v1/mcp/oauth/grants/grant-1", {
        method: "DELETE",
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );
    expect(revoke.status).toBe(204);
    expect(
      grantRequests.map((request) => new URL(request.url).pathname),
    ).toEqual(["/internal/mcp/grants", "/internal/mcp/grants/grant-1"]);
    expect(grantRequests.map((request) => request.method)).toEqual([
      "GET",
      "DELETE",
    ]);
    for (const request of grantRequests) {
      expect(
        decodeAuthContext(request.headers.get("x-omi-auth-context")),
      ).toMatchObject({
        uid: "grant-user",
        audience: "auth",
        method: request.method,
        path: new URL(request.url).pathname,
      });
      expect(request.headers.get("authorization")).toBeNull();
      expect(request.headers.get("cookie")).toBeNull();
    }
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
      "/v1/trends",
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
      "/v1/trends",
    ]);
  });

  it("routes public desktop preview reads through the core worker", async () => {
    const paths: string[] = [];
    const env = {
      API_CORE: service((request) => {
        paths.push(`${request.method} ${new URL(request.url).pathname}`);
        return new Response("<html>preview</html>", {
          headers: { "content-type": "text/html; charset=utf-8" },
        });
      }),
    };
    for (const path of [
      "/v2/desktop/previews/feature-demo",
      "/v2/desktop/previews/feature-demo/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`),
        env as never,
      );
      expect(response.status).toBe(200);
      expect(response.headers.get("content-type")).toContain("text/html");
    }
    expect(paths).toEqual([
      "GET /v2/desktop/previews/feature-demo",
      "GET /v2/desktop/previews/feature-demo/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ]);
  });

  it("routes preview delisting to the core worker without stripping the admin key", async () => {
    let forwarded: Request | undefined;
    const env = {
      API_CORE: service((request) => {
        forwarded = request;
        return Response.json({ success: true, deleted: true });
      }),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v2/desktop/previews/feature-demo", {
        method: "DELETE",
        headers: {
          "content-type": "application/json",
          "secret-key": "preview-secret",
        },
        body: JSON.stringify({ expected_generation: 1 }),
      }),
      env as never,
    );
    expect(response.status).toBe(200);
    expect(forwarded?.method).toBe("DELETE");
    expect(forwarded?.headers.get("secret-key")).toBe("preview-secret");
    await expect(forwarded?.json()).resolves.toEqual({
      expected_generation: 1,
    });
  });

  it("routes preview publication to the core worker without stripping the admin key", async () => {
    let forwarded: Request | undefined;
    const env = {
      API_CORE: service((request) => {
        forwarded = request;
        return Response.json({ success: true }, { status: 201 });
      }),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v2/desktop/previews/publish", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "secret-key": "preview-secret",
        },
        body: JSON.stringify({
          slug: "feature-demo",
          source_sha: "a".repeat(40),
        }),
      }),
      env as never,
    );
    expect(response.status).toBe(201);
    expect(forwarded?.method).toBe("POST");
    expect(forwarded?.headers.get("secret-key")).toBe("preview-secret");
    await expect(forwarded?.json()).resolves.toEqual({
      slug: "feature-demo",
      source_sha: "a".repeat(40),
    });
  });

  it("routes admin Persona reads and deletes to the core worker", async () => {
    const paths: string[] = [];
    const env = {
      API_CORE: service((request) => {
        paths.push(`${request.method} ${new URL(request.url).pathname}`);
        return Response.json({ status: "ok" });
      }),
    };
    for (const [method, path] of [
      ["GET", "/v1/personas/alice"],
      ["DELETE", "/v1/personas/persona-id"],
    ] as const) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method,
          headers: { "secret-key": "admin-secret" },
        }),
        env as never,
      );
      expect(response.status).toBe(200);
    }
    expect(paths).toEqual([
      "GET /v1/personas/alice",
      "DELETE /v1/personas/persona-id",
    ]);
  });

  it("routes authenticated app plan reads through the core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "plans-user", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json([
          { title: "Monthly Recurring", id: "monthly_recurring" },
        ]);
      }),
    };
    const unauthenticated = await edge.fetch(
      new Request("https://edge.test/v1/app/plans"),
      env as never,
    );
    expect(unauthenticated.status).toBe(401);

    const response = await edge.fetch(
      new Request("https://edge.test/v1/app/plans", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual([
      { title: "Monthly Recurring", id: "monthly_recurring" },
    ]);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe("/v1/app/plans");
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes authenticated app prompt generation through the AI worker", async () => {
    const aiRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({
            uid: "prompt-user",
            authority: "better-auth",
          });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() =>
        Response.json({
          state: "new",
          client_action: "none",
          product_traffic_allowed: true,
          migration: { destination_backend_bound: true },
        }),
      ),
      API_AI: service((request) => {
        aiRequests.push(request);
        return Response.json({
          prompts: ["one", "two", "three", "four", "five"],
        });
      }),
      RATE_LIMITS: rateLimits(() => allowRateLimit()),
    };

    const unauthenticated = await edge.fetch(
      new Request("https://edge.test/v1/app/generate-prompts"),
      env as never,
    );
    expect(unauthenticated.status).toBe(401);

    const response = await edge.fetch(
      new Request("https://edge.test/v1/app/generate-prompts", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      prompts: ["one", "two", "three", "four", "five"],
    });
    expect(aiRequests).toHaveLength(1);
    expect(new URL(aiRequests[0].url).pathname).toBe(
      "/v1/app/generate-prompts",
    );
    expect(aiRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();

    for (const path of [
      "/v1/app/generate",
      "/v1/app/generate-description",
      "/v1/app/generate-description-emoji",
    ]) {
      const generated = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method: "POST",
          headers: {
            authorization: "Bearer opaque-session",
            "content-type": "application/json",
          },
          body: JSON.stringify({
            name: "Focus",
            description: "Tracks focus",
            prompt: "Tracks focus",
          }),
        }),
        env as never,
      );
      expect(generated.status).toBe(200);
    }
    expect(aiRequests.map((request) => new URL(request.url).pathname)).toEqual([
      "/v1/app/generate-prompts",
      "/v1/app/generate",
      "/v1/app/generate-description",
      "/v1/app/generate-description-emoji",
    ]);
  });

  it("keeps native payment terminal pages public", async () => {
    const coreRequests: Request[] = [];
    const env = {
      API_CORE: service((request) => {
        coreRequests.push(request);
        return new Response("<html>ok</html>", {
          headers: { "content-type": "text/html; charset=utf-8" },
        });
      }),
    };

    for (const path of [
      "/v1/payments/success?session_id=cs_test_123",
      "/v1/payments/cancel",
      "/v1/payments/portal-return",
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          headers: { authorization: "Bearer untrusted" },
        }),
        env as never,
      );
      expect(response.status).toBe(200);
      expect(response.headers.get("content-type")).toContain("text/html");
    }

    expect(
      coreRequests.map(
        (request) =>
          `${new URL(request.url).pathname}${new URL(request.url).search}`,
      ),
    ).toEqual([
      "/v1/payments/success?session_id=cs_test_123",
      "/v1/payments/cancel",
      "/v1/payments/portal-return",
    ]);
    for (const request of coreRequests) {
      expect(request.headers.get("authorization")).toBeNull();
      expect(request.headers.get("x-omi-auth-context")).toBeNull();
    }
  });

  it("preserves the raw public Stripe webhook and signs payment mutations for Jobs", async () => {
    const jobRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() => Response.json({ status: "ok" })),
      JOBS: service(async (request) => {
        jobRequests.push(request);
        return Response.json({ status: "ok" });
      }),
    };

    const rawWebhook = '{"id":"evt_raw","data":{"object":{"id":"sub_raw"}}}';
    const webhook = await edge.fetch(
      new Request("https://edge.test/v1/stripe/webhook", {
        method: "POST",
        headers: {
          authorization: "Bearer untrusted-client-header",
          "content-type": "application/json",
          "stripe-signature": "t=1,v1=abc",
        },
        body: rawWebhook,
      }),
      env as never,
    );
    expect(webhook.status).toBe(200);
    expect(jobRequests[0].headers.get("authorization")).toBeNull();
    expect(jobRequests[0].headers.get("stripe-signature")).toBe("t=1,v1=abc");
    expect(jobRequests[0].headers.get("x-omi-auth-context")).toBeNull();
    await expect(jobRequests[0].text()).resolves.toBe(rawWebhook);

    const rawConnectWebhook =
      '{"id":"evt_connect_raw","data":{"object":{"id":"acct_raw"}}}';
    const connectWebhook = await edge.fetch(
      new Request("https://edge.test/v1/stripe/connect/webhook", {
        method: "POST",
        headers: {
          authorization: "Bearer untrusted-client-header",
          "content-type": "application/json",
          "stripe-signature": "t=2,v1=def",
        },
        body: rawConnectWebhook,
      }),
      env as never,
    );
    expect(connectWebhook.status).toBe(200);
    expect(jobRequests[1].headers.get("authorization")).toBeNull();
    expect(jobRequests[1].headers.get("stripe-signature")).toBe("t=2,v1=def");
    expect(jobRequests[1].headers.get("x-omi-auth-context")).toBeNull();
    await expect(jobRequests[1].text()).resolves.toBe(rawConnectWebhook);

    for (const path of [
      "/v1/stripe/supported-countries",
      "/v1/stripe/refresh/acct_public123?token=signed",
      "/v1/stripe/return/acct_public123",
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          headers: { authorization: "Bearer untrusted-client-header" },
        }),
        env as never,
      );
      expect(response.status).toBe(200);
      const forwarded = jobRequests.at(-1);
      expect(forwarded?.headers.get("authorization")).toBeNull();
      expect(forwarded?.headers.get("x-omi-auth-context")).toBeNull();
    }

    const authenticatedStart = jobRequests.length;
    for (const [method, path] of [
      ["POST", "/v1/payments/checkout-session"],
      ["POST", "/v1/payments/customer-portal"],
      ["POST", "/v1/payments/upgrade-subscription"],
      ["DELETE", "/v1/payments/subscription"],
      ["POST", "/v1/stripe/connect-accounts"],
      ["GET", "/v1/stripe/onboarded"],
      ["POST", "/v1/stripe/refresh/acct_owned123"],
      ["POST", "/v1/paypal/payment-details"],
      ["GET", "/v1/paypal/payment-details"],
      ["GET", "/v1/payment-methods/status"],
      ["POST", "/v1/payment-methods/default"],
    ]) {
      const init: RequestInit = {
        method,
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "application/json",
          "idempotency-key": "billing-attempt-1",
        },
      };
      if (method !== "GET") init.body = "{}";
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, init),
        env as never,
      );
      expect(response.status).toBe(200);
    }
    const authenticatedRequests = jobRequests.slice(authenticatedStart);
    expect(
      authenticatedRequests.map((request) => new URL(request.url).pathname),
    ).toEqual([
      "/v1/payments/checkout-session",
      "/v1/payments/customer-portal",
      "/v1/payments/upgrade-subscription",
      "/v1/payments/subscription",
      "/v1/stripe/connect-accounts",
      "/v1/stripe/onboarded",
      "/v1/stripe/refresh/acct_owned123",
      "/v1/paypal/payment-details",
      "/v1/paypal/payment-details",
      "/v1/payment-methods/status",
      "/v1/payment-methods/default",
    ]);
    for (const request of authenticatedRequests) {
      expect(
        decodeAuthContext(request.headers.get("x-omi-auth-context")),
      ).toMatchObject({
        uid: "user-1",
        audience: "jobs",
      });
      expect(request.headers.get("authorization")).toBeNull();
      expect(request.headers.get("idempotency-key")).toBe("billing-attempt-1");
    }
  });

  it("keeps the X OAuth callback public and signs the authenticated connector surface for Jobs", async () => {
    const jobRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() => Response.json({ status: "ok" })),
      JOBS: service((request) => {
        jobRequests.push(request);
        return Response.json({ status: "ok" });
      }),
    };

    const callback = await edge.fetch(
      new Request(
        "https://edge.test/v1/x/oauth/callback?code=provider-code&state=opaque-state",
        {
          headers: {
            authorization: "Bearer untrusted-client-header",
            "x-omi-auth-context": "untrusted-context",
          },
        },
      ),
      env as never,
    );
    expect(callback.status).toBe(200);
    expect(jobRequests[0].headers.get("authorization")).toBeNull();
    expect(jobRequests[0].headers.get("x-omi-auth-context")).toBeNull();

    for (const [method, path] of [
      ["GET", "/v1/x/oauth-url"],
      ["GET", "/v1/x/connection-status"],
      ["GET", "/v1/x/posts?kind=bookmark&limit=10"],
      ["POST", "/v1/x/sync"],
      ["POST", "/v1/x/disconnect"],
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method,
          headers: {
            authorization: "Bearer opaque-session",
            "x-omi-auth-context": "untrusted-context",
          },
        }),
        env as never,
      );
      expect(response.status, `${method} ${path}`).toBe(200);
    }

    expect(
      jobRequests.slice(1).map((request) => {
        const url = new URL(request.url);
        return `${request.method} ${url.pathname}${url.search}`;
      }),
    ).toEqual([
      "GET /v1/x/oauth-url",
      "GET /v1/x/connection-status",
      "GET /v1/x/posts?kind=bookmark&limit=10",
      "POST /v1/x/sync",
      "POST /v1/x/disconnect",
    ]);
    for (const request of jobRequests.slice(1)) {
      expect(
        decodeAuthContext(request.headers.get("x-omi-auth-context")),
      ).toMatchObject({ uid: "user-1", audience: "jobs" });
      expect(request.headers.get("authorization")).toBeNull();
    }
  });

  it("keeps task OAuth callbacks public and signs all task integration API routes for Jobs", async () => {
    const jobRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "task-user", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() => Response.json({ status: "ok" })),
      JOBS: service((request) => {
        jobRequests.push(request);
        return Response.json({ status: "ok" });
      }),
    };

    for (const path of [
      "/v2/integrations/todoist/callback?code=c&state=s",
      "/v2/integrations/asana/callback?code=c&state=s",
      "/v2/integrations/google-tasks/callback?code=c&state=s",
      "/v2/integrations/clickup/callback?code=c&state=s",
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          headers: {
            authorization: "Bearer untrusted-client-header",
            "x-omi-auth-context": "untrusted-context",
          },
        }),
        env as never,
      );
      expect(response.status).toBe(200);
    }
    for (const request of jobRequests) {
      expect(request.headers.get("authorization")).toBeNull();
      expect(request.headers.get("x-omi-auth-context")).toBeNull();
    }

    const authenticatedStart = jobRequests.length;
    for (const [method, path] of [
      ["GET", "/v1/task-integrations"],
      ["GET", "/v1/task-integrations/default"],
      ["PUT", "/v1/task-integrations/default"],
      ["GET", "/v1/task-integrations/asana/workspaces"],
      ["GET", "/v1/task-integrations/asana/projects/workspace-1"],
      ["GET", "/v1/task-integrations/clickup/teams"],
      ["GET", "/v1/task-integrations/clickup/spaces/team-1"],
      ["GET", "/v1/task-integrations/clickup/lists/space-1"],
      ["GET", "/v1/task-integrations/todoist/oauth-url"],
      ["POST", "/v1/task-integrations/todoist/tasks"],
      ["PUT", "/v1/task-integrations/todoist"],
      ["DELETE", "/v1/task-integrations/todoist"],
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method,
          headers: {
            authorization: "Bearer opaque-session",
            "x-omi-auth-context": "untrusted-context",
          },
        }),
        env as never,
      );
      expect(response.status).toBe(200);
    }
    const authenticatedRequests = jobRequests.slice(authenticatedStart);
    expect(authenticatedRequests).toHaveLength(12);
    for (const request of authenticatedRequests) {
      expect(
        decodeAuthContext(request.headers.get("x-omi-auth-context")),
      ).toMatchObject({ uid: "task-user", audience: "jobs" });
      expect(request.headers.get("authorization")).toBeNull();
    }
  });

  it("keeps the Google Calendar callback public and signs Calendar routes for Jobs", async () => {
    const jobRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({
            uid: "calendar-user",
            authority: "better-auth",
          });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() => Response.json({ status: "ok" })),
      JOBS: service((request) => {
        jobRequests.push(request);
        return Response.json({ status: "ok" });
      }),
    };

    const callback = await edge.fetch(
      new Request(
        "https://edge.test/v2/integrations/google-calendar/callback?code=c&state=s",
        {
          headers: {
            authorization: "Bearer untrusted-client-header",
            "x-omi-auth-context": "untrusted-context",
          },
        },
      ),
      env as never,
    );
    expect(callback.status).toBe(200);
    expect(jobRequests[0].headers.get("authorization")).toBeNull();
    expect(jobRequests[0].headers.get("x-omi-auth-context")).toBeNull();

    const legacyCallback = await edge.fetch(
      new Request(
        "https://edge.test/v2/integrations/google_calendar/callback?code=c&state=s",
        {
          headers: {
            authorization: "Bearer untrusted-client-header",
            "x-omi-auth-context": "untrusted-context",
          },
        },
      ),
      env as never,
    );
    expect(legacyCallback.status).toBe(200);
    expect(jobRequests[1].headers.get("authorization")).toBeNull();
    expect(jobRequests[1].headers.get("x-omi-auth-context")).toBeNull();

    const genericCallback = await edge.fetch(
      new Request(
        "https://edge.test/v2/integrations/gmail/callback?code=c&state=s",
        {
          headers: {
            authorization: "Bearer untrusted-client-header",
            "x-omi-auth-context": "untrusted-context",
          },
        },
      ),
      env as never,
    );
    expect(genericCallback.status).toBe(200);
    expect(jobRequests[2].headers.get("authorization")).toBeNull();
    expect(jobRequests[2].headers.get("x-omi-auth-context")).toBeNull();

    for (const [method, path] of [
      ["GET", "/v1/integrations/google_calendar"],
      ["PUT", "/v1/integrations/google_calendar"],
      ["PUT", "/v1/integrations/google-calendar"],
      ["DELETE", "/v1/integrations/google_calendar"],
      ["GET", "/v1/integrations/google_calendar/oauth-url"],
      ["GET", "/v1/calendar/google/events?max_results=20"],
      ["POST", "/v1/conversations/conversation-1/calendar-event"],
      ["POST", "/v1/conversations/conversation-1/calendar-event/auto-link"],
      ["POST", "/v1/tools/calendar-events"],
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method,
          headers: {
            authorization: "Bearer opaque-session",
            "x-omi-auth-context": "untrusted-context",
          },
        }),
        env as never,
      );
      expect(response.status, `${method} ${path}`).toBe(200);
    }

    expect(jobRequests).toHaveLength(12);
    for (const request of jobRequests.slice(3)) {
      expect(
        decodeAuthContext(request.headers.get("x-omi-auth-context")),
      ).toMatchObject({ uid: "calendar-user", audience: "jobs" });
      expect(request.headers.get("authorization")).toBeNull();
    }
  });

  it("routes generic integration status reads to Core while preserving Calendar ownership in Jobs", async () => {
    const coreRequests: Request[] = [];
    const jobRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({
            uid: "integration-user",
            authority: "better-auth",
          });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ connected: false, app_key: "todoist" });
      }),
      JOBS: service((request) => {
        jobRequests.push(request);
        return Response.json({ connected: false, app_key: "google_calendar" });
      }),
    };
    const headers = { authorization: "Bearer opaque-session" };
    const generic = await edge.fetch(
      new Request("https://edge.test/v1/integrations/todoist", { headers }),
      env,
    );
    const calendar = await edge.fetch(
      new Request("https://edge.test/v1/integrations/google_calendar", {
        headers,
      }),
      env,
    );
    const gmailOAuth = await edge.fetch(
      new Request("https://edge.test/v1/integrations/gmail/oauth-url", {
        headers,
      }),
      env,
    );

    expect(generic.status).toBe(200);
    expect(calendar.status).toBe(200);
    expect(gmailOAuth.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(jobRequests).toHaveLength(2);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/integrations/todoist",
    );
    expect(new URL(jobRequests[0].url).pathname).toBe(
      "/v1/integrations/google_calendar",
    );
    expect(new URL(jobRequests[1].url).pathname).toBe(
      "/v1/integrations/gmail/oauth-url",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
    expect(jobRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
    expect(jobRequests[1].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes the team notification contract to Jobs without forwarding user auth", async () => {
    const jobsRequests: Request[] = [];
    const env = {
      JOBS: service((request) => {
        jobsRequests.push(request);
        return Response.json({ status: "Ok" });
      }),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/notification", {
        method: "POST",
        headers: {
          "secret-key": "admin-secret",
          authorization: "Bearer user-token",
          cookie: "session=untrusted",
          "x-omi-auth-context": "untrusted-context",
        },
        body: JSON.stringify({ uid: "user-1", title: "Title", body: "Body" }),
      }),
      env as never,
    );
    expect(response.status).toBe(200);
    expect(jobsRequests).toHaveLength(1);
    expect(new URL(jobsRequests[0].url).pathname).toBe("/v1/notification");
    expect(jobsRequests[0].headers.get("secret-key")).toBe("admin-secret");
    expect(jobsRequests[0].headers.get("authorization")).toBeNull();
    expect(jobsRequests[0].headers.get("cookie")).toBeNull();
    expect(jobsRequests[0].headers.get("x-omi-auth-context")).toBeNull();
  });

  it("routes retired compatibility endpoints to API Core", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({
            uid: "compat-user",
            authority: "better-auth",
          });
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

    for (const path of [
      "/v1/staged-tasks/migrate",
      "/v1/staged-tasks/migrate-conversation-items?limit=50&cursor=page-1",
      "/v1/action-items/restore-legacy-conversation-items",
      "/v1/import/limitless/conversations",
      "/v1/chat-first/blocks/validate",
      "/v1/chat/deferrals",
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method: path.startsWith("/v1/import/") ? "DELETE" : "POST",
          headers: { authorization: "Bearer opaque-session" },
        }),
        env,
      );
      expect(response.status, path).toBe(200);
    }

    expect(coreRequests).toHaveLength(6);
    expect(
      coreRequests.map((request) => new URL(request.url).pathname),
    ).toEqual([
      "/v1/staged-tasks/migrate",
      "/v1/staged-tasks/migrate-conversation-items",
      "/v1/action-items/restore-legacy-conversation-items",
      "/v1/import/limitless/conversations",
      "/v1/chat-first/blocks/validate",
      "/v1/chat/deferrals",
    ]);
    for (const request of coreRequests) {
      expect(request.headers.get("x-omi-auth-context")).toBeTruthy();
      expect(request.headers.get("authorization")).toBeNull();
    }
  });

  it("keeps share previews public and signs the Better Auth display name for share creation", async () => {
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

    for (const path of [
      "/v1/action-items/shared/public-token",
      "/v2/messages/shared/public-token",
      "/v1/conversations/shared-conversation/shared",
      "/v1/daily-summaries/shared-summary/shared",
    ]) {
      const preview = await edge.fetch(
        new Request(`https://edge.test${path}`),
        env as never,
      );
      expect(preview.status).toBe(200);
    }
    for (const path of ["/v1/action-items/share", "/v2/messages/share"]) {
      const share = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method: "POST",
          headers: { authorization: "Bearer opaque-session" },
        }),
        env as never,
      );
      expect(share.status).toBe(200);
    }

    expect(
      coreRequests.map((request) => new URL(request.url).pathname),
    ).toEqual([
      "/v1/action-items/shared/public-token",
      "/v2/messages/shared/public-token",
      "/v1/conversations/shared-conversation/shared",
      "/v1/daily-summaries/shared-summary/shared",
      "/v1/action-items/share",
      "/v2/messages/share",
    ]);
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeNull();
    expect(coreRequests[1].headers.get("x-omi-auth-context")).toBeNull();
    expect(coreRequests[2].headers.get("x-omi-auth-context")).toBeNull();
    expect(coreRequests[3].headers.get("x-omi-auth-context")).toBeNull();
    expect(
      decodeAuthContext(coreRequests[4].headers.get("x-omi-auth-context")),
    ).toMatchObject({ uid: "user-1", displayName: "Alice" });
    expect(
      decodeAuthContext(coreRequests[5].headers.get("x-omi-auth-context")),
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
    const catalogUnauthenticated = await edge.fetch(
      new Request("https://edge.test/v1/apps"),
      env as never,
    );
    const catalog = await edge.fetch(
      new Request("https://edge.test/v1/apps", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );
    const testerCheck = await edge.fetch(
      new Request("https://edge.test/v1/apps/tester/check", {
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
    expect(catalogUnauthenticated.status).toBe(401);
    expect(catalog.status).toBe(200);
    expect(testerCheck.status).toBe(200);
    expect(detailUnauthenticated.status).toBe(401);
    expect(detail.status).toBe(200);
    expect(corePaths).toEqual([
      "/v1/approved-apps",
      "/v1/apps/popular",
      "/v1/apps",
      "/v1/apps/tester/check",
      "/v1/apps/summary-app",
    ]);
  });

  it("preserves the independent app admin key while stripping caller auth", async () => {
    const jobsRequests: Request[] = [];
    const env = {
      JOBS: service((request) => {
        jobsRequests.push(request);
        return Response.json({ status: "ok" });
      }),
    };
    for (const [method, path] of [
      ["GET", "/v1/apps/public/unapproved"],
      ["POST", "/v1/apps/review-app/approve?uid=creator-user"],
      ["GET", "/v1/summary-app-ids"],
      ["POST", "/v1/summary-app-ids/summary-app"],
      ["DELETE", "/v1/summary-app-ids/summary-app"],
    ] as const) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method,
          headers: {
            authorization: "Bearer attacker-token",
            cookie: "better-auth.session=attacker",
            "secret-key": "apps-admin-secret",
            "x-omi-auth-context": "attacker-context",
          },
        }),
        env as never,
      );
      expect(response.status).toBe(200);
    }
    expect(
      jobsRequests.map(
        (request) => `${request.method} ${new URL(request.url).pathname}`,
      ),
    ).toEqual([
      "GET /v1/apps/public/unapproved",
      "POST /v1/apps/review-app/approve",
      "GET /v1/summary-app-ids",
      "POST /v1/summary-app-ids/summary-app",
      "DELETE /v1/summary-app-ids/summary-app",
    ]);
    for (const request of jobsRequests) {
      expect(request.headers.get("secret-key")).toBe("apps-admin-secret");
      expect(request.headers.get("authorization")).toBeNull();
      expect(request.headers.get("cookie")).toBeNull();
      expect(request.headers.get("x-omi-auth-context")).toBeNull();
    }
  });

  it("serves immutable app logos publicly and signs app create/update for Jobs", async () => {
    const jobsRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({
            uid: "creator-user",
            authority: "better-auth",
          });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service(() => Response.json({ status: "ok" })),
      JOBS: service((request) => {
        jobsRequests.push(request);
        return Response.json({ status: "ok" });
      }),
    };
    const logo = await edge.fetch(
      new Request(
        "https://edge.test/v1/apps/app-1/logo/00000000-0000-4000-8000-000000000000",
      ),
      env as never,
    );
    const thumbnail = await edge.fetch(
      new Request(
        "https://edge.test/v1/app/thumbnails/01ARZ3NDEKTSV4RRFFQ69G5FAV.jpg",
      ),
      env as never,
    );
    const unauthenticated = await edge.fetch(
      new Request("https://edge.test/v1/apps", { method: "POST" }),
      env as never,
    );
    for (const [method, route] of [
      ["POST", "/v1/apps"],
      ["PATCH", "/v1/apps/app-1"],
    ] as const) {
      const response = await edge.fetch(
        new Request(`https://edge.test${route}`, {
          method,
          headers: { authorization: "Bearer opaque-session" },
        }),
        env as never,
      );
      expect(response.status).toBe(200);
    }

    expect(logo.status).toBe(200);
    expect(thumbnail.status).toBe(200);
    expect(unauthenticated.status).toBe(401);
    expect(
      jobsRequests.map(
        (request) => `${request.method} ${new URL(request.url).pathname}`,
      ),
    ).toEqual([
      "GET /v1/apps/app-1/logo/00000000-0000-4000-8000-000000000000",
      "GET /v1/app/thumbnails/01ARZ3NDEKTSV4RRFFQ69G5FAV.jpg",
      "POST /v1/apps",
      "PATCH /v1/apps/app-1",
    ]);
    for (const request of jobsRequests.slice(0, 2)) {
      expect(request.headers.get("x-omi-auth-context")).toBeNull();
    }
    for (const request of jobsRequests.slice(2)) {
      expect(
        decodeAuthContext(request.headers.get("x-omi-auth-context")),
      ).toMatchObject({ uid: "creator-user", audience: "jobs" });
    }
  });

  it("keeps app review lists public and signs every review mutation", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "reviewer-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ status: "ok" });
      }),
    };

    const publicList = await edge.fetch(
      new Request("https://edge.test/v1/apps/app-1/reviews"),
      env as never,
    );
    for (const [method, path] of [
      ["POST", "/v1/apps/review?app_id=app-1"],
      ["PATCH", "/v1/apps/app-1/review"],
      ["PATCH", "/v1/apps/app-1/review/reply"],
    ] as const) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method,
          headers: { authorization: "Bearer opaque-session" },
        }),
        env as never,
      );
      expect(response.status).toBe(200);
    }

    expect(publicList.status).toBe(200);
    expect(
      coreRequests.map((request) => new URL(request.url).pathname),
    ).toEqual([
      "/v1/apps/app-1/reviews",
      "/v1/apps/review",
      "/v1/apps/app-1/review",
      "/v1/apps/app-1/review/reply",
    ]);
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeNull();
    for (const request of coreRequests.slice(1)) {
      expect(
        decodeAuthContext(request.headers.get("x-omi-auth-context")),
      ).toMatchObject({ uid: "reviewer-1" });
    }
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
    expect(
      (
        await edge.fetch(
          new Request(
            "https://edge.test/v1/users/preferences/app?app_id=free-app",
            {
              method: "PUT",
              headers: auth,
            },
          ),
          env as never,
        )
      ).status,
    ).toBe(200);
    expect(corePaths).toEqual([
      "GET /v1/apps/enabled",
      "POST /v1/apps/enable",
      "POST /v1/apps/disable",
      "PUT /v1/users/preferences/app",
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

  it("routes uid-scoped feedback reads and writes to the core worker", async () => {
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
    for (const [method, path] of [
      ["GET", "/v1/users/analytics/memory_summary?memory_id=memory-1"],
      ["POST", "/v1/users/analytics/memory_summary?memory_id=memory-1&value=1"],
      [
        "POST",
        "/v1/users/analytics/chat_message?message_id=message-1&value=-1",
      ],
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method,
          headers: { authorization: "Bearer opaque-session" },
        }),
        env,
      );
      expect(response.status).toBe(200);
    }
    expect(coreRequests.map(({ method }) => method)).toEqual([
      "GET",
      "POST",
      "POST",
    ]);
    expect(
      coreRequests.map((request) => new URL(request.url).pathname),
    ).toEqual([
      "/v1/users/analytics/memory_summary",
      "/v1/users/analytics/memory_summary",
      "/v1/users/analytics/chat_message",
    ]);
    expect(
      coreRequests.every((request) =>
        Boolean(request.headers.get("x-omi-auth-context")),
      ),
    ).toBe(true);
  });

  it("routes chat sessions and message-management APIs to the core worker", async () => {
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
    const routes = [
      ["GET", "/v2/messages"],
      ["DELETE", "/v2/messages"],
      ["DELETE", "/v1/messages"],
      ["POST", "/v1/messages/message-1/report"],
      ["POST", "/v2/messages/message-1/report"],
      ["PATCH", "/v2/messages/message-1/rating"],
      ["GET", "/v1/users/stats/chat-messages"],
      ["POST", "/v2/chat-sessions"],
      ["GET", "/v2/chat-sessions"],
      ["GET", "/v2/chat-sessions/session-1"],
      ["PATCH", "/v2/chat-sessions/session-1"],
      ["DELETE", "/v2/chat-sessions/session-1"],
      ["POST", "/v2/desktop/messages"],
      ["GET", "/v2/desktop/messages"],
      ["GET", "/v2/desktop/messages/reconcile"],
      ["DELETE", "/v2/desktop/messages"],
      ["PATCH", "/v2/desktop/messages/message-1/rating"],
    ] as const;
    for (const [method, path] of routes) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method,
          headers: { authorization: "Bearer opaque-session" },
        }),
        env,
      );
      expect(response.status).toBe(200);
    }
    expect(coreRequests.map((request) => request.method)).toEqual(
      routes.map(([method]) => method),
    );
    expect(
      coreRequests.map((request) => new URL(request.url).pathname),
    ).toEqual(routes.map(([, path]) => path));
    expect(
      coreRequests.every((request) =>
        Boolean(request.headers.get("x-omi-auth-context")),
      ),
    ).toBe(true);
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
    for (const path of [
      "/v1/cf/conversations?limit=10",
      "/v1/conversations/conversation-1/suggested-apps",
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          headers: { authorization: "Bearer opaque-session" },
        }),
        env,
      );
      expect(response.status).toBe(200);
    }
    expect(coreRequests).toHaveLength(2);
    expect(
      coreRequests.map((request) => new URL(request.url).pathname),
    ).toEqual([
      "/v1/cf/conversations",
      "/v1/conversations/conversation-1/suggested-apps",
    ]);
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes pre-transcribed creation, search, and deletion to the authenticated core worker", async () => {
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
          new Request("https://edge.test/v1/conversations/from-segments", {
            method: "POST",
            headers: {
              ...auth,
              "content-type": "application/json",
              "x-app-platform": "macos",
              "x-device-id-hash": "a1b2c3d4",
            },
            body: JSON.stringify({
              transcript_segments: [
                { text: "Worker native", start: 0, end: 1 },
              ],
            }),
          }),
          env,
        )
      ).status,
    ).toBe(200);
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
      "POST /v1/conversations/from-segments",
      "POST /v1/conversations/search",
      "DELETE /v1/conversations/conversation-1",
    ]);
    expect(
      coreRequests.every((request) =>
        Boolean(request.headers.get("x-omi-auth-context")),
      ),
    ).toBe(true);
    expect(coreRequests[0].headers.get("x-app-platform")).toBe("macos");
    expect(coreRequests[0].headers.get("x-device-id-hash")).toBe("a1b2c3d4");
    await expect(coreRequests[0].json()).resolves.toEqual({
      transcript_segments: [{ text: "Worker native", start: 0, end: 1 }],
    });
    expect(rateLimitNames).toEqual([
      "conversations:from-segments:user-1",
      "conversations:search:user-1",
    ]);
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

  it("routes first-party retrieval tools and action search to the authenticated core worker", async () => {
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
      API_AI: service(() => Response.json({ status: "wrong owner" })),
      REALTIME: service(() => Response.json({ status: "wrong owner" })),
      RATE_LIMITS: rateLimits(allowRateLimit, rateLimitNames),
    };
    const requests: Array<[string, string, string?]> = [
      ["GET", "/v1/action-items/search?query=worker"],
      ["GET", "/v1/tools/conversations?limit=1"],
      ["POST", "/v1/tools/conversations/search", '{"query":"worker"}'],
      ["POST", "/v1/tools/conversations/search-chunks", '{"query":"worker"}'],
      ["GET", "/v1/tools/memories?limit=1"],
      ["POST", "/v1/tools/memories/search", '{"query":"worker"}'],
      ["GET", "/v1/tools/action-items?limit=1"],
      ["POST", "/v1/tools/action-items", '{"description":"ship"}'],
      ["PATCH", "/v1/tools/action-items/action-1", '{"completed":true}'],
    ];
    for (const [method, path, body] of requests) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method,
          headers: {
            authorization: "Bearer opaque-session",
            ...(body ? { "content-type": "application/json" } : {}),
          },
          body,
        }),
        env,
      );
      expect(response.status).toBe(200);
    }

    expect(
      coreRequests.map(
        (request) => `${request.method} ${new URL(request.url).pathname}`,
      ),
    ).toEqual(
      requests.map(([method, path]) => `${method} ${path.split("?")[0]}`),
    );
    expect(
      coreRequests.every((request) =>
        Boolean(request.headers.get("x-omi-auth-context")),
      ),
    ).toBe(true);
    await expect(coreRequests[2].json()).resolves.toEqual({ query: "worker" });
    await expect(coreRequests[8].json()).resolves.toEqual({ completed: true });
    expect(rateLimitNames).toEqual([
      "tools:search:user-1",
      "tools:search:user-1",
      "tools:search:user-1",
      "tools:mutate:user-1",
      "tools:mutate:user-1",
    ]);
  });

  it("routes Crisp unread reads to the authenticated core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "crisp-user", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return Response.json({ unread_count: 0, messages: [] });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };

    const response = await edge.fetch(
      new Request("https://edge.test/v1/crisp/unread?since=0", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe("/v1/crisp/unread");
    expect(new URL(coreRequests[0].url).search).toBe("?since=0");
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
      ["POST", "/v3/memories/batch"],
      ["PATCH", "/v3/memories/memory-1?value=edited"],
      ["PATCH", "/v3/memories/memory-1/visibility?value=public"],
      ["PATCH", "/v3/memories/memory-1/read"],
      ["PATCH", "/v3/memories/memory-1/baseline?value=true"],
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
      "POST /v3/memories/batch",
      "PATCH /v3/memories/memory-1",
      "PATCH /v3/memories/memory-1/visibility",
      "PATCH /v3/memories/memory-1/read",
      "PATCH /v3/memories/memory-1/baseline",
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
      "memories:batch:user-1",
      "memories:modify:user-1",
      "memories:modify:user-1",
      "memories:modify:user-1",
      "memories:modify:user-1",
      "memories:modify:user-1",
      "memories:delete_batch:user-1",
      "memories:delete:user-1",
      "memories:delete_all:user-1",
    ]);
  });

  it("routes the complete advice CRUD surface to the authenticated core worker", async () => {
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
      ["GET", "/v1/advice?limit=25"],
      ["POST", "/v1/advice"],
      ["PATCH", "/v1/advice/advice-1"],
      ["DELETE", "/v1/advice/advice-1"],
      ["POST", "/v1/advice/mark-all-read"],
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
      "GET /v1/advice",
      "POST /v1/advice",
      "PATCH /v1/advice/advice-1",
      "DELETE /v1/advice/advice-1",
      "POST /v1/advice/mark-all-read",
    ]);
    expect(
      coreRequests.every((request) =>
        request.headers.has("x-omi-auth-context"),
      ),
    ).toBe(true);
    expect(rateLimitNames).toEqual([]);
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

  it("guards and routes conversation sharing and speaker mutations through core", async () => {
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
    };
    const unauthorized = await edge.fetch(
      new Request(
        "https://edge.test/v1/conversations/conversation-1/visibility?value=public",
        { method: "PATCH" },
      ),
      env as never,
    );
    expect(unauthorized.status).toBe(401);

    const auth = { authorization: "Bearer opaque-session" };
    const cases = [
      "/v1/conversations/conversation-1/segments/0/assign?assign_type=is_user&value=true",
      "/v1/conversations/conversation-1/assign-speaker/1?assign_type=person_id&value=person-1",
      "/v1/conversations/conversation-1/visibility?value=public",
    ];
    for (const path of cases) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method: "PATCH",
          headers: auth,
        }),
        env as never,
      );
      expect(response.status, path).toBe(200);
    }
    expect(
      coreRequests.map(
        (request) => `${request.method} ${new URL(request.url).pathname}`,
      ),
    ).toEqual([
      "PATCH /v1/conversations/conversation-1/segments/0/assign",
      "PATCH /v1/conversations/conversation-1/assign-speaker/1",
      "PATCH /v1/conversations/conversation-1/visibility",
    ]);

    const bulkResponse = await edge.fetch(
      new Request("https://edge.test/v1/conversations/conversation-1/segments/assign-bulk", {
        method: "PATCH",
        headers: { ...auth, "content-type": "application/json" },
        body: JSON.stringify({ segment_ids: ["segment-1"], assign_type: "person_id", value: "person-1" }),
      }),
      env as never,
    );
    expect(bulkResponse.status).toBe(200);
    expect(coreRequests.at(-1)?.method).toBe("PATCH");
    expect(new URL(coreRequests.at(-1)?.url || "https://invalid").pathname).toBe(
      "/v1/conversations/conversation-1/segments/assign-bulk",
    );
    expect(await coreRequests.at(-1)?.clone().json()).toEqual({
      segment_ids: ["segment-1"],
      assign_type: "person_id",
      value: "person-1",
    });
    expect(
      coreRequests.every((request) =>
        Boolean(request.headers.get("x-omi-auth-context")),
      ),
    ).toBe(true);
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

  it("routes Apple Health sync and connection lifecycle to the core worker", async () => {
    const coreRequests: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "health-user", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        coreRequests.push(request);
        return request.method === "DELETE"
          ? new Response(null, { status: 204 })
          : Response.json({ status: "ok", app_key: "apple_health" });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const headers = { authorization: "Bearer opaque-session" };
    const requests = [
      new Request("https://edge.test/v1/integrations/apple-health/sync", {
        method: "PUT",
        headers: { ...headers, "content-type": "application/json" },
        body: JSON.stringify({ period_days: 7, total_steps: 100 }),
      }),
      new Request("https://edge.test/v1/integrations/apple_health", {
        method: "PUT",
        headers: { ...headers, "content-type": "application/json" },
        body: JSON.stringify({}),
      }),
      new Request("https://edge.test/v1/integrations/apple_health", {
        method: "DELETE",
        headers,
      }),
    ];
    const responses = [];
    for (const request of requests) responses.push(await edge.fetch(request, env));

    expect(responses.map((response) => response.status)).toEqual([200, 200, 204]);
    expect(coreRequests).toHaveLength(3);
    expect(coreRequests.map((request) => `${request.method} ${new URL(request.url).pathname}`)).toEqual([
      "PUT /v1/integrations/apple-health/sync",
      "PUT /v1/integrations/apple_health",
      "DELETE /v1/integrations/apple_health",
    ]);
    for (const request of coreRequests) {
      expect(decodeAuthContext(request.headers.get("x-omi-auth-context"))).toMatchObject({
        uid: "health-user",
        audience: "api-core",
      });
      expect(request.headers.get("authorization")).toBeNull();
    }
  });

  it("routes Joan follow-up questions to the authenticated core worker", async () => {
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
        return Response.json({ result: "What should we explore next?" });
      }),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/joan/conversation-1/followup-question", {
        method: "DELETE",
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ result: "What should we explore next?" });
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/joan/conversation-1/followup-question",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes persona initial-message generation to the authenticated core worker", async () => {
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
        return Response.json({ message: "Want to chat about something fun?" });
      }),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/personas/twitter/initial-message?username=alice", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ message: "Want to chat about something fun?" });
    expect(coreRequests).toHaveLength(1);
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/personas/twitter/initial-message",
    );
    expect(new URL(coreRequests[0].url).search).toBe("?username=alice");
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

  it("authenticates audio metadata while allowing core-verified signed playback", async () => {
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
        return new Response("audio", { status: 200 });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };

    const urls = await edge.fetch(
      new Request("https://edge.test/v1/sync/audio/conversation-1/urls", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );
    expect(urls.status).toBe(200);
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();

    const playback = await edge.fetch(
      new Request(
        "https://edge.test/v1/sync/audio/conversation-1/audio-1?token=core-signed",
      ),
      env,
    );
    expect(playback.status).toBe(200);
    expect(coreRequests[1].headers.get("x-omi-auth-context")).toBeNull();

    const unsigned = await edge.fetch(
      new Request("https://edge.test/v1/sync/audio/conversation-1/audio-1"),
      env,
    );
    expect(unsigned.status).toBe(401);
    expect(coreRequests).toHaveLength(2);
  });

  it("authenticates speech-profile APIs while allowing signed profile playback", async () => {
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
        return new Response("profile", { status: 200 });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };

    const unauthenticated = await edge.fetch(
      new Request("https://edge.test/v3/speech-profile"),
      env,
    );
    expect(unauthenticated.status).toBe(401);
    expect(coreRequests).toHaveLength(0);

    const profile = await edge.fetch(
      new Request("https://edge.test/v3/speech-profile", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );
    expect(profile.status).toBe(200);
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();

    const playback = await edge.fetch(
      new Request(
        "https://edge.test/v3/speech-profile/audio?token=core-signed",
        {
          headers: {
            authorization: "Bearer untrusted-client-token",
            "x-omi-auth-context": "untrusted-context",
            "x-omi-internal-signature": "untrusted-signature",
          },
        },
      ),
      env,
    );
    expect(playback.status).toBe(200);
    expect(coreRequests[1].headers.get("authorization")).toBeNull();
    expect(coreRequests[1].headers.get("x-omi-auth-context")).toBeNull();
    expect(coreRequests[1].headers.get("x-omi-internal-signature")).toBeNull();
  });

  it("routes legacy audio precache to Jobs as its single rebuild owner", async () => {
    const jobRequests: Request[] = [];
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
        return Response.json({ error: "wrong owner" }, { status: 500 });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
      JOBS: service((request) => {
        jobRequests.push(request);
        return Response.json({ status: "started" }, { status: 202 });
      }),
    };

    const response = await edge.fetch(
      new Request("https://edge.test/v1/sync/audio/conversation-1/precache", {
        method: "POST",
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );

    expect(response.status).toBe(202);
    expect(jobRequests).toHaveLength(1);
    expect(new URL(jobRequests[0].url).pathname).toBe(
      "/v1/sync/audio/conversation-1/precache",
    );
    const context = decodeAuthContext(
      jobRequests[0].headers.get("x-omi-auth-context"),
    );
    expect(context).toMatchObject({ uid: "user-1", audience: "jobs" });
    expect(corePaths).toEqual([]);
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

  it("routes account, quota, and LLM usage reads and writes to API Core", async () => {
    const corePaths: string[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({
            uid: "user-1",
            authority: "better-auth",
            accountCreatedAt: 1_700_000_000,
          });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        corePaths.push(new URL(request.url).pathname);
        expect(request.headers.get("authorization")).toBeNull();
        expect(
          decodeAuthContext(request.headers.get("x-omi-auth-context")),
        ).toMatchObject({
          uid: "user-1",
          accountCreatedAt: 1_700_000_000,
        });
        return Response.json({ status: "ok" });
      }),
      API_AI: service(() =>
        Response.json({ error: "wrong owner" }, { status: 500 }),
      ),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };

    for (const path of [
      "/v1/users/me/usage?period=monthly",
      "/v1/users/me/subscription",
      "/v1/users/me/usage-quota",
      "/v1/users/me/paywall?platform=desktop",
      "/v1/users/me/trial",
      "/v1/users/me/llm-usage?days=30",
      "/v1/users/me/llm-usage/top-features?days=30&limit=3",
      "/v1/users/me/llm-usage/total",
      "/v1/users/export",
      "/v1/payments/available-plans",
      "/v1/payments/overage-info",
    ]) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          headers: { authorization: "Bearer opaque-session" },
        }),
        env as never,
      );
      expect(response.status).toBe(200);
    }

    expect(corePaths).toEqual([
      "/v1/users/me/usage",
      "/v1/users/me/subscription",
      "/v1/users/me/usage-quota",
      "/v1/users/me/paywall",
      "/v1/users/me/trial",
      "/v1/users/me/llm-usage",
      "/v1/users/me/llm-usage/top-features",
      "/v1/users/me/llm-usage/total",
      "/v1/users/export",
      "/v1/payments/available-plans",
      "/v1/payments/overage-info",
    ]);

    const write = await edge.fetch(
      new Request("https://edge.test/v1/users/me/llm-usage", {
        method: "POST",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "application/json",
        },
        body: JSON.stringify({ input_tokens: 1, output_tokens: 2 }),
      }),
      env as never,
    );
    expect(write.status).toBe(200);
    expect(corePaths.at(-1)).toBe("/v1/users/me/llm-usage");
  });

  it("routes person speech-sample deletion to authenticated API Core", async () => {
    let forwarded: Request | undefined;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service(() =>
        Response.json({ uid: "user-1", authority: "better-auth" }),
      ),
      API_CORE: service((request) => {
        forwarded = request;
        return new Response(null, { status: 204 });
      }),
    };

    const unauthenticated = await edge.fetch(
      new Request(
        "https://edge.test/v1/users/people/person-1/speech-samples/0",
        { method: "DELETE" },
      ),
      env as never,
    );
    expect(unauthenticated.status).toBe(401);
    expect(forwarded).toBeUndefined();

    const response = await edge.fetch(
      new Request(
        "https://edge.test/v1/users/people/person-1/speech-samples/0",
        {
          method: "DELETE",
          headers: { authorization: "Bearer opaque-session" },
        },
      ),
      env as never,
    );
    expect(response.status).toBe(204);
    expect(forwarded?.method).toBe("DELETE");
    expect(new URL(forwarded?.url ?? "https://invalid.test").pathname).toBe(
      "/v1/users/people/person-1/speech-samples/0",
    );
    expect(forwarded?.headers.get("authorization")).toBeNull();
    expect(
      decodeAuthContext(forwarded?.headers.get("x-omi-auth-context") ?? null),
    ).toMatchObject({ uid: "user-1" });
  });

  it("routes the authenticated fair-use status read to API Core", async () => {
    let forwarded: Request | undefined;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "user-1", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        forwarded = request;
        return Response.json({ stage: "none" });
      }),
      API_AI: service(() =>
        Response.json({ error: "wrong owner" }, { status: 500 }),
      ),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };

    const response = await edge.fetch(
      new Request("https://edge.test/v1/fair-use/status", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );

    expect(response.status).toBe(200);
    expect(new URL(forwarded?.url || "https://invalid").pathname).toBe(
      "/v1/fair-use/status",
    );
    expect(forwarded?.headers.get("authorization")).toBeNull();
    expect(forwarded?.headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes the data-protection migration inventory to API Core", async () => {
    let forwarded: Request | undefined;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "migration-user", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        forwarded = request;
        return Response.json({ needs_migration: [] });
      }),
    };

    const unauthenticated = await edge.fetch(
      new Request("https://edge.test/v1/users/migration/requests?target_level=enhanced"),
      env as never,
    );
    expect(unauthenticated.status).toBe(401);

    const response = await edge.fetch(
      new Request("https://edge.test/v1/users/migration/requests?target_level=enhanced", {
        headers: { authorization: "Bearer opaque-session", cookie: "session=must-not-forward" },
      }),
      env as never,
    );
    expect(response.status).toBe(200);
    expect(new URL(forwarded?.url || "https://invalid").pathname).toBe(
      "/v1/users/migration/requests",
    );
    expect(new URL(forwarded?.url || "https://invalid").search).toBe(
      "?target_level=enhanced",
    );
    expect(forwarded?.headers.get("authorization")).toBeNull();
    expect(forwarded?.headers.get("cookie")).toBeNull();
    expect(forwarded?.headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes candidate workflow control to API Core with the authenticated boundary", async () => {
    let forwarded: Request | undefined;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "candidate-user", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        forwarded = request;
        return Response.json({ workflow_mode: "off", account_generation: 0, chat_first_ui: false });
      }),
    };

    const unauthenticated = await edge.fetch(
      new Request("https://edge.test/v1/candidates/control"),
      env as never,
    );
    expect(unauthenticated.status).toBe(401);

    const response = await edge.fetch(
      new Request("https://edge.test/v1/candidates/control", {
        headers: { authorization: "Bearer opaque-session", cookie: "session=must-not-forward" },
      }),
      env as never,
    );
    expect(response.status).toBe(200);
    expect(new URL(forwarded?.url || "https://invalid").pathname).toBe("/v1/candidates/control");
    expect(forwarded?.headers.get("authorization")).toBeNull();
    expect(forwarded?.headers.get("cookie")).toBeNull();
    expect(forwarded?.headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("keeps candidate lifecycle routes on the fail-closed API Core boundary", async () => {
    let forwarded: Request | undefined;
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: service((request) => {
        if (request.url.endsWith("/internal/verify")) {
          return Response.json({ uid: "candidate-user", authority: "better-auth" });
        }
        return Response.json({ status: "ok" });
      }),
      API_CORE: service((request) => {
        forwarded = request;
        return Response.json({ detail: "Not found" }, { status: 404 });
      }),
    };

    const unauthenticated = await edge.fetch(
      new Request("https://edge.test/v1/candidates/candidate-1", { method: "GET" }),
      env as never,
    );
    expect(unauthenticated.status).toBe(401);

    const response = await edge.fetch(
      new Request("https://edge.test/v1/candidates/candidate-1", {
        headers: { authorization: "Bearer opaque-session", cookie: "session=must-not-forward" },
      }),
      env as never,
    );
    expect(response.status).toBe(404);
    expect(new URL(forwarded?.url || "https://invalid").pathname).toBe(
      "/v1/candidates/candidate-1",
    );
    expect(forwarded?.headers.get("authorization")).toBeNull();
    expect(forwarded?.headers.get("cookie")).toBeNull();
    expect(forwarded?.headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("routes the public high-entropy fair-use case lookup without authentication context", async () => {
    let forwarded: Request | undefined;
    const env = {
      API_CORE: service((request) => {
        forwarded = request;
        return Response.json({ case_ref: "FU-A1B2C3D4E5F6", stage: "warning" });
      }),
    };

    const response = await edge.fetch(
      new Request("https://edge.test/v1/fair-use/case/FU-A1B2C3D4E5F6/status"),
      env as never,
    );

    expect(response.status).toBe(200);
    expect(new URL(forwarded?.url || "https://invalid").pathname).toBe(
      "/v1/fair-use/case/FU-A1B2C3D4E5F6/status",
    );
    expect(forwarded?.headers.get("authorization")).toBeNull();
    expect(forwarded?.headers.get("x-omi-auth-context")).toBeNull();
  });

  it("routes the app voice-message upload to the API AI worker", async () => {
    let forwarded: Request | undefined;
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
        forwarded = request;
        return Response.json({ transcript: "hello", outcome: "success" });
      }),
      RATE_LIMITS: rateLimits(allowRateLimit, rateLimitNames),
    };
    const body =
      "--voice\r\ncontent-disposition: form-data; name=files; filename=audio.wav\r\n\r\naudio\r\n--voice--\r\n";
    const response = await edge.fetch(
      new Request("https://edge.test/v2/voice-message/transcribe", {
        method: "POST",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "multipart/form-data; boundary=voice",
        },
        body,
      }),
      env as never,
    );

    expect(response.status).toBe(200);
    expect(new URL(forwarded?.url || "https://invalid.test").pathname).toBe(
      "/v2/voice-message/transcribe",
    );
    expect(forwarded?.headers.get("authorization")).toBeNull();
    expect(forwarded?.headers.get("x-omi-auth-context")).toBeTruthy();
    expect(await forwarded?.text()).toBe(body);
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

  it("streams sync manifests, multipart WAL uploads, and status reads to jobs", async () => {
    const jobRequests: Request[] = [];
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
      REALTIME: service(() => Response.json({ status: "ok" })),
      JOBS: service(async (request) => {
        jobRequests.push(request);
        return Response.json({ status: "queued" }, { status: 202 });
      }),
    };
    const manifest = await edge.fetch(
      new Request("https://edge.test/v2/sync-capture-manifest", {
        method: "POST",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "application/json",
          "x-app-platform": "ios",
          "x-device-id-hash": "1234abcd",
        },
        body: JSON.stringify({ conversation_id: "capture-1", files: [] }),
      }),
      env as never,
    );
    expect(manifest.status).toBe(202);

    const form = new FormData();
    form.append(
      "files",
      new File(
        [new Uint8Array([1, 2, 3])],
        "audio_pcm16_16000_1_fs160_1787932800.bin",
      ),
    );
    const upload = await edge.fetch(
      new Request(
        "https://edge.test/v2/sync-local-files?conversation_id=capture-1",
        {
          method: "POST",
          headers: {
            authorization: "Bearer opaque-session",
            "x-app-platform": "ios",
            "x-device-id-hash": "1234abcd",
            "x-omi-sync-capture-manifest": "signed.manifest",
          },
          body: form,
        },
      ),
      env as never,
    );
    expect(upload.status).toBe(202);

    const status = await edge.fetch(
      new Request("https://edge.test/v2/sync-local-files/job-1", {
        headers: { authorization: "Bearer opaque-session" },
      }),
      env as never,
    );
    expect(status.status).toBe(202);
    expect(jobRequests.map((request) => new URL(request.url).pathname)).toEqual(
      [
        "/v2/sync-capture-manifest",
        "/v2/sync-local-files",
        "/v2/sync-local-files/job-1",
      ],
    );
    expect(
      new URL(jobRequests[1].url).searchParams.get("conversation_id"),
    ).toBe("capture-1");
    expect(jobRequests[1].headers.get("x-omi-auth-context")).toBeTruthy();
    expect(jobRequests[1].headers.get("x-app-platform")).toBe("ios");
    expect(jobRequests[1].headers.get("x-device-id-hash")).toBe("1234abcd");
    expect(jobRequests[1].headers.get("x-omi-sync-capture-manifest")).toBe(
      "signed.manifest",
    );
    const forwarded = await jobRequests[1].formData();
    expect((forwarded.get("files") as File).size).toBe(3);
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

  it("routes the mobile TTS contract to API AI with the legacy edge policy", async () => {
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
      new Request("https://edge.test/v2/tts/synthesize", {
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
    expect(aiPath).toBe("/v2/tts/synthesize");
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

  it("routes destructive recording deletion to the durable Jobs owner", async () => {
    let jobsRequest: Request | undefined;
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
      JOBS: service((request) => {
        jobsRequest = request;
        return Response.json({ status: "ok" });
      }),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/users/store-recording-permission", {
        method: "DELETE",
        headers: { authorization: "Bearer opaque-session" },
      }),
      env,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
    expect(new URL(jobsRequest!.url).pathname).toBe(
      "/v1/users/store-recording-permission",
    );
    expect(jobsRequest!.headers.get("x-omi-auth-context")).toBeTruthy();
  });

  it("preserves only the app API key for integration routes", async () => {
    let forwarded: Request | undefined;
    let authCalls = 0;
    const response = await edge.fetch(
      new Request(
        "https://edge.test/v2/integrations/integration-app/tasks?uid=user-1",
        {
          headers: {
            authorization: "Bearer sk_integration-secret",
            cookie: "session=must-not-forward",
            "x-omi-auth-context": "attacker-context",
            "x-omi-internal-signature": "attacker-signature",
          },
        },
      ),
      {
        AUTH: rawService(() => {
          authCalls += 1;
          return Response.json({ status: "unexpected" });
        }),
        API_CORE: rawService((request) => {
          forwarded = request;
          return Response.json({ tasks: [] });
        }),
      } as never,
    );
    expect(response.status).toBe(200);
    expect(authCalls).toBe(0);
    expect(forwarded?.headers.get("authorization")).toBe(
      "Bearer sk_integration-secret",
    );
    expect(forwarded?.headers.get("cookie")).toBeNull();
    expect(forwarded?.headers.get("x-omi-auth-context")).toBeNull();
    expect(forwarded?.headers.get("x-omi-internal-signature")).toBeNull();
  });

  it("routes Developer API reads directly to API Core with only the Developer bearer", async () => {
    const forwarded: Request[] = [];
    let authCalls = 0;
    const env = {
      AUTH: rawService(() => {
        authCalls += 1;
        return Response.json({ status: "unexpected" });
      }),
      API_CORE: rawService((request) => {
        forwarded.push(request);
        return Response.json({ status: "ok" });
      }),
    } as never;
    const paths = [
      "/v1/dev/user/memories",
      "/v1/dev/user/memories/vector/search?query=cloudflare",
      "/v1/dev/user/action-items",
      "/v1/dev/user/folders",
      "/v1/dev/user/conversations",
      "/v1/dev/user/conversations/conversation-1",
      "/v1/dev/user/goals",
      "/v1/dev/user/goals/goal-1",
      "/v1/dev/user/goals/goal-1/history",
    ];
    const bearer = `Bearer omi_dev_${"c".repeat(32)}`;

    for (const path of paths) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          headers: {
            authorization: bearer,
            cookie: "session=must-not-forward",
            "x-omi-auth-context": "attacker-context",
            "x-omi-internal-signature": "attacker-signature",
          },
        }),
        env,
      );
      expect(response.status).toBe(200);
    }

    expect(authCalls).toBe(0);
    expect(forwarded).toHaveLength(paths.length);
    for (const [index, request] of forwarded.entries()) {
      const expected = new URL(`https://edge.test${paths[index]}`);
      const actual = new URL(request.url);
      expect(actual.pathname).toBe(expected.pathname);
      expect(actual.search).toBe(expected.search);
      expect(request.headers.get("authorization")).toBe(bearer);
      expect(request.headers.get("cookie")).toBeNull();
      expect(request.headers.get("x-omi-auth-context")).toBeNull();
      expect(request.headers.get("x-omi-internal-signature")).toBeNull();
    }
  });

  it("routes Developer API mutations with the raw bearer and body", async () => {
    const forwarded: Request[] = [];
    let authCalls = 0;
    const rateLimitNames: string[] = [];
    const env = {
      AUTH: rawService(() => {
        authCalls += 1;
        return Response.json({ status: "unexpected" });
      }),
      API_CORE: rawService((request) => {
        forwarded.push(request);
        return Response.json({ status: "ok" });
      }),
      RATE_LIMITS: rateLimits(allowRateLimit, rateLimitNames),
    } as never;
    const cases = [
      ["POST", "/v1/dev/user/memories", '{"content":"one"}'],
      ["POST", "/v1/dev/user/memories/batch", '{"memories":[]}'],
      ["PATCH", "/v1/dev/user/memories/memory-1", '{"content":"two"}'],
      ["DELETE", "/v1/dev/user/memories/memory-1"],
      ["POST", "/v1/dev/user/action-items", '{"description":"one"}'],
      ["POST", "/v1/dev/user/action-items/batch", '{"action_items":[]}'],
      ["PATCH", "/v1/dev/user/action-items/action-1", '{"completed":true}'],
      ["DELETE", "/v1/dev/user/action-items/action-1"],
      ["POST", "/v1/dev/user/conversations", '{"text":"Worker native"}'],
      [
        "POST",
        "/v1/dev/user/conversations/from-segments",
        '{"transcript_segments":[{"text":"Worker native","start":0,"end":1}]}',
      ],
      [
        "PATCH",
        "/v1/dev/user/conversations/conversation-1",
        '{"discarded":true}',
      ],
      ["DELETE", "/v1/dev/user/conversations/conversation-1"],
      ["POST", "/v1/dev/user/goals", '{"title":"Ship Cloudflare"}'],
      ["PATCH", "/v1/dev/user/goals/goal-1", '{"title":"Verify Cloudflare"}'],
      ["PATCH", "/v1/dev/user/goals/goal-1/progress?current_value=7"],
      ["DELETE", "/v1/dev/user/goals/goal-1"],
    ] as const;
    const bearer = `Bearer omi_dev_${"d".repeat(32)}`;

    for (const [method, path, body] of cases) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method,
          headers: {
            authorization: bearer,
            cookie: "session=must-not-forward",
            "content-type": "application/json",
            "x-omi-auth-context": "attacker-context",
            "x-omi-internal-signature": "attacker-signature",
          },
          body,
        }),
        env,
      );
      expect(response.status).toBe(200);
    }

    expect(authCalls).toBe(0);
    expect(forwarded).toHaveLength(cases.length);
    for (const [index, request] of forwarded.entries()) {
      expect(request.method).toBe(cases[index][0]);
      expect(new URL(request.url).pathname).toBe(
        new URL(`https://edge.test${cases[index][1]}`).pathname,
      );
      expect(request.headers.get("authorization")).toBe(bearer);
      expect(request.headers.get("cookie")).toBeNull();
      expect(request.headers.get("x-omi-auth-context")).toBeNull();
      expect(request.headers.get("x-omi-internal-signature")).toBeNull();
      if (cases[index][2]) {
        await expect(request.text()).resolves.toBe(cases[index][2]);
      }
    }
    expect(rateLimitNames).toHaveLength(cases.length);
    expect(rateLimitNames).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/^memories:create:developer:[0-9a-f]{64}$/),
        expect.stringMatching(/^memories:batch:developer:[0-9a-f]{64}$/),
        expect.stringMatching(/^action_items:write:developer:[0-9a-f]{64}$/),
        expect.stringMatching(/^dev:conversations:developer:[0-9a-f]{64}$/),
        expect.stringMatching(/^dev:goals_write:developer:[0-9a-f]{64}$/),
      ]),
    );
    expect(rateLimitNames.join(" ")).not.toContain("d".repeat(32));
  });

  it("routes MCP data tools directly to API Core with only the MCP bearer", async () => {
    const forwarded: Request[] = [];
    let authCalls = 0;
    const rateLimitNames: string[] = [];
    const env = {
      AUTH: rawService(() => {
        authCalls += 1;
        return Response.json({ status: "unexpected" });
      }),
      API_CORE: rawService((request) => {
        forwarded.push(request);
        return Response.json({ status: "ok" });
      }),
      RATE_LIMITS: rateLimits(allowRateLimit, rateLimitNames),
    } as never;
    const cases = [
      ["POST", "/v1/mcp/memories", "{}"],
      ["DELETE", "/v1/mcp/memories/memory-1"],
      ["PATCH", "/v1/mcp/memories/memory-1"],
      ["GET", "/v1/mcp/profile"],
      ["GET", "/v1/mcp/memories"],
      ["GET", "/v1/mcp/memories/search"],
      ["GET", "/v1/mcp/conversations"],
      ["GET", "/v1/mcp/conversations/search"],
      ["GET", "/v1/mcp/conversations/conversation-1"],
      ["GET", "/v1/mcp/action-items"],
      ["GET", "/v1/mcp/action-items/search"],
      ["POST", "/v1/mcp/action-items", "{}"],
      ["POST", "/v1/mcp/action-items/item-1/complete", "{}"],
      ["PATCH", "/v1/mcp/action-items/item-1", "{}"],
      ["DELETE", "/v1/mcp/action-items/item-1"],
      ["GET", "/v1/mcp/goals"],
      ["GET", "/v1/mcp/chat"],
      ["GET", "/v1/mcp/people"],
      ["GET", "/v1/mcp/screen-activity"],
      ["GET", "/v1/mcp/daily-summaries"],
    ] as const;
    const bearer = `Bearer omi_mcp_${"a".repeat(32)}`;

    for (const [method, path, body] of cases) {
      const response = await edge.fetch(
        new Request(`https://edge.test${path}`, {
          method,
          headers: {
            authorization: bearer,
            cookie: "session=must-not-forward",
            "content-type": "application/json",
            "x-omi-auth-context": "attacker-context",
            "x-omi-internal-signature": "attacker-signature",
          },
          body,
        }),
        env,
      );
      expect(response.status).toBe(200);
    }

    expect(authCalls).toBe(0);
    expect(forwarded).toHaveLength(cases.length);
    for (const [index, request] of forwarded.entries()) {
      expect(request.method).toBe(cases[index][0]);
      expect(new URL(request.url).pathname).toBe(cases[index][1]);
      expect(request.headers.get("authorization")).toBe(bearer);
      expect(request.headers.get("cookie")).toBeNull();
      expect(request.headers.get("x-omi-auth-context")).toBeNull();
      expect(request.headers.get("x-omi-internal-signature")).toBeNull();
    }
    expect(rateLimitNames).toHaveLength(5);
    expect(rateLimitNames).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/^memories:create:mcp:[0-9a-f]{64}$/),
        expect.stringMatching(/^action_items:write:mcp:[0-9a-f]{64}$/),
      ]),
    );
    expect(rateLimitNames.join(" ")).not.toContain("a".repeat(32));
  });

  it("routes MCP conversation search to API Core before the detail parameter", async () => {
    const corePaths: string[] = [];
    const env = {
      API_CORE: rawService((request) => {
        corePaths.push(new URL(request.url).pathname);
        return Response.json({ owner: "api-core" });
      }),
    } as never;
    const search = await edge.fetch(
      new Request("https://edge.test/v1/mcp/conversations/search", {
        headers: { authorization: `Bearer omi_mcp_${"b".repeat(32)}` },
      }),
      env,
    );
    const detail = await edge.fetch(
      new Request("https://edge.test/v1/mcp/conversations/conversation-1", {
        headers: { authorization: `Bearer omi_mcp_${"b".repeat(32)}` },
      }),
      env,
    );
    expect(await search.json()).toEqual({ owner: "api-core" });
    expect(await detail.json()).toEqual({ owner: "api-core" });
    expect(corePaths).toEqual([
      "/v1/mcp/conversations/search",
      "/v1/mcp/conversations/conversation-1",
    ]);
  });

  it("routes app API key management through authenticated Jobs", async () => {
    let forwarded: Request | undefined;
    const response = await edge.fetch(
      new Request("https://edge.test/v1/apps/integration-app/keys", {
        method: "POST",
        headers: {
          authorization: "Bearer owner-session",
          cookie: "session=owner-session",
        },
      }),
      {
        INTERNAL_ASSERTION_SECRET: "test-secret",
        AUTH: rawService(() =>
          Response.json({ uid: "owner-user", authority: "better-auth" }),
        ),
        API_CORE: service(() => Response.json({ status: "unexpected" })),
        JOBS: rawService((request) => {
          forwarded = request;
          return Response.json({ status: "ok" });
        }),
      } as never,
    );
    expect(response.status).toBe(200);
    expect(forwarded?.headers.get("authorization")).toBeNull();
    expect(forwarded?.headers.get("cookie")).toBeNull();
    expect(forwarded?.headers.get("x-omi-auth-context")).toBeTruthy();
    expect(forwarded?.headers.get("x-omi-internal-signature")).toBeTruthy();
  });

  it("routes every MCP API key lifecycle method through authenticated Jobs", async () => {
    const forwarded: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: rawService(() =>
        Response.json({ uid: "mcp-owner", authority: "better-auth" }),
      ),
      API_CORE: service(() => Response.json({ status: "unexpected" })),
      JOBS: rawService((request) => {
        forwarded.push(request);
        return request.method === "DELETE"
          ? new Response(null, { status: 204 })
          : Response.json({ status: "ok" });
      }),
    } as never;
    const cases = [
      {
        method: "POST",
        path: "/v1/mcp/keys",
        body: JSON.stringify({ name: "Cloudflare smoke" }),
      },
      { method: "GET", path: "/v1/mcp/keys" },
      { method: "DELETE", path: "/v1/mcp/keys/missing-key" },
    ];

    for (const testCase of cases) {
      const response = await edge.fetch(
        new Request(`https://edge.test${testCase.path}`, {
          method: testCase.method,
          headers: {
            authorization: "Bearer owner-session",
            cookie: "session=must-not-forward",
            "content-type": "application/json",
            "x-omi-auth-context": "attacker-context",
          },
          body: testCase.body,
        }),
        env,
      );
      expect(response.status).toBe(testCase.method === "DELETE" ? 204 : 200);
    }

    expect(forwarded).toHaveLength(3);
    for (const [index, request] of forwarded.entries()) {
      expect(request.method).toBe(cases[index].method);
      expect(new URL(request.url).pathname).toBe(cases[index].path);
      expect(request.headers.get("authorization")).toBeNull();
      expect(request.headers.get("cookie")).toBeNull();
      expect(
        decodeAuthContext(request.headers.get("x-omi-auth-context")),
      ).toMatchObject({ uid: "mcp-owner", authority: "better-auth" });
      expect(request.headers.get("x-omi-internal-signature")).toBeTruthy();
    }
    await expect(forwarded[0].text()).resolves.toBe(cases[0].body);
  });

  it("routes every Developer API key lifecycle method through authenticated Jobs", async () => {
    const forwarded: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      AUTH: rawService(() =>
        Response.json({ uid: "developer-owner", authority: "better-auth" }),
      ),
      API_CORE: service(() => Response.json({ status: "unexpected" })),
      JOBS: rawService((request) => {
        forwarded.push(request);
        return request.method === "DELETE"
          ? new Response(null, { status: 204 })
          : Response.json({ status: "ok" });
      }),
    } as never;
    const cases = [
      {
        method: "POST",
        path: "/v1/dev/keys",
        body: JSON.stringify({ name: "Cloudflare Developer API" }),
      },
      { method: "GET", path: "/v1/dev/keys" },
      { method: "DELETE", path: "/v1/dev/keys/missing-key" },
    ];

    for (const testCase of cases) {
      const response = await edge.fetch(
        new Request(`https://edge.test${testCase.path}`, {
          method: testCase.method,
          headers: {
            authorization: "Bearer owner-session",
            cookie: "session=must-not-forward",
            "content-type": "application/json",
            "x-omi-auth-context": "attacker-context",
          },
          body: testCase.body,
        }),
        env,
      );
      expect(response.status).toBe(testCase.method === "DELETE" ? 204 : 200);
    }

    expect(forwarded).toHaveLength(3);
    for (const [index, request] of forwarded.entries()) {
      expect(request.method).toBe(cases[index].method);
      expect(new URL(request.url).pathname).toBe(cases[index].path);
      expect(request.headers.get("authorization")).toBeNull();
      expect(request.headers.get("cookie")).toBeNull();
      expect(
        decodeAuthContext(request.headers.get("x-omi-auth-context")),
      ).toMatchObject({
        uid: "developer-owner",
        authority: "better-auth",
      });
      expect(request.headers.get("x-omi-internal-signature")).toBeTruthy();
    }
    await expect(forwarded[0].text()).resolves.toBe(cases[0].body);
  });
});
