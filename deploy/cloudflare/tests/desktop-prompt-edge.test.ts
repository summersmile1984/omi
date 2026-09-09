import { describe, expect, it } from "vitest";
import edge from "../workers/edge/index";
import { verifyRequestAuthContext } from "../workers/shared/auth-context";

describe("desktop prompt routing", () => {
  it("uses the authenticated Core owner and preserves audience query parameters", async () => {
    const seen: Request[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "prompt-routing-test-secret",
      AUTH: {
        fetch: async () =>
          Response.json({ uid: "prompt-user", authority: "better-auth" }),
      },
      API_CORE: {
        fetch: async (request: Request) => {
          seen.push(request);
          return Response.json({ prompts: [] });
        },
      },
    };
    const response = await edge.fetch(
      new Request(
        "https://edge.test/v2/desktop/prompts?channel=beta&build=123",
        {
          headers: {
            authorization: "Bearer test-session",
            cookie: "session=private",
            "x-omi-uid": "forged",
          },
        }
      ),
      env as never
    );
    expect(response.status).toBe(200);
    expect(seen).toHaveLength(1);
    expect(new URL(seen[0].url).search).toBe("?channel=beta&build=123");
    expect(seen[0].headers.has("authorization")).toBe(false);
    expect(seen[0].headers.has("cookie")).toBe(false);
    expect(seen[0].headers.has("x-omi-uid")).toBe(false);
    const identity = await verifyRequestAuthContext(
      seen[0],
      "api-core",
      env.INTERNAL_ASSERTION_SECRET
    );
    expect(identity?.uid).toBe("prompt-user");
  });

  it("does not call the Core owner for an unauthenticated client", async () => {
    const response = await edge.fetch(
      new Request("https://edge.test/v2/desktop/prompts"),
      {} as never
    );
    expect(response.status).toBe(401);
  });
});
