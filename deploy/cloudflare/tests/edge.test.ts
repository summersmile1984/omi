import { describe, expect, it } from "vitest";
import edge from "../workers/edge/index";

const service = (handler: (request: Request) => Promise<Response> | Response) =>
  ({ fetch: handler }) as Fetcher;

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
      version: "cf-00",
    });
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

  it("keeps approved app catalog public while guarding popular reads", async () => {
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

    expect(approved.status).toBe(200);
    expect(popularUnauthenticated.status).toBe(401);
    expect(popular.status).toBe(200);
    expect(corePaths).toEqual(["/v1/approved-apps", "/v1/apps/popular"]);
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
    expect((await edge.fetch(new Request("https://edge.test/v1/apps/enabled", { headers: auth }), env as never)).status).toBe(200);
    expect((await edge.fetch(new Request("https://edge.test/v1/apps/enable?app_id=free-app", { method: "POST", headers: auth }), env as never)).status).toBe(200);
    expect((await edge.fetch(new Request("https://edge.test/v1/apps/disable?app_id=free-app", { method: "POST", headers: auth }), env as never)).status).toBe(200);
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
    const response = await edge.fetch(new Request("https://edge.test/v2/apps?limit=20"), env as never);
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
    expect(new URL(coreRequests[0].url).pathname).toBe(
      "/v1/cf/conversations",
    );
    expect(coreRequests[0].headers.get("x-omi-auth-context")).toBeTruthy();
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
      new Request("https://edge.test/v1/conversations/conversation-1/segments/text", {
        method: "PATCH",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "application/json",
        },
        body: JSON.stringify({ segment_id: "segment-1", text: "edited" }),
      }),
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
        { method: "DELETE", headers: { authorization: "Bearer opaque-session" } },
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
      new Request("https://edge.test/v1/conversations/conversation-1/recording", {
        headers: { authorization: "Bearer opaque-session" },
      }),
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
        return Response.json({ deepgram: [], soniox: [], speechmatics: [], whisperx: [] });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/conversations/conversation-1/transcripts", {
        headers: { authorization: "Bearer opaque-session" },
      }),
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
        return Response.json({ conversation_id: "conversation-1", speakers: [] });
      }),
      API_AI: service(() => Response.json({ status: "ok" })),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/conversations/conversation-1/analytics", {
        headers: { authorization: "Bearer opaque-session" },
      }),
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
      new Request("https://edge.test/v1/conversations/conversation-1/action-items", {
        method: "PATCH",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "application/json",
        },
        body: JSON.stringify({ items_idx: [0], values: [true] }),
      }),
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
      new Request("https://edge.test/v1/conversations/conversation-1/action-items/0", {
        method: "PATCH",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "application/json",
        },
        body: JSON.stringify({ old_description: "task", description: "renamed" }),
      }),
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
      new Request("https://edge.test/v1/conversations/conversation-1/action-items", {
        method: "DELETE",
        headers: {
          authorization: "Bearer opaque-session",
          "content-type": "application/json",
        },
        body: JSON.stringify({ description: "task", completed: false }),
      }),
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
    };
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
      API_CORE: service((request) => {
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

  it("routes async native transcription bodies to the jobs worker and rewrites status reads", async () => {
    const jobRequests: Request[] = [];
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
      API_CORE: service(() => Response.json({ error: "wrong owner" }, { status: 500 })),
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
  });

  it("keeps every realtime contract on the realtime binding", async () => {
    const realtimePaths: string[] = [];
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
      REALTIME: service((request) => {
        realtimePaths.push(new URL(request.url).pathname);
        return Response.json({ status: "ok" });
      }),
    };
    for (const path of [
      "/v2/voice-message/transcribe-stream",
      "/v4/listen",
      "/v4/web/listen",
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
    expect(realtimePaths).toEqual([
      "/v2/voice-message/transcribe-stream",
      "/v4/listen",
      "/v4/web/listen",
      "/v1/omni/relay",
    ]);
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
