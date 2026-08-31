import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import type { JobsEnv } from "../workers/jobs/env";
import jobs from "../workers/jobs/index";
import { createSignedAuthContext } from "../workers/shared/auth-context";

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
    return {
      bind(...args: Array<string | number | bigint | Uint8Array | null>) {
        return {
          async first<T>() {
            return (
              (database.prepare(sql).get(...args) as T | undefined) || null
            );
          },
          async all<T>() {
            return {
              results: database.prepare(sql).all(...args) as T[],
              meta: { changes: 0 },
            };
          },
          async run() {
            const result = database.prepare(sql).run(...args);
            return { meta: { changes: Number(result.changes) } };
          },
        };
      },
    };
  }
}

class R2Memory {
  readonly objects = new Map<string, Uint8Array>();

  async put(key: string, value: Uint8Array) {
    this.objects.set(key, Uint8Array.from(value));
  }

  async delete(key: string) {
    this.objects.delete(key);
  }
}

const PNG = Uint8Array.from([
  137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 82,
]);

async function environment() {
  const database = new D1Memory();
  const assets = new R2Memory();
  const secret = "persona-mutation-secret";
  const ai = {
    run: vi.fn(async () => ({ response: "A curious, warm-minded builder." })),
  };
  const env = {
    AUTH: {
      fetch: vi.fn(async () =>
        Response.json({
          uid: "persona-owner",
          name: "Persona Owner",
          email: "owner@example.com",
        }),
      ),
    },
    APP_DB: database as unknown as D1Database,
    ASSETS: assets as unknown as R2Bucket,
    CONVERSATION_RECORDINGS: assets as unknown as R2Bucket,
    SPEECH_PROFILES: assets as unknown as R2Bucket,
    AI: ai,
    MEMORY_VECTORS: {},
    ACTION_ITEM_VECTORS: {},
    CONVERSATION_VECTORS: {},
    TRANSCRIPT_CHUNK_VECTORS: {},
    X_POST_VECTORS: {},
    JOBS: { send: vi.fn() },
    SYNC_FRESH: { send: vi.fn() },
    SYNC_BACKFILL: { send: vi.fn() },
    INTERNAL_ASSERTION_SECRET: secret,
    PUBLIC_API_BASE_URL: "https://edge.test",
  } as unknown as JobsEnv;
  const signed = await createSignedAuthContext(
    {
      uid: "persona-owner",
      authority: "better-auth",
      requestId: "persona-create",
    },
    "jobs",
    "POST",
    "/v1/personas",
    secret,
  );
  if (!signed) throw new Error("missing signed context");
  return { database, assets, ai, env, signed };
}

function form(data: Record<string, unknown>, image = PNG) {
  const body = new FormData();
  body.set("persona_data", JSON.stringify(data));
  body.set("file", new File([image], "persona.png", { type: "image/png" }));
  return body;
}

describe("Cloudflare Persona creation boundary", () => {
  it("creates a D1/R2 persona and deduplicates a retried multipart request", async () => {
    const state = await environment();
    const headers = {
      "x-omi-auth-context": state.signed.encoded,
      "x-omi-internal-signature": state.signed.signature,
    };
    const data = {
      name: "Cloud Persona",
      username: "cloudpersona",
      connected_accounts: ["omi"],
      private: true,
    };
    const first = await jobs.fetch(
      new Request("https://jobs.test/v1/personas", {
        method: "POST",
        headers,
        body: form(data),
      }),
      state.env,
    );
    expect(first.status).toBe(200);
    const firstBody = (await first.json()) as {
      app_id: string;
      username: string;
    };
    expect(firstBody.username).toBe("cloudpersona");
    expect(firstBody.app_id).toMatch(/^cf_persona_[0-9a-f]{32}$/);
    expect(state.assets.objects.size).toBe(1);
    expect(
      state.database.database
        .prepare("SELECT owner_uid, data_json FROM cf_app_catalog WHERE id = ?")
        .get(firstBody.app_id),
    ).toMatchObject({ owner_uid: "persona-owner" });

    const second = await jobs.fetch(
      new Request("https://jobs.test/v1/personas", {
        method: "POST",
        headers,
        body: form(data),
      }),
      state.env,
    );
    expect(second.status).toBe(200);
    expect((await second.json()) as { app_id: string }).toEqual(firstBody);
    expect(state.assets.objects.size).toBe(1);
    expect(state.ai.run).toHaveBeenCalledTimes(1);
  });

  it("requires auth and rejects malformed images without leaving R2 residue", async () => {
    const state = await environment();
    const unauthenticated = await jobs.fetch(
      new Request("https://jobs.test/v1/personas", {
        method: "POST",
        body: form({ name: "No Auth" }),
      }),
      state.env,
    );
    expect(unauthenticated.status).toBe(401);

    const signedHeaders = {
      "x-omi-auth-context": state.signed.encoded,
      "x-omi-internal-signature": state.signed.signature,
    };
    const invalid = await jobs.fetch(
      new Request("https://jobs.test/v1/personas", {
        method: "POST",
        headers: signedHeaders,
        body: form({ name: "Bad Image" }, Uint8Array.from([1, 2, 3])),
      }),
      state.env,
    );
    expect(invalid.status).toBe(422);
    expect(state.assets.objects.size).toBe(0);
  });
});
