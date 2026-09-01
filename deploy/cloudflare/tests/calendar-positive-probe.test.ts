import { describe, expect, it, vi } from "vitest";
import {
  resolveCalendarProbeConfig,
  runCalendarPositiveProbe,
} from "../scripts/calendar-positive-probe.mjs";

const config = {
  edgeUrl: "https://edge.example.test",
  bearer: "better-auth-bearer",
  accessToken: "short-lived-google-token",
};

describe("Calendar staging positive probe", () => {
  it("requires explicit disposable-account confirmation and bounded credentials", () => {
    expect(() =>
      resolveCalendarProbeConfig({
        CLOUDFLARE_CALENDAR_PROBE_BEARER_TOKEN: config.bearer,
        CLOUDFLARE_CALENDAR_PROBE_ACCESS_TOKEN: config.accessToken,
      }),
    ).toThrow("CLOUDFLARE_CALENDAR_PROBE_CONFIRM=1");
    expect(() =>
      resolveCalendarProbeConfig({
        CLOUDFLARE_CALENDAR_PROBE_BEARER_TOKEN: config.bearer,
        CLOUDFLARE_CALENDAR_PROBE_ACCESS_TOKEN: "x".repeat(16_001),
        CLOUDFLARE_CALENDAR_PROBE_CONFIRM: "1",
      }),
    ).toThrow("access token is too long");
    expect(() =>
      resolveCalendarProbeConfig({
        CLOUDFLARE_EDGE_URL: "http://edge.example.test",
        CLOUDFLARE_CALENDAR_PROBE_BEARER_TOKEN: config.bearer,
        CLOUDFLARE_CALENDAR_PROBE_ACCESS_TOKEN: config.accessToken,
        CLOUDFLARE_CALENDAR_PROBE_CONFIRM: "1",
      }),
    ).toThrow("must use https");
  });

  it("verifies save, provider-backed event read, and cleanup without logging tokens", async () => {
    const responses = [
      Response.json({ status: "ok", app_key: "google_calendar" }),
      Response.json({ connected: true, app_key: "google_calendar" }),
      Response.json([]),
      new Response(null, { status: 204 }),
      Response.json({ connected: false, app_key: "google_calendar" }),
    ];
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return responses.shift()!;
    });
    const result = await runCalendarPositiveProbe({ config, fetchImpl });
    expect(result).toEqual({ status: "passed", integration_save: 200, events: 200, cleanup: 204 });
    expect(calls).toHaveLength(5);
    expect(calls[0].init?.method).toBe("PUT");
    expect(calls[0].init?.body).toContain(config.accessToken);
    expect(JSON.stringify(result)).not.toContain(config.accessToken);
    expect(calls[3].init?.method).toBe("DELETE");
  });

  it("attempts cleanup when the provider event read fails", async () => {
    const responses = [
      Response.json({ status: "ok", app_key: "google_calendar" }),
      Response.json({ connected: true, app_key: "google_calendar" }),
      Response.json({ detail: "provider failed" }, { status: 502 }),
      new Response(null, { status: 204 }),
    ];
    const fetchImpl = vi.fn(async () => responses.shift()!);
    await expect(runCalendarPositiveProbe({ config, fetchImpl })).rejects.toThrow(
      "calendar events expected 200, got 502",
    );
    expect(fetchImpl).toHaveBeenCalledTimes(4);
  });
});
