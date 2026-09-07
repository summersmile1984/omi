import { describe, expect, it } from "vitest";
import edge from "../workers/edge/index";
import { verifyRequestAuthContext } from "../workers/shared/auth-context";

const path = "/v3/memories/batch";
function fixture(authenticated = true) {
  const forwarded: Request[] = [];
  const env = {
    INTERNAL_ASSERTION_SECRET: "synthetic-memory-batch-secret",
    AUTH: {
      fetch: async () =>
        Response.json(
          authenticated ? { uid: "owner", authority: "better-auth" } : {},
          { status: authenticated ? 200 : 401 }
        ),
    },
    RATE_LIMITS: {
      idFromName: (name: string) => name,
      get: () => ({
        fetch: async () =>
          Response.json({
            allowed: true,
            limit: 300,
            remaining: 299,
            retryAfter: 0,
            resetAt: Date.now() + 60_000,
          }),
      }),
    },
    API_CORE: {
      fetch: async (request: Request) => {
        if (new URL(request.url).pathname === "/v1/account/cutover/control")
          return Response.json({
            state: "new",
            client_action: "none",
            product_traffic_allowed: true,
            migration: { destination_backend_bound: true },
          });
        forwarded.push(request);
        return Response.json({ created_count: 100 });
      },
    },
  };
  return { env, forwarded };
}

function request(body: BodyInit, extra: Record<string, string> = {}) {
  return new Request("https://edge.test" + path, {
    method: "POST",
    body,
    duplex: "half",
    headers: {
      authorization: "Bearer synthetic",
      "content-type": "application/json",
      ...extra,
    },
  } as RequestInit);
}

describe("memory intake size admission", () => {
  it("forwards exactly 1 MB unchanged with the authenticated identity", async () => {
    const { env, forwarded } = fixture();
    const json = JSON.stringify({ memories: [{ content: "茶😀" }] });
    const body =
      json + " ".repeat(1_000_000 - new TextEncoder().encode(json).length);
    const response = await edge.fetch(request(body), env as never);
    expect(response.status).toBe(200);
    expect(forwarded).toHaveLength(1);
    expect(await forwarded[0].text()).toBe(body);
    expect(forwarded[0].headers.get("content-length")).toBe("1000000");
    expect(
      (
        await verifyRequestAuthContext(
          forwarded[0],
          "api-core",
          env.INTERNAL_ASSERTION_SECRET
        )
      )?.uid
    ).toBe("owner");
  });

  it.each<Record<string, string>>([{}, { "content-length": "1" }, { "content-length": "1000001" }])(
    "rejects an oversized stream before Core regardless of length header %j",
    async (headers) => {
      const { env, forwarded } = fixture();
      let cancelled = false;
      let reads = 0;
      const body = new ReadableStream<Uint8Array>(
        {
          pull(controller) {
            reads++;
            controller.enqueue(new Uint8Array(reads === 1 ? 1_000_000 : 1));
          },
          cancel() {
            cancelled = true;
          },
        },
        { highWaterMark: 0 }
      );
      const response = await edge.fetch(request(body, headers), env as never);
      expect(response.status).toBe(413);
      expect(await response.json()).toEqual({
        error: "memory_batch_too_large",
        max_bytes: 1_000_000,
        max_memories: 100,
      });
      expect(forwarded).toHaveLength(0);
      expect(cancelled).toBe(true);
      // Request construction may prefetch a chunk before the handler executes.
      expect(reads).toBeLessThanOrEqual(
        headers["content-length"] === "1000001" ? 1 : 2
      );
    }
  );

  it("keeps authentication ahead of body handling", async () => {
    const { env, forwarded } = fixture(false);
    const response = await edge.fetch(
      request("x".repeat(1_000_001)),
      env as never
    );
    expect(response.status).toBe(401);
    expect(forwarded).toHaveLength(0);
  });
});
