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

function unauthenticatedMemoryBatchBoundary(
  url: string,
  init?: RequestInit,
): Response | null {
  if (
    url.endsWith("/v3/memories/batch") &&
    init?.method === "POST" &&
    !new Headers(init.headers).has("authorization")
  ) {
    return new Response(null, { status: 401 });
  }
  return null;
}

function firstPartyConversationWriteBoundary(
  url: string,
  init?: RequestInit,
): Response | null {
  if (
    url.endsWith("/v1/conversations/from-segments") &&
    init?.method === "POST"
  ) {
    return new Response(null, { status: 422 });
  }
  return null;
}

function retrievalToolBoundary(
  url: string,
  init?: RequestInit,
): Response | null {
  const path = new URL(url).pathname;
  if (path === "/v1/knowledge-graph") {
    return init?.method === "DELETE"
      ? Response.json({ detail: "canonical graph" }, { status: 409 })
      : Response.json({ nodes: [], edges: [] });
  }
  if (path === "/v1/knowledge-graph/canonical") {
    return Response.json({
      nodes: [],
      edges: [],
      catalog_nodes: [],
      has_more: false,
      next_cursor: null,
    });
  }
  if (path === "/v1/knowledge-graph/rebuild") {
    return Response.json({ detail: "canonical graph" }, { status: 409 });
  }
  if (path === "/v1/knowledge-graph/extract") {
    return Response.json({ detail: "invalid extraction" }, { status: 400 });
  }
  if (
    path === "/v1/memories/extract" ||
    path === "/v1/conversations/topic" ||
    path === "/v1/connectors/synthesize"
  ) {
    return Response.json({ detail: "invalid synthesis" }, { status: 422 });
  }
  if (path === "/v1/users/ai-profile/synthesize") {
    return Response.json(
      { detail: "invalid ai profile synthesis" },
      { status: 422 },
    );
  }
  if (path === "/v1/goals/extract-progress") {
    return Response.json(
      { detail: "invalid goal progress extraction" },
      { status: 422 },
    );
  }
  if (path === "/v1/goals/suggest") {
    return Response.json({ suggested_title: "Test goal" });
  }
  if (path === "/v1/goals/advice") {
    return Response.json({ advice: "Set a goal." });
  }
  if (path === "/v1/goals/cf-smoke-missing/advice") {
    return Response.json({ detail: "Goal not found" }, { status: 404 });
  }
  if (path === "/v1/action-items/search") {
    return Response.json({ action_items: [] });
  }
  const toolNames: Record<string, string> = {
    "/v1/tools/conversations": "get_conversations",
    "/v1/tools/conversations/search": "search_conversations",
    "/v1/tools/conversations/search-chunks": "search_conversation_chunks",
    "/v1/tools/memories": "get_memories",
    "/v1/tools/memories/search": "search_memories",
    "/v1/tools/action-items": "get_action_items",
  };
  const toolName = toolNames[path];
  return toolName
    ? Response.json({
        tool_name: toolName,
        result_text: "No results found.",
        is_error: false,
        sources: [],
      })
    : null;
}

function creatorPaymentBoundary(
  url: string,
  init?: RequestInit,
): Response | null {
  if (url.endsWith("/v1/stripe/connect/webhook")) {
    return new Response(null, { status: 503 });
  }
  if (url.endsWith("/v1/stripe/supported-countries")) {
    return new Response(null, { status: 503 });
  }
  if (url.includes("/v1/stripe/return/")) {
    return new Response(null, { status: 404 });
  }
  if (url.includes("/v1/stripe/refresh/")) {
    return new Response(null, { status: 403 });
  }
  if (url.includes("/v1/stripe/connect-accounts?")) {
    return new Response(null, { status: 400 });
  }
  if (url.endsWith("/v1/apps/cf-smoke-missing/subscription")) {
    return new Response(null, {
      status: init?.method === "DELETE" ? 404 : 200,
    });
  }
  if (
    url.endsWith("/v1/apps/cf-smoke-missing") &&
    (init?.method === "DELETE" || init?.method === "PATCH")
  ) {
    return new Response(null, { status: 404 });
  }
  if (
    url.includes("/v1/apps/cf-smoke-missing/change-visibility?") ||
    url.endsWith("/v1/apps/cf-smoke-missing/refresh-manifest")
  ) {
    return new Response(null, { status: 404 });
  }
  if (url.endsWith("/v1/apps") && init?.method === "POST") {
    return new Response(null, { status: 400 });
  }
  if (url.includes("/v1/apps/cf-smoke-missing/logo/")) {
    return new Response(null, { status: 404 });
  }
  if (url.includes("/v1/apps/cf-smoke-missing/keys")) {
    return new Response(null, { status: 404 });
  }
  if (url.endsWith("/v1/mcp/keys/cf-smoke-missing-key")) {
    return new Response(null, { status: 204 });
  }
  if (url.endsWith("/v1/mcp/keys")) {
    return new Response(null, { status: init?.method === "POST" ? 422 : 200 });
  }
  if (url.endsWith("/v1/mcp/memories")) {
    return new Response(null, { status: 403 });
  }
  if (url.endsWith("/v1/dev/keys/cf-smoke-missing-key")) {
    return new Response(null, { status: 204 });
  }
  if (url.endsWith("/v1/dev/keys")) {
    return new Response(null, { status: init?.method === "POST" ? 422 : 200 });
  }
  if (url.endsWith("/v1/dev/user/memories")) {
    return new Response(null, { status: 403 });
  }
  if (url.endsWith("/v1/dev/user/action-items")) {
    return new Response(null, { status: 403 });
  }
  if (url.endsWith("/v1/dev/user/conversations/cf-smoke-missing")) {
    return new Response(null, { status: 403 });
  }
  if (url.endsWith("/v1/dev/user/goals")) {
    return new Response(null, { status: 403 });
  }
  if (
    url.endsWith("/v1/integrations/notification") ||
    url.includes("/v2/integrations/cf-smoke-missing/")
  ) {
    return new Response(null, { status: 403 });
  }
  if (
    url.endsWith("/v1/stripe/onboarded") ||
    url.endsWith("/v1/paypal/payment-details") ||
    url.endsWith("/v1/payment-methods/default") ||
    url.endsWith("/v1/payment-methods/status")
  ) {
    return new Response(null, { status: 200 });
  }
  return null;
}

function publicCompatibilityRoute(
  url: string,
  init?: RequestInit,
): Response | null {
  if (url.endsWith("/v1/health")) {
    return new Response(
      init?.method === "HEAD" ? null : JSON.stringify({ status: "ok" }),
      {
        status: 200,
        headers: { "content-type": "application/json; charset=UTF-8" },
      },
    );
  }
  if (url.endsWith("/.well-known/apple-developer-domain-association.txt")) {
    return new Response("", {
      status: 200,
      headers: { "content-type": "text/plain; charset=UTF-8" },
    });
  }
  if (url.endsWith("/.well-known/openai-apps-challenge")) {
    return new Response("ZsVB_wpc4R35_tHloCZCokY6H2fBkKyBJrz-4MtXjYE", {
      status: 200,
      headers: { "content-type": "text/plain; charset=UTF-8" },
    });
  }
  if (url.endsWith("/v1/daily-summaries/cf-smoke-missing/shared")) {
    return new Response(null, { status: 404 });
  }
  return null;
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
      const publicCompatibility = publicCompatibilityRoute(url, init);
      if (publicCompatibility) return publicCompatibility;
      const memoryBatch = unauthenticatedMemoryBatchBoundary(url, init);
      if (memoryBatch) return memoryBatch;
      const conversationWrite = firstPartyConversationWriteBoundary(url, init);
      if (conversationWrite) return conversationWrite;
      const retrievalTool = retrievalToolBoundary(url, init);
      if (retrievalTool) return retrievalTool;
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
      if (url.includes("/v1/users/analytics/memory_summary?")) {
        return Response.json({ has_rating: false });
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
      if (url.endsWith("/v1/users/stats/chat-messages")) {
        return Response.json({ count: 0 });
      }
      if (url.includes("/v2/chat-sessions?")) {
        return Response.json([]);
      }
      if (url.includes("/v2/desktop/messages?")) {
        return Response.json([]);
      }
      if (
        url.endsWith("/v2/messages/cf-smoke-missing/report") ||
        url.endsWith("/v2/desktop/messages/cf-smoke-missing/rating")
      ) {
        return new Response(null, { status: 404 });
      }
      if (url.includes("/v1/apps/enabled")) {
        return new Response(null, { status: 200 });
      }
      if (
        url.includes("/v1/apps/cf-smoke-missing/change-visibility?") ||
        url.endsWith("/v1/apps/cf-smoke-missing/refresh-manifest")
      ) {
        return new Response(null, { status: 404 });
      }
      if (url.endsWith("/v1/stripe/webhook")) {
        return new Response(null, { status: 503 });
      }
      const creatorPayment = creatorPaymentBoundary(url, init);
      if (creatorPayment) return creatorPayment;
      if (
        url.endsWith("/v1/payments/checkout-session") ||
        url.endsWith("/v1/payments/customer-portal") ||
        url.endsWith("/v1/payments/upgrade-subscription") ||
        url.endsWith("/v1/payments/subscription")
      ) {
        return new Response(null, { status: 400 });
      }
      const status = url.endsWith("/health")
        ? 200
        : url.endsWith("/v1/announcements/general")
          ? 200
          : url.endsWith("/v1/apps/cf-staging-search-app/reviews")
            ? 200
            : url.includes("/v1/payments/success?") ||
                url.endsWith("/v1/payments/cancel") ||
                url.endsWith("/v1/payments/portal-return")
              ? 200
              : url.endsWith("/v1/cf/probe")
                ? init?.headers
                  ? 200
                  : 401
                : url.includes("/v1/announcements/pending")
                  ? 401
                  : url.endsWith("/v1/announcements/all")
                    ? 403
                    : url.endsWith("/v1/admin/fair-use/flagged")
                      ? 403
                      : url.endsWith("/v1/stt/transcribe-async") ||
                          url.includes("/v2/sync-local-files")
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
                                          : url.includes(
                                                "/v1/conversations/",
                                              ) &&
                                              url.endsWith("/calendar-event")
                                            ? 404
                                            : url.includes(
                                                  "/v1/conversations/",
                                                ) &&
                                                url.endsWith("/action-items")
                                              ? 404
                                              : url.includes(
                                                    "/v1/conversations/",
                                                  ) &&
                                                  url.endsWith(
                                                    "/action-items/0",
                                                  )
                                                ? 404
                                                : url.includes(
                                                      "/v1/conversations/",
                                                    ) &&
                                                    url.endsWith("/recording")
                                                  ? 404
                                                  : url.includes(
                                                        "/v1/conversations/",
                                                      ) &&
                                                      url.endsWith(
                                                        "/segments/text",
                                                      )
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
                                                        url.includes(
                                                          "/v1/advice?",
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
                                                          "/v1/users/me/usage-quota",
                                                        ) ||
                                                        url.endsWith(
                                                          "/v1/users/me/paywall",
                                                        ) ||
                                                        url.endsWith(
                                                          "/v1/users/me/trial",
                                                        ) ||
                                                        url.includes(
                                                          "/v1/users/me/llm-usage?",
                                                        ) ||
                                                        url.includes(
                                                          "/v1/users/me/llm-usage/top-features?",
                                                        ) ||
                                                        url.endsWith(
                                                          "/v1/users/me/llm-usage/total",
                                                        ) ||
                                                        url.endsWith(
                                                          "/v1/payments/available-plans",
                                                        ) ||
                                                        url.endsWith(
                                                          "/v1/payments/overage-info",
                                                        ) ||
                                                        url.endsWith(
                                                          "/v1/fair-use/status",
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
      v1Health: 200,
      v1HealthHead: 200,
      appleAssociation: 200,
      openAiAppsChallenge: 200,
      announcementsGeneral: 200,
      sharedDailySummary: 404,
      appReviews: 200,
      paymentSuccess: 200,
      paymentCancel: 200,
      paymentPortalReturn: 200,
      stripeWebhookBoundary: 503,
      stripeConnectWebhookBoundary: 503,
      stripeSupportedCountriesBoundary: 503,
      stripeReturnMissing: 404,
      stripeBrowserRefreshBoundary: 403,
      mcpDataInvalidKey: 403,
      developerDataInvalidKey: 403,
      developerMemoryWriteInvalidKey: 403,
      developerActionItemWriteInvalidKey: 403,
      unauthenticatedProbe: 401,
      unauthenticatedAnnouncements: 401,
      unauthenticatedAnnouncementsAdmin: 403,
      unauthenticatedFairUseAdmin: 403,
      unauthenticatedAsyncTranscription: 401,
      unauthenticatedSyncUpload: 401,
      unauthenticatedSyncStatus: 401,
      unauthenticatedMemoryBatch: 401,
      authenticatedProbe: 200,
      accountCutover: 200,
      appSearch: 200,
      memorySummaryFeedback: 200,
      conversations: 200,
      conversationSearch: 200,
      actionItemSearch: 200,
      toolConversations: 200,
      toolConversationSearch: 200,
      toolConversationChunkSearch: 200,
      toolMemories: 200,
      toolMemorySearch: 200,
      toolActionItems: 200,
      knowledgeGraph: 200,
      canonicalKnowledgeGraph: 200,
      knowledgeGraphRebuildFence: 409,
      knowledgeGraphDeleteFence: 409,
      knowledgeGraphExtractValidation: 400,
      memory_extraction_validation: 422,
      conversation_topic_validation: 422,
      connector_synthesis_validation: 422,
      aiProfileSynthesisValidation: 422,
      goalProgressExtractionValidation: 422,
      goalSuggestion: 200,
      currentGoalAdvice: 200,
      missingGoalAdvice: 404,
      conversationFromSegmentsValidation: 422,
      webProxyConversations: 200,
      webProxyEnabledApps: 200,
      webProxyMemories: 200,
      workersAiChatHistoryPreflight: 200,
      webProxyWorkersAiChat: 200,
      workersAiChatCleanup: 200,
      chatMessageCount: 200,
      chatSessions: 200,
      desktopMessages: 200,
      missingChatReport: 404,
      missingDesktopRating: 404,
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
      advice: 200,
      focusSessions: 200,
      focusStats: 200,
      screenActivity: 200,
      screenActivitySummary: 200,
      calendarOnboardingStatus: 200,
      accountUsage: 200,
      accountSubscription: 200,
      llmUsage: 200,
      llmTopFeatures: 200,
      llmTotalCost: 200,
      availablePlans: 200,
      overageInfo: 200,
      checkoutBoundary: 400,
      customerPortalBoundary: 400,
      upgradeBoundary: 400,
      cancelSubscriptionBoundary: 400,
      appSubscription: 200,
      cancelAppSubscriptionBoundary: 404,
      appDeleteBoundary: 404,
      appCreateBoundary: 400,
      appUpdateBoundary: 404,
      appVisibilityBoundary: 404,
      appManifestRefreshBoundary: 404,
      appLogoBoundary: 404,
      appApiKeyCreateBoundary: 404,
      appApiKeyListBoundary: 404,
      appApiKeyDeleteBoundary: 404,
      mcpApiKeyList: 200,
      mcpApiKeyValidation: 422,
      mcpApiKeyDeleteMissing: 204,
      developerApiKeyList: 200,
      developerApiKeyValidation: 422,
      developerApiKeyDeleteMissing: 204,
      developerConversationWriteInvalidKey: 403,
      developerGoalWriteInvalidKey: 403,
      integrationNotificationV1Boundary: 403,
      integrationConversationCreateBoundary: 403,
      integrationMemoryCreateBoundary: 403,
      integrationMemoriesBoundary: 403,
      integrationConversationsBoundary: 403,
      integrationConversationSearchBoundary: 403,
      integrationNotificationV2Boundary: 403,
      integrationTasksBoundary: 403,
      connectAccountBoundary: 400,
      stripeOnboardingStatus: 200,
      stripeRefreshOwnership: 403,
      paypalSave: 200,
      paypalDetails: 200,
      defaultPaymentMethod: 200,
      paymentMethodStatus: 200,
      fairUseStatus: 200,
      invalidGeolocation: 200,
      workersAiEmptyAudio: 400,
      voiceMessageEmptyAudio: 400,
    });
    expect(calls).toHaveLength(147);
    expect(
      calls.find((call) =>
        call.url.includes("/v1/users/analytics/memory_summary?"),
      )?.init?.method,
    ).toBeUndefined();
    expect(
      calls.find((call) => call.url.endsWith("/v1/conversations/search"))?.init
        ?.method,
    ).toBe("POST");
    expect(
      calls.find((call) => call.url.endsWith("/v1/conversations/from-segments"))
        ?.init?.method,
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
      const publicCompatibility = publicCompatibilityRoute(url, init);
      if (publicCompatibility) return publicCompatibility;
      const memoryBatch = unauthenticatedMemoryBatchBoundary(url, init);
      if (memoryBatch) return memoryBatch;
      const conversationWrite = firstPartyConversationWriteBoundary(url, init);
      if (conversationWrite) return conversationWrite;
      const retrievalTool = retrievalToolBoundary(url, init);
      if (retrievalTool) return retrievalTool;
      if (
        url.endsWith("/health") ||
        url.endsWith("/v1/announcements/general") ||
        url.endsWith("/v1/apps/cf-staging-search-app/reviews") ||
        url.includes("/v1/payments/success?") ||
        url.endsWith("/v1/payments/cancel") ||
        url.endsWith("/v1/payments/portal-return")
      ) {
        return new Response(null, { status: 200 });
      }
      if (url.endsWith("/v1/cf/probe")) {
        return new Response(null, { status: init?.headers ? 200 : 401 });
      }
      if (url.endsWith("/v1/stripe/webhook")) {
        return new Response(null, { status: 503 });
      }
      const creatorPayment = creatorPaymentBoundary(url, init);
      if (creatorPayment) return creatorPayment;
      if (url.includes("/v1/announcements/pending")) {
        return new Response(null, { status: 401 });
      }
      if (url.endsWith("/v1/announcements/all")) {
        return new Response(null, { status: 403 });
      }
      if (url.endsWith("/v1/admin/fair-use/flagged")) {
        return new Response(null, { status: 403 });
      }
      if (
        url.endsWith("/v1/stt/transcribe-async") ||
        url.includes("/v2/sync-local-files")
      ) {
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
      const publicCompatibility = publicCompatibilityRoute(url, init);
      if (publicCompatibility) return publicCompatibility;
      const memoryBatch = unauthenticatedMemoryBatchBoundary(url, init);
      if (memoryBatch) return memoryBatch;
      const conversationWrite = firstPartyConversationWriteBoundary(url, init);
      if (conversationWrite) return conversationWrite;
      const retrievalTool = retrievalToolBoundary(url, init);
      if (retrievalTool) return retrievalTool;
      if (url.includes("/v1/users/analytics/memory_summary?")) {
        return Response.json({ has_rating: false });
      }
      if (url.endsWith("/v1/cf/probe")) {
        return new Response(null, { status: init?.headers ? 200 : 401 });
      }
      if (url.endsWith("/v1/stripe/webhook")) {
        return new Response(null, { status: 503 });
      }
      const creatorPayment = creatorPaymentBoundary(url, init);
      if (creatorPayment) return creatorPayment;
      if (url.includes("/v1/announcements/pending")) {
        return new Response(null, { status: 401 });
      }
      if (url.endsWith("/v1/announcements/all")) {
        return new Response(null, { status: 403 });
      }
      if (url.endsWith("/v1/admin/fair-use/flagged")) {
        return new Response(null, { status: 403 });
      }
      if (
        url.endsWith("/v1/stt/transcribe-async") ||
        url.includes("/v2/sync-local-files")
      ) {
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
      const publicCompatibility = publicCompatibilityRoute(url, init);
      if (publicCompatibility) return publicCompatibility;
      const memoryBatch = unauthenticatedMemoryBatchBoundary(url, init);
      if (memoryBatch) return memoryBatch;
      const conversationWrite = firstPartyConversationWriteBoundary(url, init);
      if (conversationWrite) return conversationWrite;
      const retrievalTool = retrievalToolBoundary(url, init);
      if (retrievalTool) return retrievalTool;
      if (url.includes("/v1/users/analytics/memory_summary?")) {
        return Response.json({ has_rating: false });
      }
      if (url.endsWith("/health")) return new Response(null, { status: 200 });
      if (url.endsWith("/v1/announcements/general"))
        return new Response(null, { status: 200 });
      if (
        url.endsWith("/v1/apps/cf-staging-search-app/reviews") ||
        url.includes("/v1/payments/success?") ||
        url.endsWith("/v1/payments/cancel") ||
        url.endsWith("/v1/payments/portal-return")
      )
        return new Response(null, { status: 200 });
      if (url.endsWith("/v1/cf/probe"))
        return new Response(null, { status: init?.headers ? 200 : 401 });
      if (url.endsWith("/v1/stripe/webhook"))
        return new Response(null, { status: 503 });
      const creatorPayment = creatorPaymentBoundary(url, init);
      if (creatorPayment) return creatorPayment;
      if (url.includes("/v1/announcements/pending"))
        return new Response(null, { status: 401 });
      if (url.endsWith("/v1/announcements/all"))
        return new Response(null, { status: 403 });
      if (url.endsWith("/v1/admin/fair-use/flagged"))
        return new Response(null, { status: 403 });
      if (
        url.endsWith("/v1/stt/transcribe-async") ||
        url.includes("/v2/sync-local-files")
      )
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
      if (url.endsWith("/v1/users/stats/chat-messages")) {
        return Response.json({ count: 0 });
      }
      if (url.includes("/v2/chat-sessions?")) {
        return Response.json([]);
      }
      if (url.includes("/v2/desktop/messages?")) {
        return Response.json([]);
      }
      if (
        url.endsWith("/v2/messages/cf-smoke-missing/report") ||
        url.endsWith("/v2/desktop/messages/cf-smoke-missing/rating")
      ) {
        return new Response(null, { status: 404 });
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
        url.includes("/v1/advice?") ||
        url.endsWith("/v1/focus-sessions") ||
        url.endsWith("/v1/focus-stats") ||
        url.endsWith("/v1/screen-activity") ||
        url.endsWith("/v1/screen-activity/summary") ||
        url.endsWith("/v1/calendar/onboarding/status") ||
        url.endsWith("/v1/users/geolocation") ||
        url.includes("/v1/users/me/usage?") ||
        url.endsWith("/v1/users/me/subscription") ||
        url.endsWith("/v1/users/me/usage-quota") ||
        url.endsWith("/v1/users/me/paywall") ||
        url.endsWith("/v1/users/me/trial") ||
        url.endsWith("/v1/payments/available-plans") ||
        url.endsWith("/v1/fair-use/status")
      ) {
        return new Response(null, { status: 200 });
      }
      if (
        url.endsWith("/v1/stt/transcribe-workers-ai") ||
        url.endsWith("/v2/voice-message/transcribe") ||
        url.endsWith("/v1/payments/checkout-session") ||
        url.endsWith("/v1/payments/customer-portal") ||
        url.endsWith("/v1/payments/upgrade-subscription") ||
        url.endsWith("/v1/payments/subscription")
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
      const publicCompatibility = publicCompatibilityRoute(url, init);
      if (publicCompatibility) return publicCompatibility;
      const memoryBatch = unauthenticatedMemoryBatchBoundary(url, init);
      if (memoryBatch) return memoryBatch;
      const conversationWrite = firstPartyConversationWriteBoundary(url, init);
      if (conversationWrite) return conversationWrite;
      const retrievalTool = retrievalToolBoundary(url, init);
      if (retrievalTool) return retrievalTool;
      if (url.includes("/v1/users/analytics/memory_summary?")) {
        return Response.json({ has_rating: false });
      }
      if (url.endsWith("/v1/cf/probe")) {
        return new Response(null, { status: init?.headers ? 200 : 401 });
      }
      if (url.endsWith("/v1/stripe/webhook")) {
        return new Response(null, { status: 503 });
      }
      const creatorPayment = creatorPaymentBoundary(url, init);
      if (creatorPayment) return creatorPayment;
      if (url.includes("/v1/announcements/pending")) {
        return new Response(null, { status: 401 });
      }
      if (url.endsWith("/v1/announcements/all")) {
        return new Response(null, { status: 403 });
      }
      if (url.endsWith("/v1/admin/fair-use/flagged")) {
        return new Response(null, { status: 403 });
      }
      if (
        url.endsWith("/v1/stt/transcribe-async") ||
        url.includes("/v2/sync-local-files")
      ) {
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
      const publicCompatibility = publicCompatibilityRoute(url, init);
      if (publicCompatibility) return publicCompatibility;
      const memoryBatch = unauthenticatedMemoryBatchBoundary(url, init);
      if (memoryBatch) return memoryBatch;
      const conversationWrite = firstPartyConversationWriteBoundary(url, init);
      if (conversationWrite) return conversationWrite;
      const retrievalTool = retrievalToolBoundary(url, init);
      if (retrievalTool) return retrievalTool;
      if (url.includes("/v1/users/analytics/memory_summary?")) {
        return Response.json({ has_rating: false });
      }
      if (url.endsWith("/v1/cf/probe")) {
        return new Response(null, { status: init?.headers ? 200 : 401 });
      }
      if (url.endsWith("/v1/stripe/webhook")) {
        return new Response(null, { status: 503 });
      }
      const creatorPayment = creatorPaymentBoundary(url, init);
      if (creatorPayment) return creatorPayment;
      if (url.includes("/v1/announcements/pending")) {
        return new Response(null, { status: 401 });
      }
      if (url.endsWith("/v1/announcements/all")) {
        return new Response(null, { status: 403 });
      }
      if (url.endsWith("/v1/admin/fair-use/flagged")) {
        return new Response(null, { status: 403 });
      }
      if (
        url.endsWith("/v1/stt/transcribe-async") ||
        url.includes("/v2/sync-local-files")
      ) {
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
