import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  PRODUCTION_CONFIRMATION,
  PRODUCTION_DEPLOYMENTS,
  assertProductionConfirmation,
  assertValidProductionDeploymentOrder,
  createInitialProductionSecrets,
  createProductionDeploymentSnapshot,
  productionRollbackPlan,
  productionUrls,
  renderProductionConfig,
  resolveWorkersSubdomain,
} from "../scripts/production-environment.mjs";

const root = resolve(import.meta.dirname, "..");
const appDatabaseId = "22222222-2222-4222-8222-222222222222";
const authDatabaseId = "11111111-1111-4111-8111-111111111111";

describe("independent Cloudflare production environment", () => {
  it("requires the exact production confirmation", () => {
    expect(() => assertProductionConfirmation({})).toThrow(
      PRODUCTION_CONFIRMATION,
    );
    expect(() =>
      assertProductionConfirmation({
        CLOUDFLARE_PRODUCTION_CONFIRM: PRODUCTION_CONFIRMATION,
      }),
    ).not.toThrow();
  });

  it("builds stable public workers.dev origins", () => {
    expect(resolveWorkersSubdomain("Omi-Prod")).toBe("omi-prod");
    expect(productionUrls("omi-prod")).toEqual({
      auth: "https://omi-cf-auth-production.omi-prod.workers.dev",
      edge: "https://omi-cf-edge-production.omi-prod.workers.dev",
      web: "https://omi-web-app-production.omi-prod.workers.dev",
    });
    expect(() => resolveWorkersSubdomain("bad.domain")).toThrow(
      "invalid Cloudflare",
    );
  });

  it.each([
    "workers/auth/wrangler.jsonc",
    "workers/rate-limit/wrangler.jsonc",
    "python/api-core/wrangler.jsonc",
    "python/api-ai/wrangler.jsonc",
    "workers/realtime/wrangler.jsonc",
    "workers/jobs/wrangler.jsonc",
    "workers/edge/wrangler.jsonc",
  ])("renders an isolated production config from %s", (relativePath) => {
    const rendered = renderProductionConfig(
      readFileSync(resolve(root, relativePath), "utf8"),
      { appDatabaseId, authDatabaseId, subdomain: "omi-prod" },
    );
    expect(rendered).not.toContain("omi-cf-app-staging");
    expect(rendered).not.toContain("omi-cf-auth-staging");
    expect(rendered).not.toContain("omi-cf-jobs-staging");
    expect(rendered).not.toContain("omi-web-app-staging");
    expect(rendered).not.toContain(".summersmile1984.workers.dev");
    if (rendered.includes('"database_name": "omi-cf-app-production"')) {
      expect(rendered).toContain(`"database_id": "${appDatabaseId}"`);
    }
    if (rendered.includes('"database_name": "omi-cf-auth-production"')) {
      expect(rendered).toContain(`"database_id": "${authDatabaseId}"`);
    }
  });

  it("disables unauthenticated DCR and legacy external owners in production", () => {
    const auth = renderProductionConfig(
      readFileSync(resolve(root, "workers/auth/wrangler.jsonc"), "utf8"),
      { appDatabaseId, authDatabaseId },
    );
    const jobs = renderProductionConfig(
      readFileSync(resolve(root, "workers/jobs/wrangler.jsonc"), "utf8"),
      { appDatabaseId, authDatabaseId },
    );
    expect(auth).toContain('"MCP_ALLOW_UNAUTHENTICATED_DCR": "false"');
    expect(jobs).toContain(
      '"LEGACY_EXTERNAL_APP_OAUTH_STAGING_ENABLED": "false"',
    );
    expect(jobs).toContain(
      '"ACCOUNT_CUTOVER_MANIFEST_ID": "isolated-production-v1"',
    );
  });

  it("keeps deployment dependency order fail-closed", () => {
    expect(() => assertValidProductionDeploymentOrder()).not.toThrow();
    expect(() =>
      assertValidProductionDeploymentOrder([...PRODUCTION_DEPLOYMENTS].reverse()),
    ).toThrow("must deploy after dependency");
  });

  it("generates one shared internal secret and per-boundary secrets", () => {
    let counter = 0;
    const secrets = createInitialProductionSecrets(
      "omi-prod",
      () => `secret-${++counter}`,
    );
    const workers = secrets.workers;
    const internal = workers["omi-cf-auth-production"].INTERNAL_ASSERTION_SECRET;
    expect(workers["omi-cf-edge-production"].INTERNAL_ASSERTION_SECRET).toBe(
      internal,
    );
    expect(workers["omi-cf-jobs-production"].INTERNAL_ASSERTION_SECRET).toBe(
      internal,
    );
    expect(workers["omi-cf-auth-production"].BETTER_AUTH_URL).toBe(
      "https://omi-web-app-production.omi-prod.workers.dev",
    );
    expect(workers["omi-cf-edge-production"].BYOK_FINGERPRINT_PEPPER).not.toBe(
      internal,
    );
  });

  it("rolls existing workers back and removes first-release workers", () => {
    const statuses = Object.fromEntries(
      [
        "omi-cf-auth-production",
        "omi-cf-rate-limit-production",
        "omi-cf-api-core-production",
        "omi-cf-api-ai-production",
        "omi-cf-realtime-production",
        "omi-cf-jobs-production",
        "omi-cf-edge-production",
        "omi-web-app-production",
      ].map((name, index) => [
        name,
        index === 0
          ? JSON.stringify({
              versions: [
                {
                  version_id: "11111111-1111-4111-8111-111111111111",
                  percentage: 100,
                },
              ],
            })
          : null,
      ]),
    );
    const snapshot = createProductionDeploymentSnapshot(
      statuses,
      new Date("2026-09-01T00:00:00Z"),
    );
    const plan = productionRollbackPlan(snapshot);
    expect(plan.at(-1)).toEqual({
      action: "rollback",
      workerName: "omi-cf-auth-production",
      versionId: "11111111-1111-4111-8111-111111111111",
    });
    expect(plan[0]).toEqual({
      action: "delete",
      workerName: "omi-web-app-production",
      versionId: null,
    });
  });
});
