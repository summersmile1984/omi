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
    // CI's Node 22 emits a built-in node:sqlite ExperimentalWarning. Compare
    // exactly with a clean in-memory SQLite process, normalizing only its PID;
    // application stderr (including any additional warning) must still fail.
    const options = {
      cwd: cloudflareDirectory,
      encoding: "utf8",
      timeout: 30000,
      env: { PATH: process.env.PATH || "" },
    };
    const baseline = spawnSync(
      process.execPath,
      [
        "--input-type=module",
        "-e",
        "import {DatabaseSync} from 'node:sqlite'; new DatabaseSync(':memory:').close();",
      ],
      options,
    );
    expect(baseline.status).toBe(0);
    expect(baseline.stdout).toBe("");
    const normalize = (stderr) => stderr.replace(/^\(node:\d+\)/, "(node:PID)");
    expect(normalize(baseline.stderr)).toMatch(
      /^(?:\(node:PID\) ExperimentalWarning: SQLite is an experimental feature and might change at any time\n\(Use `node --trace-warnings \.\.\.` to show where the warning was created\)\n)?$/,
    );
    const result = spawnSync(
      process.execPath,
      ["scripts/dry-run-firebase-identity-import.mjs"],
      options,
    );
    expect(result.status).toBe(0);
    expect(normalize(result.stderr)).toBe(normalize(baseline.stderr));
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
