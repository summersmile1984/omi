import { describe, expect, it } from "vitest";
import { stagingHealthTargets, verifyStagingHealth } from "../scripts/deploy-health.mjs";

describe("staging deployment health", () => {
  it("keeps the six Worker readiness targets explicit and overridable", () => {
    const targets = stagingHealthTargets({ edge: "https://edge.test/health" });
    expect(targets.map((target) => target.name)).toEqual([
      "edge",
      "auth",
      "api-core",
      "api-ai",
      "realtime",
      "jobs",
    ]);
    expect(targets[0].url).toBe("https://edge.test/health");
    expect(targets[1].url.endsWith("/ready")).toBe(true);
  });

  it("fails the release when one readiness endpoint is not healthy", async () => {
    const calls: string[] = [];
    const fetchImpl = async (url: string) => {
      calls.push(url);
      return new Response(null, { status: url.includes("api-ai") ? 503 : 200 });
    };
    await expect(verifyStagingHealth({ fetchImpl })).rejects.toThrow(
      "api-ai expected HTTP 200, received HTTP 503",
    );
    expect(calls).toHaveLength(4);
  });

  it("returns a status map after consuming every health body", async () => {
    const calls: string[] = [];
    const fetchImpl = async (url: string) => {
      calls.push(url);
      return new Response(new Uint8Array([1]), { status: 200 });
    };
    await expect(verifyStagingHealth({ fetchImpl })).resolves.toEqual({
      edge: 200,
      auth: 200,
      "api-core": 200,
      "api-ai": 200,
      realtime: 200,
      jobs: 200,
    });
    expect(calls).toHaveLength(6);
  });
});
