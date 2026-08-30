import { readFile } from "node:fs/promises";

const DEFAULT_EDGE_URL =
  "https://omi-cf-edge-staging.summersmile1984.workers.dev";
const DEFAULT_WEB_URL =
  "https://omi-web-app-staging.summersmile1984.workers.dev";
const REQUEST_TIMEOUT_MS = 15_000;

export function resolveEdgeUrl(raw = process.env.CLOUDFLARE_EDGE_URL) {
  const value = (raw || DEFAULT_EDGE_URL).trim();
  if (!value) throw new Error("CLOUDFLARE_EDGE_URL must not be empty");
  const url = new URL(value);
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error("CLOUDFLARE_EDGE_URL must use http or https");
  }
  return url.toString().replace(/\/$/, "");
}

export function resolveWebUrl(raw = process.env.CLOUDFLARE_WEB_URL) {
  const value = (raw || DEFAULT_WEB_URL).trim();
  if (!value) throw new Error("CLOUDFLARE_WEB_URL must not be empty");
  const url = new URL(value);
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error("CLOUDFLARE_WEB_URL must use http or https");
  }
  return url.toString().replace(/\/$/, "");
}

export function parseTokenPayload(raw) {
  const payload = JSON.parse(raw);
  if (!payload || typeof payload.token !== "string" || !payload.token.trim()) {
    throw new Error("smoke token file must contain a non-empty token field");
  }
  return payload.token.trim();
}

export function assertAuthenticatedSmokeConfigured(env = process.env) {
  const direct = env.CLOUDFLARE_SMOKE_BEARER_TOKEN?.trim();
  const tokenFile = env.CLOUDFLARE_SMOKE_TOKEN_FILE?.trim();
  if (!direct && !tokenFile) {
    throw new Error(
      "staging release requires CLOUDFLARE_SMOKE_BEARER_TOKEN or CLOUDFLARE_SMOKE_TOKEN_FILE",
    );
  }
}

async function readOptionalBearerToken() {
  const direct = process.env.CLOUDFLARE_SMOKE_BEARER_TOKEN?.trim();
  if (direct) return direct;
  const tokenFile = process.env.CLOUDFLARE_SMOKE_TOKEN_FILE?.trim();
  if (!tokenFile) return null;
  return parseTokenPayload(await readFile(tokenFile, "utf8"));
}

async function request(fetchImpl, url, init = {}) {
  const response = await fetchImpl(url, {
    ...init,
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  // Consume each body so Node can reuse the connection for the next check.
  await response.arrayBuffer();
  return response;
}

async function requestJson(fetchImpl, url, init = {}) {
  const response = await fetchImpl(url, {
    ...init,
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    // Status validation owns the useful error for a non-JSON failure.
  }
  return { response, body };
}

function expectStatus(label, response, expected) {
  if (response.status !== expected) {
    throw new Error(
      `${label} expected HTTP ${expected}, received HTTP ${response.status}`,
    );
  }
}

function expectStatusIn(label, response, expected) {
  if (!expected.includes(response.status)) {
    throw new Error(
      `${label} expected HTTP ${expected.join(" or ")}, received HTTP ${response.status}`,
    );
  }
}

async function exerciseWorkersAiChat(fetchImpl, webBase, authHeaders) {
  const preflight = await requestJson(
    fetchImpl,
    `${webBase}/api/proxy/v2/messages?limit=1`,
    { headers: authHeaders },
  );
  expectStatus("Workers AI chat history preflight", preflight.response, 200);
  if (
    !Array.isArray(preflight.body) ||
    preflight.body.length !== 1 ||
    preflight.body[0]?.id !== "cf-initial-chat"
  ) {
    throw new Error(
      "Workers AI chat smoke requires a dedicated account with empty chat history",
    );
  }
  let chatStatus;
  let chatCleanup;
  try {
    const response = await fetchImpl(`${webBase}/api/proxy/v2/messages`, {
      method: "POST",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({
        text: "Reply with a short Cloudflare staging acknowledgement.",
        file_ids: [],
        context: null,
      }),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    chatStatus = response.status;
    const contentType = response.headers.get("content-type") || "";
    const body = await response.text();
    expectStatus("Web to Workers AI chat", response, 200);
    if (!contentType.includes("text/event-stream")) {
      throw new Error(
        `Web to Workers AI chat expected text/event-stream, received ${contentType || "missing content type"}`,
      );
    }
    const lines = body.split(/\r?\n/);
    const dataLines = lines.filter((line) => line.startsWith("data: "));
    const doneLines = lines.filter((line) => line.startsWith("done: "));
    if (
      dataLines.length === 0 ||
      !dataLines.some((line) => line.slice(6).trim())
    ) {
      throw new Error("Web to Workers AI chat returned no response data");
    }
    if (doneLines.length !== 1) {
      throw new Error(
        `Web to Workers AI chat expected one completion frame, received ${doneLines.length}`,
      );
    }
    let completedMessage;
    try {
      completedMessage = JSON.parse(
        Buffer.from(doneLines[0].slice(6).trim(), "base64").toString("utf8"),
      );
    } catch {
      throw new Error(
        "Web to Workers AI chat returned an invalid completion frame",
      );
    }
    if (
      completedMessage?.sender !== "ai" ||
      typeof completedMessage.text !== "string" ||
      !completedMessage.text.trim()
    ) {
      throw new Error("Web to Workers AI chat returned an invalid AI message");
    }
  } finally {
    chatCleanup = await request(fetchImpl, `${webBase}/api/proxy/v2/messages`, {
      method: "DELETE",
      headers: authHeaders,
    });
    expectStatus("Workers AI chat smoke cleanup", chatCleanup, 200);
  }
  return {
    chatHistoryPreflightStatus: preflight.response.status,
    chatStatus,
    chatCleanupStatus: chatCleanup.status,
  };
}

export async function runSmoke({
  edgeUrl = resolveEdgeUrl(),
  webUrl = resolveWebUrl(),
  token = null,
  nativeTts = process.env.CLOUDFLARE_SMOKE_NATIVE_TTS === "1",
  fetchImpl = fetch,
} = {}) {
  const base = resolveEdgeUrl(edgeUrl);
  const webBase = resolveWebUrl(webUrl);
  const health = await request(fetchImpl, `${base}/health`);
  expectStatus("edge health", health, 200);
  const announcementsGeneral = await request(
    fetchImpl,
    `${base}/v1/announcements/general`,
  );
  expectStatus("public announcements", announcementsGeneral, 200);
  const appReviews = await request(
    fetchImpl,
    `${base}/v1/apps/cf-staging-search-app/reviews`,
  );
  expectStatus("public app reviews", appReviews, 200);
  const paymentSuccess = await request(
    fetchImpl,
    `${base}/v1/payments/success?session_id=cf_smoke_session`,
  );
  expectStatus("public payment success page", paymentSuccess, 200);
  const paymentCancel = await request(fetchImpl, `${base}/v1/payments/cancel`);
  expectStatus("public payment cancel page", paymentCancel, 200);
  const paymentPortalReturn = await request(
    fetchImpl,
    `${base}/v1/payments/portal-return`,
  );
  expectStatus("public payment portal return page", paymentPortalReturn, 200);
  const stripeWebhookBoundary = await request(
    fetchImpl,
    `${base}/v1/stripe/webhook`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    },
  );
  // A provisioned webhook rejects the unsigned body with 400. An isolated
  // environment without its whsec credential fails closed before parsing.
  expectStatusIn("Stripe webhook boundary", stripeWebhookBoundary, [400, 503]);
  const stripeConnectWebhookBoundary = await request(
    fetchImpl,
    `${base}/v1/stripe/connect/webhook`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    },
  );
  expectStatusIn(
    "Stripe Connect webhook boundary",
    stripeConnectWebhookBoundary,
    [400, 503],
  );
  const stripeSupportedCountriesBoundary = await request(
    fetchImpl,
    `${base}/v1/stripe/supported-countries`,
  );
  expectStatusIn(
    "Stripe supported countries provider boundary",
    stripeSupportedCountriesBoundary,
    [200, 503],
  );
  const stripeReturnMissing = await request(
    fetchImpl,
    `${base}/v1/stripe/return/acct_cfSmokeMissing123`,
  );
  expectStatus("Stripe return account authority", stripeReturnMissing, 404);
  const stripeBrowserRefreshBoundary = await request(
    fetchImpl,
    `${base}/v1/stripe/refresh/acct_cfSmokeMissing123?token=invalid`,
  );
  expectStatus(
    "Stripe browser refresh signature",
    stripeBrowserRefreshBoundary,
    403,
  );
  const mcpDataInvalidKey = await request(
    fetchImpl,
    `${base}/v1/mcp/memories`,
    { headers: { authorization: `Bearer omi_mcp_${"f".repeat(32)}` } },
  );
  expectStatus("MCP data invalid key", mcpDataInvalidKey, 403);

  const result = {
    edgeHealth: health.status,
    announcementsGeneral: announcementsGeneral.status,
    appReviews: appReviews.status,
    paymentSuccess: paymentSuccess.status,
    paymentCancel: paymentCancel.status,
    paymentPortalReturn: paymentPortalReturn.status,
    stripeWebhookBoundary: stripeWebhookBoundary.status,
    stripeConnectWebhookBoundary: stripeConnectWebhookBoundary.status,
    stripeSupportedCountriesBoundary: stripeSupportedCountriesBoundary.status,
    stripeReturnMissing: stripeReturnMissing.status,
    stripeBrowserRefreshBoundary: stripeBrowserRefreshBoundary.status,
    mcpDataInvalidKey: mcpDataInvalidKey.status,
  };
  if (!token) return { ...result, authenticatedChecks: "skipped" };

  const unauthenticated = await request(fetchImpl, `${base}/v1/cf/probe`);
  expectStatus("unauthenticated probe", unauthenticated, 401);
  const unauthenticatedAnnouncements = await request(
    fetchImpl,
    `${base}/v1/announcements/pending?app_version=0.0.0&platform=ios&trigger=app_launch`,
  );
  expectStatus(
    "unauthenticated announcements",
    unauthenticatedAnnouncements,
    401,
  );
  const unauthenticatedAnnouncementsAdmin = await request(
    fetchImpl,
    `${base}/v1/announcements/all`,
  );
  expectStatus(
    "unauthenticated announcement admin",
    unauthenticatedAnnouncementsAdmin,
    403,
  );
  const unauthenticatedFairUseAdmin = await request(
    fetchImpl,
    `${base}/v1/admin/fair-use/flagged`,
  );
  expectStatus(
    "unauthenticated fair use admin",
    unauthenticatedFairUseAdmin,
    403,
  );
  const unauthenticatedAsyncTranscription = await request(
    fetchImpl,
    `${base}/v1/stt/transcribe-async`,
    {
      method: "POST",
      headers: { "content-type": "audio/wav" },
      body: "",
    },
  );
  expectStatus(
    "unauthenticated async transcription",
    unauthenticatedAsyncTranscription,
    401,
  );
  const unauthenticatedSyncUpload = await request(
    fetchImpl,
    `${base}/v2/sync-local-files`,
    { method: "POST", body: "" },
  );
  expectStatus("unauthenticated sync upload", unauthenticatedSyncUpload, 401);
  const unauthenticatedSyncStatus = await request(
    fetchImpl,
    `${base}/v2/sync-local-files/missing`,
  );
  expectStatus("unauthenticated sync status", unauthenticatedSyncStatus, 401);

  const authHeaders = { authorization: `Bearer ${token}` };
  const probe = await request(fetchImpl, `${base}/v1/cf/probe`, {
    headers: authHeaders,
  });
  expectStatus("authenticated probe", probe, 200);
  const accountCutoverResult = await requestJson(
    fetchImpl,
    `${base}/v1/account/cutover/control`,
    { headers: authHeaders },
  );
  const accountCutover = accountCutoverResult.response;
  expectStatus("account cutover control", accountCutover, 200);
  const accountControl = accountCutoverResult.body;
  if (
    !accountControl ||
    accountControl.state !== "new" ||
    accountControl.product_traffic_allowed !== true ||
    accountControl.migration?.destination_backend_bound !== true
  ) {
    throw new Error(
      "account cutover control is not bound to the Cloudflare data plane",
    );
  }

  const appSearch = await request(fetchImpl, `${base}/v2/apps/search?limit=1`, {
    headers: authHeaders,
  });
  expectStatus("authenticated app catalog search", appSearch, 200);

  const memorySummaryFeedbackResult = await requestJson(
    fetchImpl,
    `${base}/v1/users/analytics/memory_summary?memory_id=cf-smoke-missing`,
    { headers: authHeaders },
  );
  const memorySummaryFeedback = memorySummaryFeedbackResult.response;
  expectStatus(
    "memory-summary feedback missing-row",
    memorySummaryFeedback,
    200,
  );
  if (memorySummaryFeedbackResult.body?.has_rating !== false) {
    throw new Error("memory-summary feedback missing-row must be unrated");
  }

  const conversations = await request(
    fetchImpl,
    `${base}/v1/conversations?limit=10`,
    { headers: authHeaders },
  );
  expectStatus("canonical conversation list", conversations, 200);
  const conversationSearch = await request(
    fetchImpl,
    `${base}/v1/conversations/search`,
    {
      method: "POST",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({
        query: `cf-smoke-search-${Date.now()}`,
        page: 1,
        per_page: 1,
      }),
    },
  );
  expectStatus("canonical conversation search", conversationSearch, 200);
  const webProxyConversations = await request(
    fetchImpl,
    `${webBase}/api/proxy/v1/conversations?limit=1`,
    { headers: authHeaders },
  );
  expectStatus(
    "Web to Edge conversation service binding",
    webProxyConversations,
    200,
  );
  const webProxyEnabledApps = await request(
    fetchImpl,
    `${webBase}/api/proxy/v1/apps/enabled`,
    { headers: authHeaders },
  );
  expectStatus(
    "Web to Edge enabled apps service binding",
    webProxyEnabledApps,
    200,
  );
  const webProxyMemories = await request(
    fetchImpl,
    `${webBase}/api/proxy/v3/memories?limit=1&offset=0`,
    { headers: authHeaders },
  );
  expectStatus("Web to Edge memories service binding", webProxyMemories, 200);
  const workersAiChat = await exerciseWorkersAiChat(
    fetchImpl,
    webBase,
    authHeaders,
  );
  const chatMessageCountResult = await requestJson(
    fetchImpl,
    `${base}/v1/users/stats/chat-messages`,
    { headers: authHeaders },
  );
  expectStatus("chat message count", chatMessageCountResult.response, 200);
  if (
    typeof chatMessageCountResult.body?.count !== "number" ||
    chatMessageCountResult.body.count < 0
  ) {
    throw new Error("chat message count returned an invalid count");
  }
  const chatSessionsResult = await requestJson(
    fetchImpl,
    `${base}/v2/chat-sessions?limit=1&offset=0`,
    { headers: authHeaders },
  );
  expectStatus("chat session list", chatSessionsResult.response, 200);
  if (!Array.isArray(chatSessionsResult.body)) {
    throw new Error("chat session list returned an invalid payload");
  }
  const desktopMessagesResult = await requestJson(
    fetchImpl,
    `${base}/v2/desktop/messages?limit=1&offset=0`,
    { headers: authHeaders },
  );
  expectStatus("desktop message list", desktopMessagesResult.response, 200);
  if (!Array.isArray(desktopMessagesResult.body)) {
    throw new Error("desktop message list returned an invalid payload");
  }
  const missingChatReport = await request(
    fetchImpl,
    `${base}/v2/messages/cf-smoke-missing/report`,
    { method: "POST", headers: authHeaders },
  );
  expectStatus("chat report missing-row", missingChatReport, 404);
  const missingDesktopRating = await request(
    fetchImpl,
    `${base}/v2/desktop/messages/cf-smoke-missing/rating`,
    {
      method: "PATCH",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({ rating: 1 }),
    },
  );
  expectStatus("desktop rating missing-row", missingDesktopRating, 404);
  const memories = await request(
    fetchImpl,
    `${base}/v3/memories?limit=25&offset=0`,
    { headers: authHeaders },
  );
  expectStatus("canonical memory list", memories, 200);
  const folders = await request(fetchImpl, `${base}/v1/folders`, {
    headers: authHeaders,
  });
  expectStatus("canonical folder list", folders, 200);
  const conversationCount = await request(
    fetchImpl,
    `${base}/v1/conversations/count`,
    { headers: authHeaders },
  );
  expectStatus("canonical conversation count", conversationCount, 200);
  const conversationPhotos = await request(
    fetchImpl,
    `${base}/v1/conversations/cf-smoke-photo-probe-${Date.now()}/photos`,
    { headers: authHeaders },
  );
  expectStatus(
    "canonical conversation photos missing-row",
    conversationPhotos,
    404,
  );
  const conversationTranscripts = await request(
    fetchImpl,
    `${base}/v1/conversations/cf-smoke-transcripts-probe-${Date.now()}/transcripts`,
    { headers: authHeaders },
  );
  expectStatus(
    "canonical conversation transcripts missing-row",
    conversationTranscripts,
    404,
  );
  const conversationAnalytics = await request(
    fetchImpl,
    `${base}/v1/conversations/cf-smoke-analytics-probe-${Date.now()}/analytics`,
    { headers: authHeaders },
  );
  expectStatus(
    "canonical conversation analytics missing-row",
    conversationAnalytics,
    404,
  );
  const conversationEvents = await request(
    fetchImpl,
    `${base}/v1/conversations/cf-smoke-events-probe-${Date.now()}/events`,
    {
      method: "PATCH",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({ events_idx: [0], values: [true] }),
    },
  );
  expectStatus(
    "canonical conversation events missing-row",
    conversationEvents,
    404,
  );
  const conversationSummary = await request(
    fetchImpl,
    `${base}/v1/conversations/cf-smoke-summary-probe-${Date.now()}/summary`,
    {
      method: "PATCH",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({ content: "probe" }),
    },
  );
  expectStatus(
    "canonical conversation summary missing-row",
    conversationSummary,
    404,
  );
  const conversationCalendarUnlink = await request(
    fetchImpl,
    `${base}/v1/conversations/cf-smoke-calendar-link-probe-${Date.now()}/calendar-event`,
    { method: "DELETE", headers: authHeaders },
  );
  expectStatus(
    "canonical conversation calendar unlink missing-row",
    conversationCalendarUnlink,
    404,
  );
  const conversationActionItems = await request(
    fetchImpl,
    `${base}/v1/conversations/cf-smoke-action-items-probe-${Date.now()}/action-items`,
    {
      method: "PATCH",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({ items_idx: [0], values: [true] }),
    },
  );
  expectStatus(
    "canonical conversation action items missing-row",
    conversationActionItems,
    404,
  );
  const conversationActionItemDescription = await request(
    fetchImpl,
    `${base}/v1/conversations/cf-smoke-action-item-description-probe-${Date.now()}/action-items/0`,
    {
      method: "PATCH",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({
        old_description: "missing",
        description: "probe",
      }),
    },
  );
  expectStatus(
    "canonical conversation action-item description missing-row",
    conversationActionItemDescription,
    404,
  );
  const conversationActionItemDelete = await request(
    fetchImpl,
    `${base}/v1/conversations/cf-smoke-action-item-delete-probe-${Date.now()}/action-items`,
    {
      method: "DELETE",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({ description: "missing", completed: false }),
    },
  );
  expectStatus(
    "canonical conversation action-item delete missing-row",
    conversationActionItemDelete,
    404,
  );
  const conversationRecording = await request(
    fetchImpl,
    `${base}/v1/conversations/cf-smoke-recording-probe-${Date.now()}/recording`,
    { headers: authHeaders },
  );
  expectStatus(
    "canonical conversation recording missing-row",
    conversationRecording,
    404,
  );
  const segmentText = await request(
    fetchImpl,
    `${base}/v1/conversations/cf-smoke-segment-probe-${Date.now()}/segments/text`,
    {
      method: "PATCH",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({ segment_id: "missing", text: "probe" }),
    },
  );
  expectStatus(
    "canonical conversation segment text missing-row",
    segmentText,
    404,
  );

  const assistantSettings = await request(
    fetchImpl,
    `${base}/v1/users/assistant-settings`,
    { headers: authHeaders },
  );
  expectStatus("assistant settings", assistantSettings, 200);

  const aiProfile = await request(fetchImpl, `${base}/v1/users/ai-profile`, {
    headers: authHeaders,
  });
  expectStatus("AI profile", aiProfile, 200);

  const trainingDataOptIn = await request(
    fetchImpl,
    `${base}/v1/users/training-data-opt-in`,
    { headers: authHeaders },
  );
  expectStatus("training data opt-in", trainingDataOptIn, 200);

  const developerWebhooksStatus = await request(
    fetchImpl,
    `${base}/v1/users/developer/webhooks/status`,
    {
      headers: authHeaders,
    },
  );
  expectStatus("developer webhooks status", developerWebhooksStatus, 200);

  const profile = await request(fetchImpl, `${base}/v1/users/profile`, {
    headers: authHeaders,
  });
  expectStatus("user profile", profile, 200);

  const dailySummarySettings = await request(
    fetchImpl,
    `${base}/v1/users/daily-summary-settings`,
    { headers: authHeaders },
  );
  expectStatus("daily summary settings", dailySummarySettings, 200);

  const mentorNotificationSettings = await request(
    fetchImpl,
    `${base}/v1/users/mentor-notification-settings`,
    {
      headers: authHeaders,
    },
  );
  expectStatus("mentor notification settings", mentorNotificationSettings, 200);

  const dailyScore = await request(fetchImpl, `${base}/v1/daily-score`, {
    headers: authHeaders,
  });
  expectStatus("daily score", dailyScore, 200);

  const scores = await request(fetchImpl, `${base}/v1/scores`, {
    headers: authHeaders,
  });
  expectStatus("scores", scores, 200);

  const advice = await request(fetchImpl, `${base}/v1/advice?limit=1`, {
    headers: authHeaders,
  });
  expectStatus("advice", advice, 200);

  const focusSessions = await request(fetchImpl, `${base}/v1/focus-sessions`, {
    headers: authHeaders,
  });
  expectStatus("focus sessions", focusSessions, 200);

  const focusStats = await request(fetchImpl, `${base}/v1/focus-stats`, {
    headers: authHeaders,
  });
  expectStatus("focus stats", focusStats, 200);

  const screenActivity = await request(
    fetchImpl,
    `${base}/v1/screen-activity`,
    { headers: authHeaders },
  );
  expectStatus("screen activity", screenActivity, 200);

  const screenActivitySummary = await request(
    fetchImpl,
    `${base}/v1/screen-activity/summary`,
    {
      headers: authHeaders,
    },
  );
  expectStatus("screen activity summary", screenActivitySummary, 200);

  const calendarOnboardingStatus = await request(
    fetchImpl,
    `${base}/v1/calendar/onboarding/status`,
    {
      headers: authHeaders,
    },
  );
  expectStatus("calendar onboarding status", calendarOnboardingStatus, 200);

  const accountUsage = await request(
    fetchImpl,
    `${base}/v1/users/me/usage?period=monthly`,
    { headers: authHeaders },
  );
  expectStatus("account usage", accountUsage, 200);

  const accountSubscription = await request(
    fetchImpl,
    `${base}/v1/users/me/subscription`,
    { headers: authHeaders },
  );
  expectStatus("account subscription", accountSubscription, 200);

  const chatQuota = await request(
    fetchImpl,
    `${base}/v1/users/me/usage-quota`,
    { headers: authHeaders },
  );
  expectStatus("chat quota", chatQuota, 200);

  const paywall = await request(fetchImpl, `${base}/v1/users/me/paywall`, {
    headers: authHeaders,
  });
  expectStatus("paywall", paywall, 200);

  const trial = await request(fetchImpl, `${base}/v1/users/me/trial`, {
    headers: authHeaders,
  });
  expectStatus("trial", trial, 200);

  const llmUsage = await request(
    fetchImpl,
    `${base}/v1/users/me/llm-usage?days=30`,
    { headers: authHeaders },
  );
  expectStatus("LLM usage summary", llmUsage, 200);

  const llmTopFeatures = await request(
    fetchImpl,
    `${base}/v1/users/me/llm-usage/top-features?days=30&limit=3`,
    { headers: authHeaders },
  );
  expectStatus("LLM usage top features", llmTopFeatures, 200);

  const llmTotalCost = await request(
    fetchImpl,
    `${base}/v1/users/me/llm-usage/total`,
    { headers: authHeaders },
  );
  expectStatus("LLM usage total cost", llmTotalCost, 200);

  const availablePlans = await request(
    fetchImpl,
    `${base}/v1/payments/available-plans`,
    { headers: authHeaders },
  );
  expectStatus("available plans", availablePlans, 200);

  const overageInfo = await request(
    fetchImpl,
    `${base}/v1/payments/overage-info`,
    { headers: authHeaders },
  );
  expectStatus("overage info", overageInfo, 200);

  const checkoutBoundary = await request(
    fetchImpl,
    `${base}/v1/payments/checkout-session`,
    {
      method: "POST",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({ price_id: "price_cfSmokeUnavailable" }),
    },
  );
  expectStatus("checkout price allowlist", checkoutBoundary, 400);

  const customerPortalBoundary = await request(
    fetchImpl,
    `${base}/v1/payments/customer-portal`,
    { method: "POST", headers: authHeaders },
  );
  expectStatus(
    "customer portal customer authority",
    customerPortalBoundary,
    400,
  );

  const upgradeBoundary = await request(
    fetchImpl,
    `${base}/v1/payments/upgrade-subscription`,
    {
      method: "POST",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({ price_id: "price_cfSmokeUnavailable" }),
    },
  );
  expectStatus("subscription upgrade price allowlist", upgradeBoundary, 400);

  const cancelSubscriptionBoundary = await request(
    fetchImpl,
    `${base}/v1/payments/subscription`,
    { method: "DELETE", headers: authHeaders },
  );
  expectStatus(
    "subscription cancellation authority",
    cancelSubscriptionBoundary,
    400,
  );

  const appSubscription = await request(
    fetchImpl,
    `${base}/v1/apps/cf-smoke-missing/subscription`,
    { headers: authHeaders },
  );
  expectStatus("paid app subscription projection", appSubscription, 200);

  const cancelAppSubscriptionBoundary = await request(
    fetchImpl,
    `${base}/v1/apps/cf-smoke-missing/subscription`,
    { method: "DELETE", headers: authHeaders },
  );
  expectStatus(
    "paid app subscription cancellation authority",
    cancelAppSubscriptionBoundary,
    404,
  );

  const appDeleteBoundary = await request(
    fetchImpl,
    `${base}/v1/apps/cf-smoke-missing`,
    { method: "DELETE", headers: authHeaders },
  );
  expectStatus("app deletion ownership boundary", appDeleteBoundary, 404);

  const appCreateBoundary = await request(fetchImpl, `${base}/v1/apps`, {
    method: "POST",
    headers: authHeaders,
  });
  expectStatus("app create multipart boundary", appCreateBoundary, 400);

  const appUpdateBoundary = await request(
    fetchImpl,
    `${base}/v1/apps/cf-smoke-missing`,
    { method: "PATCH", headers: authHeaders },
  );
  expectStatus("app update ownership boundary", appUpdateBoundary, 404);

  const appVisibilityBoundary = await request(
    fetchImpl,
    `${base}/v1/apps/cf-smoke-missing/change-visibility?private=false`,
    { method: "PATCH", headers: authHeaders },
  );
  expectStatus("app visibility ownership boundary", appVisibilityBoundary, 404);

  const appManifestRefreshBoundary = await request(
    fetchImpl,
    `${base}/v1/apps/cf-smoke-missing/refresh-manifest`,
    { method: "POST", headers: authHeaders },
  );
  expectStatus(
    "app manifest refresh ownership boundary",
    appManifestRefreshBoundary,
    404,
  );

  const appLogoBoundary = await request(
    fetchImpl,
    `${base}/v1/apps/cf-smoke-missing/logo/00000000-0000-4000-8000-000000000000`,
  );
  expectStatus("app logo catalog boundary", appLogoBoundary, 404);

  const appApiKeyCreateBoundary = await request(
    fetchImpl,
    `${base}/v1/apps/cf-smoke-missing/keys`,
    { method: "POST", headers: authHeaders },
  );
  expectStatus(
    "app API key create ownership boundary",
    appApiKeyCreateBoundary,
    404,
  );
  const appApiKeyListBoundary = await request(
    fetchImpl,
    `${base}/v1/apps/cf-smoke-missing/keys`,
    { headers: authHeaders },
  );
  expectStatus(
    "app API key list ownership boundary",
    appApiKeyListBoundary,
    404,
  );
  const appApiKeyDeleteBoundary = await request(
    fetchImpl,
    `${base}/v1/apps/cf-smoke-missing/keys/cf-smoke-missing-key`,
    { method: "DELETE", headers: authHeaders },
  );
  expectStatus(
    "app API key delete ownership boundary",
    appApiKeyDeleteBoundary,
    404,
  );

  const mcpApiKeyList = await request(fetchImpl, `${base}/v1/mcp/keys`, {
    headers: authHeaders,
  });
  expectStatus("MCP API key list", mcpApiKeyList, 200);
  const mcpApiKeyValidation = await request(fetchImpl, `${base}/v1/mcp/keys`, {
    method: "POST",
    headers: { ...authHeaders, "content-type": "application/json" },
    body: JSON.stringify({ name: "   " }),
  });
  expectStatus("MCP API key validation", mcpApiKeyValidation, 422);
  const mcpApiKeyDeleteMissing = await request(
    fetchImpl,
    `${base}/v1/mcp/keys/cf-smoke-missing-key`,
    { method: "DELETE", headers: authHeaders },
  );
  expectStatus("MCP API key idempotent deletion", mcpApiKeyDeleteMissing, 204);

  const integrationHeaders = {
    authorization: "Bearer sk_cf-smoke-invalid-integration-key",
    "content-type": "application/json",
  };
  const integrationBoundaries = {};
  for (const boundary of [
    {
      key: "integrationNotificationV1Boundary",
      label: "integration notification v1 key boundary",
      url: `${base}/v1/integrations/notification`,
      method: "POST",
      body: { aid: "cf-smoke-missing", uid: "cf-smoke-user", message: "smoke" },
    },
    {
      key: "integrationConversationCreateBoundary",
      label: "integration conversation create key boundary",
      url: `${base}/v2/integrations/cf-smoke-missing/user/conversations?uid=cf-smoke-user`,
      method: "POST",
      body: { text: "Cloudflare smoke" },
    },
    {
      key: "integrationMemoryCreateBoundary",
      label: "integration memory create key boundary",
      url: `${base}/v2/integrations/cf-smoke-missing/user/memories?uid=cf-smoke-user`,
      method: "POST",
      body: { memories: [{ content: "Cloudflare smoke" }] },
    },
    {
      key: "integrationMemoriesBoundary",
      label: "integration memory read key boundary",
      url: `${base}/v2/integrations/cf-smoke-missing/memories?uid=cf-smoke-user`,
    },
    {
      key: "integrationConversationsBoundary",
      label: "integration conversation read key boundary",
      url: `${base}/v2/integrations/cf-smoke-missing/conversations?uid=cf-smoke-user`,
    },
    {
      key: "integrationConversationSearchBoundary",
      label: "integration conversation search key boundary",
      url: `${base}/v2/integrations/cf-smoke-missing/search/conversations?uid=cf-smoke-user`,
      method: "POST",
      body: { query: "smoke" },
    },
    {
      key: "integrationNotificationV2Boundary",
      label: "integration notification v2 key boundary",
      url: `${base}/v2/integrations/cf-smoke-missing/notification?uid=cf-smoke-user&message=smoke`,
      method: "POST",
    },
    {
      key: "integrationTasksBoundary",
      label: "integration task read key boundary",
      url: `${base}/v2/integrations/cf-smoke-missing/tasks?uid=cf-smoke-user`,
    },
  ]) {
    const response = await request(fetchImpl, boundary.url, {
      method: boundary.method,
      headers: integrationHeaders,
      body: boundary.body ? JSON.stringify(boundary.body) : undefined,
    });
    expectStatus(boundary.label, response, 403);
    integrationBoundaries[boundary.key] = response.status;
  }

  const connectAccountBoundary = await request(
    fetchImpl,
    `${base}/v1/stripe/connect-accounts?country=USA`,
    { method: "POST", headers: authHeaders },
  );
  expectStatus(
    "Stripe Connect country validation",
    connectAccountBoundary,
    400,
  );

  const stripeOnboardingStatus = await request(
    fetchImpl,
    `${base}/v1/stripe/onboarded`,
    { headers: authHeaders },
  );
  expectStatus("Stripe onboarding status", stripeOnboardingStatus, 200);

  const stripeRefreshOwnership = await request(
    fetchImpl,
    `${base}/v1/stripe/refresh/acct_cfSmokeMissing123`,
    { method: "POST", headers: authHeaders },
  );
  expectStatus("Stripe refresh account authority", stripeRefreshOwnership, 403);

  const paypalSave = await request(
    fetchImpl,
    `${base}/v1/paypal/payment-details`,
    {
      method: "POST",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({
        email: "cf-smoke@example.invalid",
        paypalme_url: "paypal.me/cf-smoke",
      }),
    },
  );
  expectStatus("PayPal payment details mutation", paypalSave, 200);

  const paypalDetails = await request(
    fetchImpl,
    `${base}/v1/paypal/payment-details`,
    { headers: authHeaders },
  );
  expectStatus("PayPal payment details", paypalDetails, 200);

  const defaultPaymentMethod = await request(
    fetchImpl,
    `${base}/v1/payment-methods/default`,
    {
      method: "POST",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({ method: "paypal" }),
    },
  );
  expectStatus("default creator payment method", defaultPaymentMethod, 200);

  const paymentMethodStatus = await request(
    fetchImpl,
    `${base}/v1/payment-methods/status`,
    { headers: authHeaders },
  );
  expectStatus("creator payment method status", paymentMethodStatus, 200);

  const fairUseStatus = await request(fetchImpl, `${base}/v1/fair-use/status`, {
    headers: authHeaders,
  });
  expectStatus("fair use status", fairUseStatus, 200);

  // Exercise geolocation route ownership without persisting location data.
  // The legacy contract intentionally returns 200 for invalid coordinates.
  const invalidGeolocation = await request(
    fetchImpl,
    `${base}/v1/users/geolocation`,
    {
      method: "PATCH",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({ latitude: 200, longitude: 0 }),
    },
  );
  expectStatus("invalid geolocation", invalidGeolocation, 200);

  // Exercise route ownership and input validation without billable inference.
  const workersAiEmpty = await request(
    fetchImpl,
    `${base}/v1/stt/transcribe-workers-ai`,
    {
      method: "POST",
      headers: { ...authHeaders, "content-type": "audio/wav" },
      body: "",
    },
  );
  expectStatus("Workers AI empty audio", workersAiEmpty, 400);

  const voiceMessageEmpty = await request(
    fetchImpl,
    `${base}/v2/voice-message/transcribe`,
    {
      method: "POST",
      headers: {
        ...authHeaders,
        "content-type": "application/octet-stream",
      },
      body: "",
    },
  );
  expectStatus("voice-message empty audio", voiceMessageEmpty, 400);

  let nativeTtsResult = {};
  if (nativeTts) {
    const response = await fetchImpl(`${base}/v1/tts/synthesize-workers-ai`, {
      method: "POST",
      headers: { ...authHeaders, "content-type": "application/json" },
      body: JSON.stringify({ text: "cloudflare smoke", speaker: "luna" }),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    const body = await response.arrayBuffer();
    expectStatus("Workers AI native TTS", response, 200);
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.startsWith("audio/mpeg")) {
      throw new Error(
        `Workers AI native TTS expected audio/mpeg, received ${contentType || "missing content type"}`,
      );
    }
    if (body.byteLength === 0)
      throw new Error("Workers AI native TTS returned empty audio");
    nativeTtsResult = {
      nativeTts: response.status,
      nativeTtsBytes: body.byteLength,
    };
  }

  return {
    ...result,
    unauthenticatedProbe: unauthenticated.status,
    unauthenticatedAnnouncements: unauthenticatedAnnouncements.status,
    unauthenticatedAnnouncementsAdmin: unauthenticatedAnnouncementsAdmin.status,
    unauthenticatedFairUseAdmin: unauthenticatedFairUseAdmin.status,
    unauthenticatedAsyncTranscription: unauthenticatedAsyncTranscription.status,
    unauthenticatedSyncUpload: unauthenticatedSyncUpload.status,
    unauthenticatedSyncStatus: unauthenticatedSyncStatus.status,
    authenticatedProbe: probe.status,
    accountCutover: accountCutover.status,
    appSearch: appSearch.status,
    memorySummaryFeedback: memorySummaryFeedback.status,
    conversations: conversations.status,
    conversationSearch: conversationSearch.status,
    webProxyConversations: webProxyConversations.status,
    webProxyEnabledApps: webProxyEnabledApps.status,
    webProxyMemories: webProxyMemories.status,
    workersAiChatHistoryPreflight: workersAiChat.chatHistoryPreflightStatus,
    webProxyWorkersAiChat: workersAiChat.chatStatus,
    workersAiChatCleanup: workersAiChat.chatCleanupStatus,
    chatMessageCount: chatMessageCountResult.response.status,
    chatSessions: chatSessionsResult.response.status,
    desktopMessages: desktopMessagesResult.response.status,
    missingChatReport: missingChatReport.status,
    missingDesktopRating: missingDesktopRating.status,
    memories: memories.status,
    folders: folders.status,
    conversationCount: conversationCount.status,
    conversationPhotos: conversationPhotos.status,
    conversationTranscripts: conversationTranscripts.status,
    conversationAnalytics: conversationAnalytics.status,
    conversationEvents: conversationEvents.status,
    conversationSummary: conversationSummary.status,
    conversationCalendarUnlink: conversationCalendarUnlink.status,
    conversationActionItems: conversationActionItems.status,
    conversationActionItemDescription: conversationActionItemDescription.status,
    conversationActionItemDelete: conversationActionItemDelete.status,
    conversationRecording: conversationRecording.status,
    segmentText: segmentText.status,
    assistantSettings: assistantSettings.status,
    aiProfile: aiProfile.status,
    trainingDataOptIn: trainingDataOptIn.status,
    developerWebhooksStatus: developerWebhooksStatus.status,
    userProfile: profile.status,
    dailySummarySettings: dailySummarySettings.status,
    mentorNotificationSettings: mentorNotificationSettings.status,
    dailyScore: dailyScore.status,
    scores: scores.status,
    advice: advice.status,
    focusSessions: focusSessions.status,
    focusStats: focusStats.status,
    screenActivity: screenActivity.status,
    screenActivitySummary: screenActivitySummary.status,
    calendarOnboardingStatus: calendarOnboardingStatus.status,
    accountUsage: accountUsage.status,
    accountSubscription: accountSubscription.status,
    llmUsage: llmUsage.status,
    llmTopFeatures: llmTopFeatures.status,
    llmTotalCost: llmTotalCost.status,
    availablePlans: availablePlans.status,
    overageInfo: overageInfo.status,
    checkoutBoundary: checkoutBoundary.status,
    customerPortalBoundary: customerPortalBoundary.status,
    upgradeBoundary: upgradeBoundary.status,
    cancelSubscriptionBoundary: cancelSubscriptionBoundary.status,
    appSubscription: appSubscription.status,
    cancelAppSubscriptionBoundary: cancelAppSubscriptionBoundary.status,
    appDeleteBoundary: appDeleteBoundary.status,
    appCreateBoundary: appCreateBoundary.status,
    appUpdateBoundary: appUpdateBoundary.status,
    appVisibilityBoundary: appVisibilityBoundary.status,
    appManifestRefreshBoundary: appManifestRefreshBoundary.status,
    appLogoBoundary: appLogoBoundary.status,
    appApiKeyCreateBoundary: appApiKeyCreateBoundary.status,
    appApiKeyListBoundary: appApiKeyListBoundary.status,
    appApiKeyDeleteBoundary: appApiKeyDeleteBoundary.status,
    mcpApiKeyList: mcpApiKeyList.status,
    mcpApiKeyValidation: mcpApiKeyValidation.status,
    mcpApiKeyDeleteMissing: mcpApiKeyDeleteMissing.status,
    ...integrationBoundaries,
    connectAccountBoundary: connectAccountBoundary.status,
    stripeOnboardingStatus: stripeOnboardingStatus.status,
    stripeRefreshOwnership: stripeRefreshOwnership.status,
    paypalSave: paypalSave.status,
    paypalDetails: paypalDetails.status,
    defaultPaymentMethod: defaultPaymentMethod.status,
    paymentMethodStatus: paymentMethodStatus.status,
    fairUseStatus: fairUseStatus.status,
    invalidGeolocation: invalidGeolocation.status,
    workersAiEmptyAudio: workersAiEmpty.status,
    voiceMessageEmptyAudio: voiceMessageEmpty.status,
    ...nativeTtsResult,
  };
}

async function main() {
  const token = await readOptionalBearerToken();
  const result = await runSmoke({ token });
  console.log(`Cloudflare staging smoke passed: ${JSON.stringify(result)}`);
  if (!token) {
    console.log(
      "Authenticated checks skipped; set CLOUDFLARE_SMOKE_BEARER_TOKEN or CLOUDFLARE_SMOKE_TOKEN_FILE to enable them.",
    );
  }
}

if (process.argv[1]?.endsWith("smoke-staging.mjs")) {
  main().catch((error) => {
    console.error(
      `Cloudflare staging smoke failed: ${error instanceof Error ? error.message : "unknown error"}`,
    );
    process.exitCode = 1;
  });
}
