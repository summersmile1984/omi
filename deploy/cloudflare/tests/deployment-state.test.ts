import { describe, expect, it } from "vitest";
import {
  activeVersionFromStatus,
  assertValidDeploymentOrder,
  createDeploymentSnapshot,
  rollbackPlan,
  STAGING_DEPLOYMENTS,
  STAGING_WORKERS,
} from "../scripts/deployment-state.mjs";

describe("Cloudflare deployment snapshots", () => {
  it("captures one fully active version for every staging Worker", () => {
    const statuses = Object.fromEntries(
      STAGING_WORKERS.map((workerName, index) => [
        workerName,
        { versions: [{ version_id: `version-${index}`, percentage: 100 }] },
      ]),
    );
    const snapshot = createDeploymentSnapshot(
      statuses,
      new Date("2026-08-28T00:00:00Z"),
    );
    expect(snapshot.environment).toBe("staging");
    expect(snapshot.createdAt).toBe("2026-08-28T00:00:00.000Z");
    expect(snapshot.workers["omi-cf-edge-staging"]).toBe("version-6");
    expect(rollbackPlan(snapshot).map((step) => step.workerName)).toEqual([
      "omi-web-app-staging",
      "omi-cf-edge-staging",
      "omi-cf-jobs-staging",
      "omi-cf-realtime-staging",
      "omi-cf-api-ai-staging",
      "omi-cf-api-core-staging",
      "omi-cf-rate-limit-staging",
      "omi-cf-auth-staging",
    ]);
  });

  it("rejects a split deployment because rollback authority is ambiguous", () => {
    expect(() =>
      activeVersionFromStatus(
        {
          versions: [
            { version_id: "old", percentage: 90 },
            { version_id: "candidate", percentage: 10 },
          ],
        },
        "omi-cf-edge-staging",
      ),
    ).toThrow("exactly one 100% active version");
  });

  it("requires every service binding owner to deploy before its consumers", () => {
    expect(() => assertValidDeploymentOrder()).not.toThrow();
    expect(() =>
      assertValidDeploymentOrder([...STAGING_DEPLOYMENTS].reverse()),
    ).toThrow("must deploy after dependency");
  });

  it("rejects incomplete or tampered snapshots", () => {
    expect(() => rollbackPlan({ version: 1, environment: "prod" })).toThrow(
      "invalid Cloudflare staging deployment snapshot",
    );
    expect(() =>
      rollbackPlan({
        version: 1,
        environment: "staging",
        workers: Object.fromEntries(
          STAGING_WORKERS.map((workerName) => [workerName, "../bad"]),
        ),
      }),
    ).toThrow("invalid rollback version");
  });
});
