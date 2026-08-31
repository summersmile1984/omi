import { describe, expect, it } from "vitest";
import edge from "../workers/edge/index";

const rawService = (handler: (request: Request) => Promise<Response> | Response) =>
  ({ fetch: handler }) as Fetcher;

const rateLimits = () =>
  ({
    idFromName(name: string) {
      return name;
    },
    get() {
      return {
        fetch: async () =>
          Response.json({
            allowed: true,
            limit: 5,
            remaining: 4,
            retryAfter: 0,
            resetAt: Date.now() + 3_600_000,
          }),
      };
    },
  }) as unknown as DurableObjectNamespace;

describe("Edge Phone/Twilio routing", () => {
  it("authenticates and forwards phone REST calls to Jobs", async () => {
    const forwarded: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "edge-test-secret",
      AUTH: rawService(async (request) => {
        expect(new URL(request.url).pathname).toBe("/internal/verify");
        return Response.json({ uid: "phone-user", authority: "better-auth" });
      }),
      API_CORE: rawService(async (request) => {
        expect(new URL(request.url).pathname).toBe("/v1/account/cutover/control");
        return Response.json({
          state: "new",
          product_traffic_allowed: true,
          migration: { destination_backend_bound: true },
        });
      }),
      JOBS: rawService(async (request) => {
        forwarded.push(request);
        return Response.json({ numbers: [] });
      }),
      RATE_LIMITS: rateLimits(),
    };

    const response = await edge.fetch(
      new Request("https://edge.test/v1/phone/numbers", {
        headers: { authorization: "Bearer opaque-session", cookie: "must-not-forward" },
      }),
      env as never,
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ numbers: [] });
    expect(forwarded).toHaveLength(1);
    expect(new URL(forwarded[0].url).pathname).toBe("/v1/phone/numbers");
    expect(forwarded[0].headers.get("authorization")).toBeNull();
    expect(forwarded[0].headers.get("cookie")).toBeNull();
    expect(forwarded[0].headers.get("x-omi-auth-context")).toBeTruthy();
    expect(forwarded[0].headers.get("x-omi-internal-signature")).toBeTruthy();
  });

  it("keeps the Twilio signature/body on the public webhook path", async () => {
    const forwarded: Request[] = [];
    const env = {
      JOBS: rawService(async (request) => {
        forwarded.push(request);
        return new Response("<Response />", { headers: { "content-type": "text/xml" } });
      }),
    };
    const response = await edge.fetch(
      new Request("https://edge.test/v1/phone/twiml", {
        method: "POST",
        headers: {
          "content-type": "application/x-www-form-urlencoded",
          "x-twilio-signature": "opaque-signature",
          cookie: "must-not-forward",
        },
        body: "To=%2B15551234567&From=client%3Aphone-user&CallSid=CA123",
      }),
      env as never,
    );

    expect(response.status).toBe(200);
    expect(forwarded).toHaveLength(1);
    expect(forwarded[0].headers.get("x-twilio-signature")).toBe("opaque-signature");
    expect(forwarded[0].headers.get("cookie")).toBeNull();
    await expect(forwarded[0].text()).resolves.toBe("To=%2B15551234567&From=client%3Aphone-user&CallSid=CA123");
  });
});
