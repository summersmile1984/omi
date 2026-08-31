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
      return url.includes("worker-ready")
        ? new Response(null, { status: 503 })
        : Response.json({ status: "ready" });
    };
    await expect(verifyStagingHealth({ fetchImpl })).rejects.toThrow(
      "web expected HTTP 200, received HTTP 503",
    );
    expect(calls).toHaveLength(2);
  });

  it("rejects a 200 response that is not a ready JSON envelope", async () => {
    await expect(
      verifyStagingHealth({
        fetchImpl: async () =>
          new Response("<html>ok</html>", {
            status: 200,
            headers: { "content-type": "text/html" },
          }),
      }),
    ).rejects.toThrow("edge readiness response was not valid JSON");

    await expect(
      verifyStagingHealth({
        fetchImpl: async () => Response.json({ status: "ok" }),
      }),
    ).rejects.toThrow("edge readiness response did not report status=ready");
  });

  it("returns a status map after validating every readiness body", async () => {
    const calls: string[] = [];
    const fetchImpl = async (url: string) => {
      calls.push(url);
      return Response.json({ status: "ready", service: "edge" });
    };
    await expect(verifyStagingHealth({ fetchImpl })).resolves.toEqual({
      edge: 200,
      web: 200,
    });
    expect(calls).toHaveLength(2);
  });
});
