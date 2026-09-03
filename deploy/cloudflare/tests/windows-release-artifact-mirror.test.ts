import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { Hono } from "hono";
import { describe, expect, it, vi } from "vitest";
import {
  processWindowsReleaseArtifactMessage,
  registerWindowsReleaseHistoryRoutes,
  normalizedWindowsReleasePlan,
} from "../workers/jobs/windows-release-artifact-mirror";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256(value: Uint8Array | string): string {
  return createHash("sha256").update(value).digest("hex");
}

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");
  constructor() {
    const directory = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../migrations/app");
    for (const filename of readdirSync(directory).filter((value) => value.endsWith(".sql")).sort()) this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
  }
  prepare(sql: string) {
    const build = (args: unknown[] = []) => ({
      bind: (...values: unknown[]) => build(values),
      first: async <T>() => (this.database.prepare(sql).get(...(args as never[])) as T | undefined) ?? null,
      all: async <T>() => ({ results: this.database.prepare(sql).all(...(args as never[])) as T[] }),
      run: async () => ({ meta: { changes: Number(this.database.prepare(sql).run(...(args as never[])).changes) } }),
    });
    return build();
  }
  async batch(statements: Array<ReturnType<SqliteD1["prepare"]>>) {
    this.database.exec("BEGIN");
    try { const results = []; for (const statement of statements) results.push(await statement.run()); this.database.exec("COMMIT"); return results; }
    catch (error) { this.database.exec("ROLLBACK"); throw error; }
  }
  close() { this.database.close(); }
}

class FakeR2 {
  readonly objects = new Map<string, { bytes: Uint8Array; customMetadata: Record<string, string> }>();
  async head(key: string): Promise<R2Object | null> {
    const object = this.objects.get(key);
    return object ? ({ key, size: object.bytes.byteLength, customMetadata: object.customMetadata } as unknown as R2Object) : null;
  }
  async put(key: string, body: ReadableStream<Uint8Array>, options?: R2PutOptions): Promise<void> {
    const reader = body.getReader(); const chunks: Uint8Array[] = []; let size = 0;
    while (true) { const item = await reader.read(); if (item.done) break; chunks.push(item.value); size += item.value.byteLength; }
    const bytes = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    this.objects.set(key, { bytes, customMetadata: { ...(options?.customMetadata || {}) } });
  }
  async delete(key: string): Promise<void> { this.objects.delete(key); }
}

function plan() {
  const releaseId = "v1.2.3-windows"; const version = "1.2.3";
  const bytes = { exe: new TextEncoder().encode("windows-exe"), blockmap: new TextEncoder().encode("windows-blockmap"), latest_yml: new TextEncoder().encode("version: 1.2.3\n") };
  const assets = {
    exe: { url: `https://github.com/BasedHardware/omi/releases/download/${releaseId}/Omi-for-Windows-Setup-${version}.exe`, sha256: sha256(bytes.exe) },
    blockmap: { url: `https://github.com/BasedHardware/omi/releases/download/${releaseId}/Omi-for-Windows-Setup-${version}.exe.blockmap`, sha256: sha256(bytes.blockmap) },
    latest_yml: { url: `https://github.com/BasedHardware/omi/releases/download/${releaseId}/latest.yml`, sha256: sha256(bytes.latest_yml) },
  };
  const withoutHash = { mode: "dry-run", schema_version: 1, source: { kind: "github-release", repository: "BasedHardware/omi", release_id: releaseId, release_fingerprint: "f".repeat(64) }, release: { release_id: releaseId, version, build_number: 1203, prerelease: true, channel: "beta", assets }, action: "stage", status: "planned" };
  return { ...withoutHash, plan_hash: sha256(stableJson(withoutHash)), bytes };
}

function setup(enabled = true) {
  const database = new SqliteD1(); const updates = new FakeR2(); const jobs: JobMessage[] = [];
  const env = { APP_DB: database, ADMIN_KEY: "windows-admin", WINDOWS_RELEASE_HISTORY_IMPORT_STAGING_ENABLED: enabled ? "true" : "false", DESKTOP_UPDATES: updates, JOBS: { send: async (message: JobMessage) => { jobs.push(message); } } } as unknown as JobsEnv;
  const app = new Hono<{ Bindings: JobsEnv }>(); registerWindowsReleaseHistoryRoutes(app);
  return { database, updates, jobs, env, app };
}

const headers = () => ({ "secret-key": "windows-admin", "content-type": "application/json" });

describe("reviewed Windows release artifact mirror", () => {
  it("is fail-closed and rejects a tampered plan", async () => {
    const { app, env, database } = setup(false); const { bytes: _bytes, ...value } = plan();
    try {
      expect((await app.request("/internal/windows-release-history/reviews", { method: "POST", headers: headers(), body: JSON.stringify(value) }, env)).status).toBe(503);
      env.WINDOWS_RELEASE_HISTORY_IMPORT_STAGING_ENABLED = "true";
      const rejected = await app.request("/internal/windows-release-history/reviews", { method: "POST", headers: headers(), body: JSON.stringify({ ...value, plan_hash: "0".repeat(64) }) }, env);
      expect(rejected.status).toBe(422);
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_windows_release_artifact_review_batches").get()).toMatchObject({ count: 0 });
    } finally { database.close(); }
  });

  it("reviews, applies, queues, copies and idempotently replays all three artifacts", async () => {
    const { app, env, jobs, updates, database } = setup(); const { bytes, ...value } = plan(); const originalFetch = globalThis.fetch;
    vi.stubGlobal("fetch", async (request: RequestInfo | URL) => { const url = String(request); const key = url.endsWith(".blockmap") ? "blockmap" : url.endsWith("latest.yml") ? "latest_yml" : "exe"; return new Response(bytes[key], { status: 200 }); });
    try {
      expect(normalizedWindowsReleasePlan(value)).not.toBeNull();
      const review = await app.request("/internal/windows-release-history/reviews", { method: "POST", headers: headers(), body: JSON.stringify(value) }, env); expect(review.status).toBe(201);
      const { review_id: reviewId } = await review.json() as { review_id: string };
      expect((await app.request(`/internal/windows-release-history/reviews/${reviewId}/apply`, { method: "POST", headers: headers() }, env)).status).toBe(200);
      const queuedResponse = await app.request(`/internal/windows-release-history/reviews/${reviewId}/artifacts/apply`, { method: "POST", headers: headers() }, env); expect(queuedResponse.status).toBe(202);
      expect(jobs).toHaveLength(3);
      for (const job of jobs) { let acked = false; await processWindowsReleaseArtifactMessage({ body: job, attempts: 1, ack: () => { acked = true; }, retry: () => { throw new Error("unexpected retry"); } } as never, env); expect(acked).toBe(true); }
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_windows_release_artifacts WHERE status = 'copied'").get()).toMatchObject({ count: 3 }); expect(updates.objects.size).toBe(3);
      expect((await app.request(`/internal/windows-release-history/reviews/${reviewId}/artifacts/apply`, { method: "POST", headers: headers() }, env)).status).toBe(200); expect(jobs).toHaveLength(3);
    } finally { vi.stubGlobal("fetch", originalFetch); database.close(); }
  });

  it("accepts only the bounded GitHub signed-CDN redirect form", async () => {
    const { app, env, jobs, updates, database } = setup(); const { bytes, ...value } = plan(); const originalFetch = globalThis.fetch;
    let calls = 0;
    vi.stubGlobal("fetch", async () => {
      calls += 1;
      if (calls === 1) return new Response(null, { status: 302, headers: { location: "https://release-assets.githubusercontent.com/asset?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20260901T000000Z&X-Amz-Expires=60&X-Amz-Signature=abc" } });
      return new Response(bytes.exe, { status: 200 });
    });
    try {
      const review = await app.request("/internal/windows-release-history/reviews", { method: "POST", headers: headers(), body: JSON.stringify(value) }, env); const { review_id: reviewId } = await review.json() as { review_id: string };
      await app.request(`/internal/windows-release-history/reviews/${reviewId}/apply`, { method: "POST", headers: headers() }, env); await app.request(`/internal/windows-release-history/reviews/${reviewId}/artifacts/apply`, { method: "POST", headers: headers() }, env);
      await processWindowsReleaseArtifactMessage({ body: jobs[0], attempts: 1, ack: () => undefined, retry: () => { throw new Error("unexpected retry"); } } as never, env);
      expect(calls).toBe(2); expect(updates.objects.size).toBe(1);
    } finally { vi.stubGlobal("fetch", originalFetch); database.close(); }
  });

  it("marks a digest mismatch terminal and removes any partial object", async () => {
    const { app, env, jobs, updates, database } = setup(); const { bytes: _bytes, ...value } = plan(); const originalFetch = globalThis.fetch;
    vi.stubGlobal("fetch", async () => new Response("wrong-bytes", { status: 200 }));
    try {
      const review = await app.request("/internal/windows-release-history/reviews", { method: "POST", headers: headers(), body: JSON.stringify(value) }, env); const { review_id: reviewId } = await review.json() as { review_id: string };
      await app.request(`/internal/windows-release-history/reviews/${reviewId}/apply`, { method: "POST", headers: headers() }, env); await app.request(`/internal/windows-release-history/reviews/${reviewId}/artifacts/apply`, { method: "POST", headers: headers() }, env);
      let acked = false; await processWindowsReleaseArtifactMessage({ body: jobs[0], attempts: 1, ack: () => { acked = true; }, retry: () => { throw new Error("unexpected retry"); } } as never, env); expect(acked).toBe(true); expect(updates.objects.size).toBe(0);
      expect(database.database.prepare("SELECT status, last_error FROM cf_windows_release_artifacts ORDER BY asset_name LIMIT 1").get()).toMatchObject({ status: "failed", last_error: "artifact_digest_mismatch" });
    } finally { vi.stubGlobal("fetch", originalFetch); database.close(); }
  });
});
