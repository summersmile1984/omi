import { describe, expect, it } from "vitest";
import edge from "../workers/edge/index";

const service = (handler: (request: Request) => Promise<Response> | Response) => ({ fetch: handler }) as Fetcher;

describe("edge gateway", () => {
  it("serves a versioned health response", async () => {
    const response = await edge.fetch(new Request("https://edge.test/health"), {} as never);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok", service: "edge", version: "cf-00" });
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
      API_CORE: service(() => Response.json({ error: "wrong owner" }, { status: 500 })),
      API_AI: service((request) => {
        aiPath = new URL(request.url).pathname;
        return Response.json({ error: "transcription provider is not configured" }, { status: 503 });
      }),
      REALTIME: service(() => Response.json({ status: "ok" })),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/stt/transcribe", {
        method: "POST",
        headers: { authorization: "Bearer opaque-session", "content-type": "multipart/form-data" },
        body: "audio",
      }),
      env,
    );
    expect(response.status).toBe(503);
    expect(aiPath).toBe("/v1/stt/transcribe");
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
      API_CORE: service(() => Response.json({ error: "wrong owner" }, { status: 500 })),
      API_AI: service(() => Response.json({ error: "wrong owner" }, { status: 500 })),
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
        new Request(`https://edge.test${path}`, { headers: { authorization: "Bearer opaque-session" } }),
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
});
