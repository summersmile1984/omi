import { describe, expect, it, vi } from "vitest";
import { cloudflareProductTrafficDenial } from "../workers/edge/cutover";
import type { EdgeEnv } from "../workers/edge/env";
import type { AuthContext } from "../workers/shared/auth-context";

const auth: AuthContext = {
  uid: "user-1",
  authority: "better-auth",
  requestId: "request-1",
};

function envWithFence(
  fetchImpl: typeof fetch,
  fenceEnabled?: string,
): EdgeEnv {
  return {
    API_CORE: { fetch: vi.fn(fetchImpl) },
    ACCOUNT_ACTIVATION_FENCE_ENABLED: fenceEnabled,
    INTERNAL_ASSERTION_SECRET: "test-secret",
  } as unknown as EdgeEnv;
}

describe("cloudflareProductTrafficDenial", () => {
  it("lets traffic through without calling api-core when the fence is unset (new-brand default)", async () => {
    const fetchImpl = vi.fn();
    const env = envWithFence(fetchImpl, undefined);
    const result = await cloudflareProductTrafficDenial(
      new Request("https://edge.internal/v1/x"),
      env,
      auth,
      "req-1",
    );
    expect(result).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("lets traffic through when the fence is explicitly disabled", async () => {
    const fetchImpl = vi.fn();
    const env = envWithFence(fetchImpl, "false");
    const result = await cloudflareProductTrafficDenial(
      new Request("https://edge.internal/v1/x"),
      env,
      auth,
      "req-1",
    );
    expect(result).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("still denies with 409 when enabled and the account is not an active-and-bound migration", async () => {
    const fetchImpl = vi.fn(async () =>
      Response.json({
        state: "pending",
        client_action: "migration_maintenance",
        product_traffic_allowed: false,
        migration: { destination_backend_bound: false },
      }),
    );
    const env = envWithFence(fetchImpl, "true");
    const result = await cloudflareProductTrafficDenial(
      new Request("https://edge.internal/v1/x"),
      env,
      auth,
      "req-1",
    );
    expect(result).not.toBeNull();
    expect(result?.status).toBe(409);
    const body = (await result?.json()) as { client_action: string };
    expect(body.client_action).toBe("migration_maintenance");
  });

  it("still lets traffic through when enabled and the account is active and destination-bound", async () => {
    const fetchImpl = vi.fn(async () =>
      Response.json({
        state: "new",
        product_traffic_allowed: true,
        migration: { destination_backend_bound: true },
      }),
    );
    const env = envWithFence(fetchImpl, "true");
    const result = await cloudflareProductTrafficDenial(
      new Request("https://edge.internal/v1/x"),
      env,
      auth,
      "req-1",
    );
    expect(result).toBeNull();
    expect(fetchImpl).toHaveBeenCalledOnce();
  });
});
