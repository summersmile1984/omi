import { describe, expect, it } from "vitest";
import {
  assertAuthenticatedSmokeConfigured,
  parseTokenPayload,
  resolveEdgeUrl,
  resolveWebUrl,
  runSmoke,
} from "../scripts/smoke-staging.mjs";

function workersAiChatResponse(text = "Cloudflare staging is ready") {
  const message = {
    id: "cf-smoke-ai-message",
    text,
    sender: "ai",
    created_at: "2026-08-28T00:00:00+00:00",
    type: "text",
  };
  const done = Buffer.from(JSON.stringify(message), "utf8").toString("base64");
  return new Response(`data: ${text}\n\ndone: ${done}\n\n`, {
    status: 200,
    headers: { "content-type": "text/event-stream; charset=utf-8" },
  });
}

describe("staging smoke helpers", () => {
  it("normalizes a valid edge URL and rejects unsupported protocols", () => {
    expect(resolveEdgeUrl("https://edge.example.test/")).toBe(
      "https://edge.example.test",
    );
    expect(() => resolveEdgeUrl("ftp://edge.example.test")).toThrow(
      "http or https",
    );
    expect(resolveWebUrl("https://web.example.test/")).toBe(
      "https://web.example.test",
    );
  });

  it("requires a non-empty token field", () => {
    expect(parseTokenPayload('{"token":"secret"}')).toBe("secret");
    expect(() => parseTokenPayload('{"token":""}')).toThrow("non-empty token");
  });

  it("requires authenticated smoke credentials for a staging release", () => {
    expect(() => assertAuthenticatedSmokeConfigured({})).toThrow(
      "staging release requires",
    );
    expect(() =>
      assertAuthenticatedSmokeConfigured({
        CLOUDFLARE_SMOKE_BEARER_TOKEN: " token ",
      }),
    ).not.toThrow();
    expect(() =>
      assertAuthenticatedSmokeConfigured({
        CLOUDFLARE_SMOKE_TOKEN_FILE: "/tmp/token.json",
      }),
    ).not.toThrow();
  });

  it("checks public, auth, Web proxy, and Workers AI boundaries", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (url.endsWith("/v1/account/cutover/control")) {
        return Response.json({
          state: "new",
          product_traffic_allowed: true,
          migration: { destination_backend_bound: true },
        });
      }
      if (url.endsWith("/v1/conversations/search")) {
        return new Response(null, { status: 200 });
      }
      if (url.endsWith("/api/proxy/v2/messages") && init?.method === "POST") {
        return workersAiChatResponse();
      }
      if (url.endsWith("/api/proxy/v2/messages") && init?.method === "DELETE") {
        return Response.json({ status: "ok" });
      }
      if (url.includes("/api/proxy/v2/messages?limit=1")) {
        return Response.json([{ id: "cf-initial-chat" }]);
      }
      if (url.includes("/v1/apps/enabled")) {
        return new Response(null, { status: 200 });
      }
      const status = url.endsWith("/health")
        ? 200
        : url.endsWith("/v1/announcements/general")
          ? 200
          : url.endsWith("/v1/apps/cf-staging-search-app/reviews")
            ? 200
            : url.endsWith("/v1/cf/probe")
              ? init?.headers
                ? 200
                : 401
              : url.includes("/v1/announcements/pending")
                ? 401
                : url.endsWith("/v1/announcements/all")
                  ? 403
                  : url.endsWith("/v1/stt/transcribe-async")
                    ? 401
                    : url.includes("/v2/apps/search")
                      ? 200
                      : url.includes("/v3/memories?")
                        ? 200
                        : url.endsWith("/v1/folders")
                          ? 200
                          : url.includes("/v1/conversations?") ||
                              url.endsWith("/v1/conversations") ||
                              url.endsWith("/v1/conversations/count")
                            ? 200
                            : url.includes("/v1/conversations/") &&
                                url.endsWith("/photos")
                              ? 404
                              : url.includes("/v1/conversations/") &&
                                  url.endsWith("/transcripts")
                                ? 404
                                : url.includes("/v1/conversations/") &&
                                    url.endsWith("/analytics")
                                  ? 404
                                  : url.includes("/v1/conversations/") &&
                                      url.endsWith("/events")
                                    ? 404
                                    : url.includes("/v1/conversations/") &&
                                        url.endsWith("/summary")
                                      ? 404
                                      : url.includes("/v1/conversations/") &&
                                          url.endsWith("/calendar-event")
                                        ? 404
                                        : url.includes("/v1/conversations/") &&
                                            url.endsWith("/action-items")
                                          ? 404
                                          : url.includes(
                                                "/v1/conversations/",
                                              ) &&
                                              url.endsWith("/action-items/0")
                                            ? 404
                                            : url.includes(
                                                  "/v1/conversations/",
                                                ) && url.endsWith("/recording")
                                              ? 404
                                              : url.includes(
                                                    "/v1/conversations/",
                                                  ) &&
                                                  url.endsWith("/segments/text")
                                                ? 404
                                                : url.endsWith(
                                                      "/v1/users/assistant-settings",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/users/ai-profile",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/users/training-data-opt-in",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/users/developer/webhooks/status",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/users/profile",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/users/daily-summary-settings",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/users/mentor-notification-settings",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/daily-score",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/scores",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/focus-sessions",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/focus-stats",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/screen-activity",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/screen-activity/summary",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/calendar/onboarding/status",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/users/geolocation",
                                                    ) ||
                                                    url.includes(
                                                      "/v1/users/me/usage?",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/users/me/subscription",
                                                    ) ||
                                                    url.endsWith(
                                                      "/v1/payments/available-plans",
                                                    )
                                                  ? 200
                                                  : 400;
      return new Response(null, { status });
    };

    const result = await runSmoke({
      edgeUrl: "https://edge.example.test",
      token: "token",
      fetchImpl,
    });

    expect(result).toEqual({
      edgeHealth: 200,
      announcementsGeneral: 200,
      appReviews: 200,
      unauthenticatedProbe: 401,
      unauthenticatedAnnouncements: 401,
      unauthenticatedAnnouncementsAdmin: 403,
      unauthenticatedAsyncTranscription: 401,
      authenticatedProbe: 200,
      accountCutover: 200,
      appSearch: 200,
      conversations: 200,
      conversationSearch: 200,
      webProxyConversations: 200,
      webProxyEnabledApps: 200,
      webProxyMemories: 200,
      workersAiChatHistoryPreflight: 200,
      webProxyWorkersAiChat: 200,
      workersAiChatCleanup: 200,
      memories: 200,
      folders: 200,
      conversationCount: 200,
      conversationPhotos: 404,
      conversationTranscripts: 404,
      conversationAnalytics: 404,
      conversationEvents: 404,
      conversationSummary: 404,
      conversationCalendarUnlink: 404,
      conversationActionItems: 404,
      conversationActionItemDescription: 404,
      conversationActionItemDelete: 404,
      conversationRecording: 404,
      segmentText: 404,
      assistantSettings: 200,
      aiProfile: 200,
      trainingDataOptIn: 200,
      developerWebhooksStatus: 200,
      userProfile: 200,
      dailySummarySettings: 200,
      mentorNotificationSettings: 200,
      dailyScore: 200,
      scores: 200,
      focusSessions: 200,
      focusStats: 200,
      screenActivity: 200,
      screenActivitySummary: 200,
      calendarOnboardingStatus: 200,
      accountUsage: 200,
      accountSubscription: 200,
      availablePlans: 200,
      invalidGeolocation: 200,
      workersAiEmptyAudio: 400,
      voiceMessageEmptyAudio: 400,
    });
    expect(calls).toHaveLength(52);
    expect(
      calls.find((call) => call.url.endsWith("/v1/conversations/search"))?.init
        ?.method,
    ).toBe("POST");
    expect(
      calls.find((call) => call.url.endsWith("/v1/users/geolocation"))?.init
        ?.method,
    ).toBe("PATCH");
    expect(
      calls.find((call) => call.url.endsWith("/v1/stt/transcribe-workers-ai"))
        ?.init?.method,
    ).toBe("POST");
    expect(
      calls.find((call) => call.url.endsWith("/v2/voice-message/transcribe"))
        ?.init?.method,
    ).toBe("POST");
    expect(
      calls.find((call) => call.url.endsWith("/api/proxy/v2/messages"))?.init
        ?.method,
    ).toBe("POST");
    expect(
      calls
        .slice()
        .reverse()
        .find((call) => call.url.endsWith("/api/proxy/v2/messages"))?.init
        ?.method,
    ).toBe("DELETE");
  });

  it("fails when staging authentication is not bound to the Cloudflare data plane", async () => {
    const fetchImpl = async (url: string, init?: RequestInit) => {
      if (
        url.endsWith("/health") ||
        url.endsWith("/v1/announcements/general") ||
        url.endsWith("/v1/apps/cf-staging-search-app/reviews")
      ) {
        return new Response(null, { status: 200 });
      }
      if (url.endsWith("/v1/cf/probe")) {
        return new Response(null, { status: init?.headers ? 200 : 401 });
      }
      if (url.includes("/v1/announcements/pending")) {
        return new Response(null, { status: 401 });
      }
      if (url.endsWith("/v1/announcements/all")) {
        return new Response(null, { status: 403 });
      }
      if (url.endsWith("/v1/stt/transcribe-async")) {
        return new Response(null, { status: 401 });
      }
      if (url.endsWith("/v1/account/cutover/control")) {
        return Response.json({
          state: "legacy",
          product_traffic_allowed: true,
          migration: { destination_backend_bound: false },
        });
      }
      return new Response(null, { status: 500 });
    };

    await expect(
      runSmoke({
        edgeUrl: "https://edge.example.test",
        token: "token",
        fetchImpl,
      }),
    ).rejects.toThrow("not bound to the Cloudflare data plane");
  });

  it("fails when a user-facing Web API path cannot reach Edge", async () => {
    const fetchImpl = async (url: string, init?: RequestInit) => {
      if (url.endsWith("/v1/cf/probe")) {
        return new Response(null, { status: init?.headers ? 200 : 401 });
      }
      if (url.includes("/v1/announcements/pending")) {
        return new Response(null, { status: 401 });
      }
      if (url.endsWith("/v1/announcements/all")) {
        return new Response(null, { status: 403 });
      }
      if (url.endsWith("/v1/stt/transcribe-async")) {
        return new Response(null, { status: 401 });
      }
      if (url.endsWith("/v1/account/cutover/control")) {
        return Response.json({
          state: "new",
          product_traffic_allowed: true,
          migration: { destination_backend_bound: true },
        });
      }
      if (url.includes("/api/proxy/v1/apps/enabled")) {
        return new Response("error code: 1042", { status: 404 });
      }
      return new Response(null, { status: 200 });
    };

    await expect(
      runSmoke({
        edgeUrl: "https://edge.example.test",
        webUrl: "https://web.example.test",
        token: "token",
        fetchImpl,
      }),
    ).rejects.toThrow(
      "Web to Edge enabled apps service binding expected HTTP 200, received HTTP 404",
    );
  });

  it("can opt into a real native TTS response check", async () => {
    const fetchImpl = async (url: string, init?: RequestInit) => {
      if (url.endsWith("/health")) return new Response(null, { status: 200 });
      if (url.endsWith("/v1/announcements/general"))
        return new Response(null, { status: 200 });
      if (url.endsWith("/v1/cf/probe"))
        return new Response(null, { status: init?.headers ? 200 : 401 });
      if (url.includes("/v1/announcements/pending"))
        return new Response(null, { status: 401 });
      if (url.endsWith("/v1/announcements/all"))
        return new Response(null, { status: 403 });
      if (url.endsWith("/v1/stt/transcribe-async"))
        return new Response(null, { status: 401 });
      if (url.endsWith("/v1/account/cutover/control")) {
        return Response.json({
          state: "new",
          product_traffic_allowed: true,
          migration: { destination_backend_bound: true },
        });
      }
      if (url.endsWith("/api/proxy/v2/messages") && init?.method === "POST") {
        return workersAiChatResponse();
      }
      if (url.endsWith("/api/proxy/v2/messages") && init?.method === "DELETE") {
        return Response.json({ status: "ok" });
      }
      if (url.includes("/api/proxy/v2/messages?limit=1")) {
        return Response.json([{ id: "cf-initial-chat" }]);
      }
      if (url.includes("/v2/apps/search"))
        return new Response(null, { status: 200 });
      if (url.includes("/v1/apps/enabled"))
        return new Response(null, { status: 200 });
      if (url.includes("/v3/memories?"))
        return new Response(null, { status: 200 });
      if (url.endsWith("/v1/folders"))
        return new Response(null, { status: 200 });
      if (
        url.includes("/v1/conversations?") ||
        url.endsWith("/v1/conversations") ||
        url.endsWith("/v1/conversations/count")
      ) {
        return new Response(null, { status: 200 });
      }
      if (url.includes("/v1/conversations/") && url.endsWith("/photos"))
        return new Response(null, { status: 404 });
      if (url.includes("/v1/conversations/") && url.endsWith("/transcripts"))
        return new Response(null, { status: 404 });
      if (url.includes("/v1/conversations/") && url.endsWith("/analytics"))
        return new Response(null, { status: 404 });
      if (url.includes("/v1/conversations/") && url.endsWith("/events"))
        return new Response(null, { status: 404 });
      if (url.includes("/v1/conversations/") && url.endsWith("/summary"))
        return new Response(null, { status: 404 });
      if (url.includes("/v1/conversations/") && url.endsWith("/calendar-event"))
        return new Response(null, { status: 404 });
      if (url.includes("/v1/conversations/") && url.endsWith("/action-items"))
        return new Response(null, { status: 404 });
      if (url.includes("/v1/conversations/") && url.endsWith("/action-items/0"))
        return new Response(null, { status: 404 });
      if (url.includes("/v1/conversations/") && url.endsWith("/recording"))
        return new Response(null, { status: 404 });
      if (url.includes("/v1/conversations/") && url.endsWith("/segments/text"))
        return new Response(null, { status: 404 });
      if (
        url.endsWith("/v1/users/assistant-settings") ||
        url.endsWith("/v1/users/ai-profile") ||
        url.endsWith("/v1/users/training-data-opt-in") ||
        url.endsWith("/v1/users/developer/webhooks/status") ||
        url.endsWith("/v1/users/profile") ||
        url.endsWith("/v1/users/daily-summary-settings") ||
        url.endsWith("/v1/users/mentor-notification-settings") ||
        url.endsWith("/v1/daily-score") ||
        url.endsWith("/v1/scores") ||
        url.endsWith("/v1/focus-sessions") ||
        url.endsWith("/v1/focus-stats") ||
        url.endsWith("/v1/screen-activity") ||
        url.endsWith("/v1/screen-activity/summary") ||
        url.endsWith("/v1/calendar/onboarding/status") ||
        url.endsWith("/v1/users/geolocation") ||
        url.includes("/v1/users/me/usage?") ||
        url.endsWith("/v1/users/me/subscription") ||
        url.endsWith("/v1/payments/available-plans")
      ) {
        return new Response(null, { status: 200 });
      }
      if (
        url.endsWith("/v1/stt/transcribe-workers-ai") ||
        url.endsWith("/v2/voice-message/transcribe")
      )
        return new Response(null, { status: 400 });
      return new Response(new Uint8Array([0xff, 0xfb]), {
        status: 200,
        headers: { "content-type": "audio/mpeg" },
      });
    };

    await expect(
      runSmoke({
        edgeUrl: "https://edge.example.test",
        token: "token",
        nativeTts: true,
        fetchImpl,
      }),
    ).resolves.toMatchObject({ nativeTts: 200, nativeTtsBytes: 2 });
  });

  it("cleans up chat state when the completion frame is invalid", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (url.endsWith("/v1/cf/probe")) {
        return new Response(null, { status: init?.headers ? 200 : 401 });
      }
      if (url.includes("/v1/announcements/pending")) {
        return new Response(null, { status: 401 });
      }
      if (url.endsWith("/v1/announcements/all")) {
        return new Response(null, { status: 403 });
      }
      if (url.endsWith("/v1/stt/transcribe-async")) {
        return new Response(null, { status: 401 });
      }
      if (url.endsWith("/v1/account/cutover/control")) {
        return Response.json({
          state: "new",
          product_traffic_allowed: true,
          migration: { destination_backend_bound: true },
        });
      }
      if (url.endsWith("/api/proxy/v2/messages") && init?.method === "POST") {
        return new Response("data: partial\n\ndone: invalid\n\n", {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        });
      }
      if (url.endsWith("/api/proxy/v2/messages") && init?.method === "DELETE") {
        return Response.json({ status: "ok" });
      }
      if (url.includes("/api/proxy/v2/messages?limit=1")) {
        return Response.json([{ id: "cf-initial-chat" }]);
      }
      return new Response(null, { status: 200 });
    };

    await expect(
      runSmoke({
        edgeUrl: "https://edge.example.test",
        webUrl: "https://web.example.test",
        token: "token",
        fetchImpl,
      }),
    ).rejects.toThrow("invalid completion frame");
    expect(
      calls
        .slice()
        .reverse()
        .find((call) => call.url.endsWith("/api/proxy/v2/messages"))?.init
        ?.method,
    ).toBe("DELETE");
  });

  it("refuses to overwrite an account with existing chat history", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (url.endsWith("/v1/cf/probe")) {
        return new Response(null, { status: init?.headers ? 200 : 401 });
      }
      if (url.includes("/v1/announcements/pending")) {
        return new Response(null, { status: 401 });
      }
      if (url.endsWith("/v1/announcements/all")) {
        return new Response(null, { status: 403 });
      }
      if (url.endsWith("/v1/stt/transcribe-async")) {
        return new Response(null, { status: 401 });
      }
      if (url.endsWith("/v1/account/cutover/control")) {
        return Response.json({
          state: "new",
          product_traffic_allowed: true,
          migration: { destination_backend_bound: true },
        });
      }
      if (url.includes("/api/proxy/v2/messages?limit=1")) {
        return Response.json([{ id: "existing-message", sender: "human" }]);
      }
      return new Response(null, { status: 200 });
    };

    await expect(
      runSmoke({
        edgeUrl: "https://edge.example.test",
        webUrl: "https://web.example.test",
        token: "token",
        fetchImpl,
      }),
    ).rejects.toThrow("dedicated account with empty chat history");
    expect(
      calls.some(
        (call) =>
          call.url.endsWith("/api/proxy/v2/messages") &&
          ["POST", "DELETE"].includes(call.init?.method || ""),
      ),
    ).toBe(false);
  });
});
