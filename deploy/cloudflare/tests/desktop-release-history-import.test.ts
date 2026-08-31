import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { describe, expect, it } from "vitest";
import type { JobsEnv } from "../workers/jobs/env";
import { registerDesktopReleaseHistoryImportRoutes } from "../workers/jobs/desktop-release-history-import";

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");
  private batchTail = Promise.resolve();

  constructor() {
    const directory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/app",
    );
    for (const filename of readdirSync(directory)
      .filter((value) => value.endsWith(".sql"))
      .sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string) {
    const build = (args: unknown[] = []) => ({
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...(args as never[])) as T | undefined) ?? null,
      all: async <T>() => ({
        results: this.database.prepare(sql).all(...(args as never[])) as T[],
      }),
      run: async () => ({
        meta: {
          changes: Number(this.database.prepare(sql).run(...(args as never[])).changes),
        },
      }),
    });
    return build();
  }

  async batch(statements: Array<ReturnType<SqliteD1["prepare"]>>) {
    let release!: () => void;
    const next = new Promise<void>((resolve) => {
      release = resolve;
    });
    const prior = this.batchTail;
    this.batchTail = next;
    await prior;
    this.database.exec("BEGIN");
    try {
      const results = [];
      for (const statement of statements) {
        const run = await statement.run();
        results.push({ success: true, meta: run.meta });
      }
      this.database.exec("COMMIT");
      return results;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    } finally {
      release();
    }
  }

  close() {
    this.database.close();
  }
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function manifest() {
  const releaseId = "v1.2.3+10203-macos";
  return {
    schema_version: 1,
    release_id: releaseId,
    platform: "macos",
    version: "1.2.3",
    build_number: 10203,
    app_source_sha: "a".repeat(40),
    zip_url: `https://github.com/BasedHardware/omi/releases/download/${releaseId}/Omi.zip`,
    zip_sha256: `sha256:${"b".repeat(64)}`,
    dmg_url: `https://github.com/BasedHardware/omi/releases/download/${releaseId}/omi.dmg`,
    dmg_sha256: `sha256:${"c".repeat(64)}`,
    ed_signature: "ed-signature",
    qualification_evidence_asset: `qualification-evidence-${releaseId}.json`,
    qualification_evidence_sha256: `sha256:${"d".repeat(64)}`,
    qualification_tier: "T2",
    qualification_passed: true,
    backend_mode: "app_only",
    compatibility_contract: {
      schema_version: 1,
      app_release_id: releaseId,
      app_version: "1.2.3",
      app_build_number: 10203,
      backend_mode: "app_only",
      environment_contract_version: "desktop-backend-env-v1",
    },
    environment_contract_version: "desktop-backend-env-v1",
    created_at: "2026-08-31T00:00:00Z",
  };
}

function plan() {
  const value = manifest();
  const releaseId = value.release_id;
  const digest = sha256(stableJson(value));
  const source = {
    kind: "legacy-api",
    endpoint: `https://legacy.example/v2/desktop/releases/${encodeURIComponent(releaseId)}`,
    release_id: releaseId,
    manifest_sha256: digest,
  };
  return {
    mode: "dry-run",
    schema_version: 1,
    source,
    manifest: value,
    manifest_sha256: digest,
    plan_hash: sha256(stableJson({ schema_version: 1, source, manifest_sha256: digest })),
    action: "stage",
    status: "planned",
  };
}

function environment(enabled = true) {
  const database = new SqliteD1();
  const calls: Request[] = [];
  const env = {
    APP_DB: database,
    ADMIN_KEY: "desktop-release-history-admin",
    DESKTOP_RELEASE_HISTORY_IMPORT_STAGING_ENABLED: enabled ? "true" : "false",
    API_CORE: {
      fetch: async (request: Request) => {
        calls.push(request);
        const body = JSON.parse(await request.clone().text()) as Record<string, unknown>;
        const canonical = stableJson(body);
        database.database.prepare(
          "INSERT INTO cf_desktop_release_manifests (release_id, manifest_json, manifest_sha256, created_at) VALUES (?, ?, ?, 1) ON CONFLICT(release_id) DO NOTHING",
        ).run(String(body.release_id), canonical, sha256(canonical));
        return new Response(
          JSON.stringify({
            success: true,
            manifest: body,
            manifest_sha256: sha256(canonical),
          }),
          { status: 201, headers: { "content-type": "application/json" } },
        );
      },
    },
  } as unknown as JobsEnv;
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerDesktopReleaseHistoryImportRoutes(app);
  return { app, database, env, calls };
}

function adminHeaders() {
  return {
    "secret-key": "desktop-release-history-admin",
    "content-type": "application/json",
  };
}

describe("Cloudflare reviewed desktop release history executor", () => {
  it("is fail-closed by default and requires the operator key", async () => {
    const { app, database, env } = environment(false);
    try {
      const disabled = await app.request(
        "/internal/desktop-release-history/reviews",
        { method: "POST", headers: adminHeaders(), body: JSON.stringify(plan()) },
        env,
      );
      expect(disabled.status).toBe(503);
      env.DESKTOP_RELEASE_HISTORY_IMPORT_STAGING_ENABLED = "true";
      const unauthorized = await app.request(
        "/internal/desktop-release-history/reviews",
        { method: "POST", body: JSON.stringify(plan()) },
        env,
      );
      expect(unauthorized.status).toBe(403);
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_desktop_release_import_review_batches").get()).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });

  it("reviews, applies through API Core's immutable contract, and retries idempotently", async () => {
    const { app, database, env, calls } = environment();
    try {
      const reviewed = await app.request(
        "/internal/desktop-release-history/reviews",
        { method: "POST", headers: adminHeaders(), body: JSON.stringify(plan()) },
        env,
      );
      expect(reviewed.status).toBe(201);
      const reviewBody = (await reviewed.json()) as { review_id: string; release_id: string };
      const applied = await app.request(
        `/internal/desktop-release-history/reviews/${reviewBody.review_id}/apply`,
        { method: "POST", headers: { "secret-key": "desktop-release-history-admin" } },
        env,
      );
      expect(applied.status).toBe(200);
      expect(await applied.json()).toMatchObject({ status: "applied", applied_count: 1, already_applied_count: 0 });
      expect(calls).toHaveLength(1);
      expect(new URL(calls[0].url).pathname).toBe("/v2/desktop/releases");
      expect(calls[0].headers.get("secret-key")).toBe("desktop-release-history-admin");
      expect(database.database.prepare("SELECT status FROM cf_desktop_release_import_applies").get()).toMatchObject({ status: "applied" });
      const repeated = await app.request(
        `/internal/desktop-release-history/reviews/${reviewBody.review_id}/apply`,
        { method: "POST", headers: { "secret-key": "desktop-release-history-admin" } },
        env,
      );
      expect(repeated.status).toBe(200);
      expect(await repeated.json()).toMatchObject({ status: "applied", applied_count: 1, already_applied_count: 1 });
      expect(calls).toHaveLength(1);
    } finally {
      database.close();
    }
  });

  it("rejects tampered plans and expired reviews without a destination call", async () => {
    const { app, database, env, calls } = environment();
    try {
      const tampered = plan();
      tampered.manifest.ed_signature = "tampered";
      const rejected = await app.request(
        "/internal/desktop-release-history/reviews",
        { method: "POST", headers: adminHeaders(), body: JSON.stringify(tampered) },
        env,
      );
      expect(rejected.status).toBe(422);
      const reviewed = await app.request(
        "/internal/desktop-release-history/reviews",
        { method: "POST", headers: adminHeaders(), body: JSON.stringify(plan()) },
        env,
      );
      const reviewId = ((await reviewed.json()) as { review_id: string }).review_id;
      database.database.prepare("UPDATE cf_desktop_release_import_review_batches SET reviewed_at = 1, expires_at = 2 WHERE review_id = ?").run(reviewId);
      const expired = await app.request(
        `/internal/desktop-release-history/reviews/${reviewId}/apply`,
        { method: "POST", headers: { "secret-key": "desktop-release-history-admin" } },
        env,
      );
      expect(expired.status).toBe(409);
      expect(calls).toHaveLength(0);
    } finally {
      database.close();
    }
  });
});
