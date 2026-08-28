import { describe, expect, it } from "vitest";
import {
  parseTokenPayload,
  resolveEdgeUrl,
  runSmoke,
} from "../scripts/smoke-staging.mjs";

describe("staging smoke helpers", () => {
  it("normalizes a valid edge URL and rejects unsupported protocols", () => {
    expect(resolveEdgeUrl("https://edge.example.test/")).toBe(
      "https://edge.example.test",
    );
    expect(() => resolveEdgeUrl("ftp://edge.example.test")).toThrow(
      "http or https",
    );
  });

  it("requires a non-empty token field", () => {
    expect(parseTokenPayload('{"token":"secret"}')).toBe("secret");
    expect(() => parseTokenPayload('{"token":""}')).toThrow("non-empty token");
  });

  it("checks public, auth, and billable-inference-free boundaries", async () => {
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
      const status = url.endsWith("/health")
        ? 200
        : url.endsWith("/v1/announcements/general")
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
                                      : url.includes("/v1/conversations/") &&
                                          url.endsWith("/action-items/0")
                                        ? 404
                                        : url.includes("/v1/conversations/") &&
                                            url.endsWith("/recording")
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
                                                url.endsWith("/v1/scores") ||
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
      unauthenticatedProbe: 401,
      unauthenticatedAnnouncements: 401,
      unauthenticatedAnnouncementsAdmin: 403,
      unauthenticatedAsyncTranscription: 401,
      authenticatedProbe: 200,
      accountCutover: 200,
      appSearch: 200,
      conversations: 200,
      memories: 200,
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
      invalidGeolocation: 200,
      workersAiEmptyAudio: 400,
    });
    expect(calls).toHaveLength(39);
    expect(
      calls.find((call) => call.url.endsWith("/v1/users/geolocation"))?.init
        ?.method,
    ).toBe("PATCH");
    expect(
      calls.find((call) => call.url.endsWith("/v1/stt/transcribe-workers-ai"))
        ?.init?.method,
    ).toBe("POST");
  });

  it("fails when staging authentication is not bound to the Cloudflare data plane", async () => {
    const fetchImpl = async (url: string, init?: RequestInit) => {
      if (
        url.endsWith("/health") ||
        url.endsWith("/v1/announcements/general")
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
      if (url.includes("/v2/apps/search"))
        return new Response(null, { status: 200 });
      if (url.includes("/v3/memories?"))
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
        url.endsWith("/v1/users/geolocation")
      ) {
        return new Response(null, { status: 200 });
      }
      if (url.endsWith("/v1/stt/transcribe-workers-ai"))
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
});
