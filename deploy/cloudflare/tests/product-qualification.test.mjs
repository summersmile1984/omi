import { describe, it, expect, vi } from "vitest";
import { qualifyProduct } from "../contracts/qualify-product.mjs";
import { qualifyDualTarget } from "../../../contracts/deployment/qualify-dual-target.mjs";
import { hostedOrigins } from "../contracts/hosted-product.mjs";

function context(phase) {
  return {
    root: "/source",
    candidate: {
      candidate_digest: "a".repeat(64),
      source: { commit: "b".repeat(40) },
    },
    observations: { release_phase: phase },
    verify: vi.fn(),
  };
}

describe("release product execution", () => {
  it("executes hosted business checks only for observed deployment and binds evidence", async () => {
    const local = vi.fn(async () => ["core:passed"]),
      hosted = vi.fn(async () => ["hosted:passed"]);
    const input = context("deployed");
    const proof = await qualifyProduct(input, { local, hosted });
    expect(local).not.toHaveBeenCalled();
    expect(hosted).toHaveBeenCalledWith(input);
    expect(proof.candidate_digest).toBe(input.candidate.candidate_digest);
    expect(proof.cases).toEqual([{ id: "hosted:passed", result: "pass" }]);
    expect(proof.observation_digest).toMatch(/^[a-f0-9]{64}$/);
  });
  it("propagates a real suite failure without returning a passing proof", async () => {
    await expect(
      qualifyProduct(context("candidate"), {
        local: async () => {
          throw new Error("HTTP contract failed");
        },
      })
    ).rejects.toThrow("HTTP contract failed");
  });
  it("requires exact-source CI and equal executed common cases", async () => {
    vi.stubEnv("FORK_RELEASE_CI_RUN_ID", "42");
    try {
      await expect(
        qualifyDualTarget(context("deployed"), {
          spawn: () => ({ status: 0 }),
          server: async () => ["memory.owner"],
          hosted: () => ["memory.different"],
        })
      ).rejects.toThrow("identical");
      await expect(
        qualifyDualTarget(context("candidate"), {
          spawn: () => ({ status: 1 }),
        })
      ).rejects.toThrow("CI source");
    } finally {
      vi.unstubAllEnvs();
    }
  });
  it("refuses credential-bearing or shared hosted origins", () => {
    const candidate = {
      profiles: {
        cloudflare: {
          profile: {
            api_base_url: "https://api.example.com",
            auth_base_url: "https://auth.example.com",
            web_base_url: "https://web.example.com",
          },
        },
      },
    };
    expect(hostedOrigins(candidate).api).toBe("https://api.example.com");
    candidate.profiles.cloudflare.profile.auth_base_url =
      "https://secret@auth.example.com";
    expect(() => hostedOrigins(candidate)).toThrow("exact HTTPS");
  });
});
