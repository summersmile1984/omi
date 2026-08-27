import { readFile } from "node:fs/promises";

const DEFAULT_EDGE_URL = "https://omi-cf-edge-staging.summersmile1984.workers.dev";
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
  const response = await fetchImpl(url, { ...init, signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
  // Consume each body so Node can reuse the connection for the next check.
  await response.arrayBuffer();
  return response;
}

function expectStatus(label, response, expected) {
  if (response.status !== expected) {
    throw new Error(`${label} expected HTTP ${expected}, received HTTP ${response.status}`);
  }
}

export async function runSmoke({
  edgeUrl = resolveEdgeUrl(),
  token = null,
  nativeTts = process.env.CLOUDFLARE_SMOKE_NATIVE_TTS === "1",
  fetchImpl = fetch,
} = {}) {
  const base = resolveEdgeUrl(edgeUrl);
  const health = await request(fetchImpl, `${base}/health`);
  expectStatus("edge health", health, 200);

  const result = { edgeHealth: health.status };
  if (!token) return { ...result, authenticatedChecks: "skipped" };

  const unauthenticated = await request(fetchImpl, `${base}/v1/cf/probe`);
  expectStatus("unauthenticated probe", unauthenticated, 401);

  const authHeaders = { authorization: `Bearer ${token}` };
  const probe = await request(fetchImpl, `${base}/v1/cf/probe`, { headers: authHeaders });
  expectStatus("authenticated probe", probe, 200);

  const assistantSettings = await request(fetchImpl, `${base}/v1/users/assistant-settings`, { headers: authHeaders });
  expectStatus("assistant settings", assistantSettings, 200);

  const aiProfile = await request(fetchImpl, `${base}/v1/users/ai-profile`, { headers: authHeaders });
  expectStatus("AI profile", aiProfile, 200);

  const trainingDataOptIn = await request(fetchImpl, `${base}/v1/users/training-data-opt-in`, { headers: authHeaders });
  expectStatus("training data opt-in", trainingDataOptIn, 200);

  const developerWebhooksStatus = await request(fetchImpl, `${base}/v1/users/developer/webhooks/status`, {
    headers: authHeaders,
  });
  expectStatus("developer webhooks status", developerWebhooksStatus, 200);

  const profile = await request(fetchImpl, `${base}/v1/users/profile`, { headers: authHeaders });
  expectStatus("user profile", profile, 200);

  const dailySummarySettings = await request(fetchImpl, `${base}/v1/users/daily-summary-settings`, { headers: authHeaders });
  expectStatus("daily summary settings", dailySummarySettings, 200);

  const mentorNotificationSettings = await request(fetchImpl, `${base}/v1/users/mentor-notification-settings`, {
    headers: authHeaders,
  });
  expectStatus("mentor notification settings", mentorNotificationSettings, 200);

  const dailyScore = await request(fetchImpl, `${base}/v1/daily-score`, { headers: authHeaders });
  expectStatus("daily score", dailyScore, 200);

  const scores = await request(fetchImpl, `${base}/v1/scores`, { headers: authHeaders });
  expectStatus("scores", scores, 200);

  const focusSessions = await request(fetchImpl, `${base}/v1/focus-sessions`, { headers: authHeaders });
  expectStatus("focus sessions", focusSessions, 200);

  const focusStats = await request(fetchImpl, `${base}/v1/focus-stats`, { headers: authHeaders });
  expectStatus("focus stats", focusStats, 200);

  // Exercise geolocation route ownership without persisting location data.
  // The legacy contract intentionally returns 200 for invalid coordinates.
  const invalidGeolocation = await request(fetchImpl, `${base}/v1/users/geolocation`, {
    method: "PATCH",
    headers: { ...authHeaders, "content-type": "application/json" },
    body: JSON.stringify({ latitude: 200, longitude: 0 }),
  });
  expectStatus("invalid geolocation", invalidGeolocation, 200);

  // Exercise route ownership and input validation without billable inference.
  const workersAiEmpty = await request(fetchImpl, `${base}/v1/stt/transcribe-workers-ai`, {
    method: "POST",
    headers: { ...authHeaders, "content-type": "audio/wav" },
    body: "",
  });
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
      throw new Error(`Workers AI native TTS expected audio/mpeg, received ${contentType || "missing content type"}`);
    }
    if (body.byteLength === 0) throw new Error("Workers AI native TTS returned empty audio");
    nativeTtsResult = { nativeTts: response.status, nativeTtsBytes: body.byteLength };
  }

  return {
    ...result,
    unauthenticatedProbe: unauthenticated.status,
    authenticatedProbe: probe.status,
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
    console.error(`Cloudflare staging smoke failed: ${error instanceof Error ? error.message : "unknown error"}`);
    process.exitCode = 1;
  });
}
