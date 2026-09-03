import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { JobsEnv } from "../workers/jobs/env";
import jobs from "../workers/jobs/index";

class D1Memory {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    this.database.exec("PRAGMA foreign_keys = ON");
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
    const database = this.database;
    const build = (args: Array<string | number | null> = []) => ({
      bind: (...values: Array<string | number | null>) => build(values),
      async first<T>() {
        return (database.prepare(sql).get(...(args as never[])) as T | undefined) || null;
      },
      async all<T>() {
        return {
          results: database.prepare(sql).all(...(args as never[])) as T[],
          meta: { changes: 0 },
        };
      },
      async run() {
        const result = database.prepare(sql).run(...(args as never[]));
        return { meta: { changes: Number(result.changes) } };
      },
    });
    return build();
  }
}

function bucketWith(objects: Record<string, Uint8Array>) {
  const store = new Map(Object.entries(objects));
  return {
    async get(key: string, options?: { range?: { offset: number; length: number } }) {
      const value = store.get(key);
      if (!value) return null;
      const slice = options?.range
        ? value.slice(options.range.offset, options.range.offset + options.range.length)
        : value;
      return { body: new Blob([Buffer.from(slice)]).stream() };
    },
  } as unknown as R2Bucket;
}

const RELEASE_ID = "v1.2.3+45-macos";
const REVIEW_ID = "123e4567-e89b-42d3-a456-426614174000";

function seededEnvironment() {
  const database = new D1Memory();
  database.database
    .prepare(
      "INSERT INTO cf_desktop_release_import_review_batches " +
        "(review_id, source_endpoint, release_id, manifest_sha256, plan_hash, status, reviewed_at, expires_at, updated_at) " +
        "VALUES (?, 'https://api.example.test', ?, ?, ?, 'applied', 1, 2, 1)",
    )
    .run(REVIEW_ID, RELEASE_ID, "a".repeat(64), "b".repeat(64));
  const insert = database.database.prepare(
    "INSERT INTO cf_desktop_release_artifacts " +
      "(release_id, asset_name, review_id, plan_hash, manifest_sha256, source_url, object_key, expected_sha256, " +
      "content_type, size_bytes, status, created_at, updated_at, copied_at) " +
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 1)",
  );
  insert.run(
    RELEASE_ID,
    "Omi.zip",
    REVIEW_ID,
    "b".repeat(64),
    "a".repeat(64),
    `https://github.com/BasedHardware/omi/releases/download/${RELEASE_ID}/Omi.zip`,
    `desktop-releases/${RELEASE_ID}/Omi.zip`,
    `sha256:${"c".repeat(64)}`,
    "application/zip",
    16,
    "copied",
  );
  insert.run(
    RELEASE_ID,
    "Omi.dmg",
    REVIEW_ID,
    "b".repeat(64),
    "a".repeat(64),
    `https://github.com/BasedHardware/omi/releases/download/${RELEASE_ID}/Omi.dmg`,
    `desktop-releases/${RELEASE_ID}/Omi.dmg`,
    `sha256:${"d".repeat(64)}`,
    "application/octet-stream",
    16,
    "queued",
  );
  const env = {
    APP_DB: database as unknown as D1Database,
    DESKTOP_UPDATES: bucketWith({
      [`desktop-releases/${RELEASE_ID}/Omi.zip`]: new TextEncoder().encode(
        "zip-artifact-body",
      ),
    }),
  } as unknown as JobsEnv;
  return { database, env };
}

function artifactUrl(assetName: string) {
  return `https://jobs.test/v2/desktop/artifacts/${encodeURIComponent(RELEASE_ID)}/${assetName}`;
}

describe("desktop artifact serving", () => {
  it("streams a copied artifact with immutable caching and range support", async () => {
    const { env } = seededEnvironment();
    const full = await jobs.fetch(new Request(artifactUrl("Omi.zip")), env);
    expect(full.status).toBe(200);
    expect(full.headers.get("content-type")).toBe("application/zip");
    expect(full.headers.get("cache-control")).toBe(
      "public, max-age=31536000, immutable",
    );
    expect(full.headers.get("etag")).toBe(`"${"c".repeat(64)}"`);
    expect(await full.text()).toBe("zip-artifact-body");

    const ranged = await jobs.fetch(
      new Request(artifactUrl("Omi.zip"), {
        headers: { range: "bytes=4-11" },
      }),
      env,
    );
    expect(ranged.status).toBe(206);
    expect(ranged.headers.get("content-range")).toBe("bytes 4-11/16");
    expect(await ranged.text()).toBe("artifact");

    const unsatisfiable = await jobs.fetch(
      new Request(artifactUrl("Omi.zip"), {
        headers: { range: "bytes=99-120" },
      }),
      env,
    );
    expect(unsatisfiable.status).toBe(416);
    expect(unsatisfiable.headers.get("content-range")).toBe("bytes */16");
  });

  it("refuses uncopied ledger rows, unknown assets, and missing storage", async () => {
    const { env } = seededEnvironment();
    // Ledger row exists but the digest-verified copy never completed.
    const queued = await jobs.fetch(new Request(artifactUrl("Omi.dmg")), env);
    expect(queued.status).toBe(404);

    const unknown = await jobs.fetch(new Request(artifactUrl("Other.bin")), env);
    expect(unknown.status).toBe(404);

    const withoutBucket = await jobs.fetch(
      new Request(artifactUrl("Omi.zip")),
      { ...(env as object), DESKTOP_UPDATES: undefined } as unknown as JobsEnv,
    );
    expect(withoutBucket.status).toBe(503);

    // Ledger says copied but the object vanished: loud 503, no redirect.
    const emptyBucket = await jobs.fetch(
      new Request(artifactUrl("Omi.zip")),
      { ...(env as object), DESKTOP_UPDATES: bucketWith({}) } as unknown as JobsEnv,
    );
    expect(emptyBucket.status).toBe(503);
  });
});
