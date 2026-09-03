import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { runFirebaseIdentityDryRun } from "../scripts/dry-run-firebase-identity-import.mjs";

const cloudflareDirectory = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);

describe("Firebase identity import dry-run", () => {
  it("exercises replay, conflict, revocation, and deletion fences without external credentials", async () => {
    await expect(runFirebaseIdentityDryRun()).resolves.toMatchObject({
      status: "passed",
      fixture: "synthetic",
      network_requests: 0,
      users: 1,
      accounts: 1,
      canonical_sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
      idempotent_replay: true,
      source_conflict_rejected: true,
      revoked_projection_rejected: true,
      deletion_fence_rejected: true,
    });
  });

  it("is executable through the package staging preflight command", () => {
    const result = spawnSync(
      process.execPath,
      ["scripts/dry-run-firebase-identity-import.mjs"],
      {
        cwd: cloudflareDirectory,
        encoding: "utf8",
        env: {
          PATH: process.env.PATH || "",
        },
      },
    );
    expect(result.status).toBe(0);
    expect(result.stderr).toBe("");
    expect(JSON.parse(result.stdout)).toMatchObject({
      status: "passed",
      fixture: "synthetic",
      network_requests: 0,
      idempotent_replay: true,
      source_conflict_rejected: true,
      revoked_projection_rejected: true,
      deletion_fence_rejected: true,
    });
  });
});
