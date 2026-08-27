import { describe, expect, it } from "vitest";
import { encodeAuthContext, signAuthContext } from "../workers/shared/auth-context";
import realtime from "../workers/realtime/index";

const context = encodeAuthContext({ uid: "user-1", authority: "better-auth", requestId: "req-1" });

describe("realtime gateway", () => {
  it("rejects a forged internal context before the websocket upgrade", async () => {
    const response = await realtime.fetch(
      new Request("https://realtime.test/v4/listen", {
        headers: {
          upgrade: "websocket",
          "x-omi-auth-context": context,
          "x-omi-internal-signature": "forged",
        },
      }),
      { INTERNAL_ASSERTION_SECRET: "test-secret" } as never,
    );
    expect(response.status).toBe(401);
  });

  it("checks the signature before requiring a websocket upgrade", async () => {
    const signature = await signAuthContext(context, "test-secret");
    const response = await realtime.fetch(
      new Request("https://realtime.test/v4/listen", {
        headers: {
          "x-omi-auth-context": context,
          "x-omi-internal-signature": signature || "",
        },
      }),
      { INTERNAL_ASSERTION_SECRET: "test-secret" } as never,
    );
    expect(response.status).toBe(426);
  });
});
