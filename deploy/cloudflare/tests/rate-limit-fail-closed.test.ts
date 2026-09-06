import { describe, expect, it } from "vitest";
import {
  enforceEdgeRateLimit,
  EDGE_RATE_LIMIT_POLICIES,
} from "../workers/edge/rate-limit";

describe("explicit fail-closed rate admission", () => {
  it.each([true, false])(
    "handles malformed successful provider replies with failClosed=%s",
    async (failClosed) => {
      const env = {
        RATE_LIMITS: {
          idFromName: (name: string) => name,
          get: () => ({ fetch: async () => Response.json({ allowed: true }) }),
        },
      };
      const response = await enforceEdgeRateLimit(
        env as never,
        {
          uid: "legacy-user",
          authority: "better-auth",
          requestId: "malformed-limiter",
        },
        EDGE_RATE_LIMIT_POLICIES["chat:send_message"],
        "malformed-limiter",
        { failClosed }
      );
      if (failClosed) {
        expect(response?.status).toBe(503);
        expect(response?.headers.get("cache-control")).toBe("no-store");
      } else expect(response).toBeNull();
    }
  );
});
