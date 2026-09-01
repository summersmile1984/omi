#!/usr/bin/env node
// LIFECYCLE: permanent

import { readFile } from "node:fs/promises";
import { parseTokenPayload, resolveEdgeUrl } from "./smoke-staging.mjs";

const REQUEST_TIMEOUT_MS = 15_000;

function fail(message) {
  throw new Error(`calendar positive probe: ${message}`);
}

export function resolveCalendarProbeConfig(env = process.env) {
  const bearer = env.CLOUDFLARE_CALENDAR_PROBE_BEARER_TOKEN?.trim();
  const accessToken = env.CLOUDFLARE_CALENDAR_PROBE_ACCESS_TOKEN?.trim();
  if (!bearer) fail("CLOUDFLARE_CALENDAR_PROBE_BEARER_TOKEN is required");
  if (!accessToken) fail("CLOUDFLARE_CALENDAR_PROBE_ACCESS_TOKEN is required");
  if (env.CLOUDFLARE_CALENDAR_PROBE_CONFIRM !== "1") {
    fail("set CLOUDFLARE_CALENDAR_PROBE_CONFIRM=1 for the disposable-account mutation");
  }
  if (accessToken.length > 16_000) fail("access token is too long");
  return Object.freeze({ edgeUrl: resolveEdgeUrl(env.CLOUDFLARE_EDGE_URL), bearer, accessToken });
}

async function request(fetchImpl, url, init = {}) {
  try {
    return await fetchImpl(url, {
      ...init,
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch {
    fail("staging request failed");
  }
}

async function jsonBody(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function authHeaders(bearer) {
  return { authorization: `Bearer ${bearer}` };
}

function expectStatus(label, response, expected) {
  if (response.status !== expected) fail(`${label} expected ${expected}, got ${response.status}`);
}

/**
 * Verify Calendar's provider-backed data path for a disposable staging
 * account. The caller supplies a short-lived Google access token; the probe
 * never prints it, creates an event, or leaves the integration connected.
 */
export async function runCalendarPositiveProbe({ config, fetchImpl = fetch } = {}) {
  if (!config) fail("probe config is required");
  const headers = authHeaders(config.bearer);
  let saved = false;
  try {
    const save = await request(fetchImpl, `${config.edgeUrl}/v1/integrations/google_calendar`, {
      method: "PUT",
      headers: { ...headers, "content-type": "application/json" },
      body: JSON.stringify({ connected: true, access_token: config.accessToken }),
    });
    expectStatus("integration save", save, 200);
    const savePayload = await jsonBody(save);
    if (savePayload?.status !== "ok" || savePayload?.app_key !== "google_calendar") {
      fail("integration save returned an invalid envelope");
    }
    saved = true;

    const status = await request(fetchImpl, `${config.edgeUrl}/v1/integrations/google_calendar`, {
      headers,
    });
    expectStatus("connected status", status, 200);
    const statusPayload = await jsonBody(status);
    if (statusPayload?.connected !== true || statusPayload?.app_key !== "google_calendar") {
      fail("connected status did not report the saved grant");
    }

    const events = await request(
      fetchImpl,
      `${config.edgeUrl}/v1/calendar/google/events?max_results=1`,
      { headers },
    );
    expectStatus("calendar events", events, 200);
    const eventsPayload = await jsonBody(events);
    if (!Array.isArray(eventsPayload)) fail("calendar events returned an invalid envelope");

    const disconnect = await request(fetchImpl, `${config.edgeUrl}/v1/integrations/google_calendar`, {
      method: "DELETE",
      headers,
    });
    expectStatus("integration cleanup", disconnect, 204);
    saved = false;

    const finalStatus = await request(fetchImpl, `${config.edgeUrl}/v1/integrations/google_calendar`, {
      headers,
    });
    expectStatus("disconnected status", finalStatus, 200);
    const finalPayload = await jsonBody(finalStatus);
    if (finalPayload?.connected !== false || finalPayload?.app_key !== "google_calendar") {
      fail("disconnected status did not clear the grant");
    }
    return Object.freeze({ status: "passed", integration_save: 200, events: 200, cleanup: 204 });
  } finally {
    if (saved) {
      try {
        await request(fetchImpl, `${config.edgeUrl}/v1/integrations/google_calendar`, {
          method: "DELETE",
          headers,
        });
      } catch {
        // Preserve the primary failure; the staging account remains fenced by
        // the normal account deletion workflow if cleanup is unavailable.
      }
    }
  }
}

async function readBearer(env) {
  const direct = env.CLOUDFLARE_CALENDAR_PROBE_BEARER_TOKEN?.trim();
  if (direct) return direct;
  const tokenFile = env.CLOUDFLARE_CALENDAR_PROBE_TOKEN_FILE?.trim();
  if (!tokenFile) return null;
  return parseTokenPayload(await readFile(tokenFile, "utf8"));
}

async function main() {
  const env = { ...process.env };
  const bearer = await readBearer(env);
  if (bearer) env.CLOUDFLARE_CALENDAR_PROBE_BEARER_TOKEN = bearer;
  const config = resolveCalendarProbeConfig(env);
  console.log(`Cloudflare Calendar positive probe passed: ${JSON.stringify(await runCalendarPositiveProbe({ config }))}`);
}

if (process.argv[1]?.endsWith("calendar-positive-probe.mjs")) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : "calendar positive probe failed");
    process.exitCode = 1;
  });
}

