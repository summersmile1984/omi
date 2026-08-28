import { describe, expect, it } from "vitest";
import {
  stagingHealthTargets,
  verifyStagingHealth,
} from "../scripts/deploy-health.mjs";

describe("staging deployment health", () => {
  it("checks the two public entrypoints and keeps internal readiness behind bindings", () => {
    const targets = stagingHealthTargets({ edge: "https://edge.test/ready" });
    expect(targets.map((target) => target.name)).toEqual(["edge", "web"]);
    expect(targets[0].url).toBe("https://edge.test/ready");
    expect(targets[1].url.endsWith("/api/worker-ready")).toBe(true);
  });

  it("fails the release when one readiness endpoint is not healthy", async () => {
    const calls: string[] = [];
    const fetchImpl = async (url: string) => {
      calls.push(url);
      return new Response(null, {
        status: url.includes("worker-ready") ? 503 : 200,
      });
    };
    await expect(verifyStagingHealth({ fetchImpl })).rejects.toThrow(
      "web expected HTTP 200, received HTTP 503",
    );
    expect(calls).toHaveLength(2);
  });

  it("returns a status map after consuming every health body", async () => {
    const calls: string[] = [];
    const fetchImpl = async (url: string) => {
      calls.push(url);
      return new Response(new Uint8Array([1]), { status: 200 });
    };
    await expect(verifyStagingHealth({ fetchImpl })).resolves.toEqual({
      edge: 200,
      web: 200,
    });
    expect(calls).toHaveLength(2);
  });
});
