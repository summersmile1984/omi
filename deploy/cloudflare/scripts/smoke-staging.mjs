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

  const result = {
    edgeHealth: health.status,
    announcementsGeneral: announcementsGeneral.status,
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

  const conversations = await request(
    fetchImpl,
    `${base}/v1/conversations?limit=10`,
    { headers: authHeaders },
  );
  expectStatus("canonical conversation list", conversations, 200);
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
    unauthenticatedAsyncTranscription: unauthenticatedAsyncTranscription.status,
    authenticatedProbe: probe.status,
    accountCutover: accountCutover.status,
    appSearch: appSearch.status,
    conversations: conversations.status,
    webProxyConversations: webProxyConversations.status,
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
    focusSessions: focusSessions.status,
    focusStats: focusStats.status,
    screenActivity: screenActivity.status,
    screenActivitySummary: screenActivitySummary.status,
    calendarOnboardingStatus: calendarOnboardingStatus.status,
    invalidGeolocation: invalidGeolocation.status,
    workersAiEmptyAudio: workersAiEmpty.status,
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
