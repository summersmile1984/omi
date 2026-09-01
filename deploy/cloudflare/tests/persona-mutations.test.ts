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

function updateForm(data: Record<string, unknown>, image?: Uint8Array) {
  const body = new FormData();
  body.set("persona_data", JSON.stringify(data));
  if (image) {
    const copy = new Uint8Array(image.byteLength);
    copy.set(image);
    body.set(
      "file",
      new File([copy.buffer as ArrayBuffer], "persona.png", {
        type: "image/png",
      }),
    );
  }
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

  it("updates only an owner D1 persona, preserves an omitted logo, and rotates a supplied logo", async () => {
    const state = await environment();
    const headers = {
      "x-omi-auth-context": state.signed.encoded,
      "x-omi-internal-signature": state.signed.signature,
    };
    const created = await jobs.fetch(
      new Request("https://jobs.test/v1/personas", {
        method: "POST",
        headers,
        body: form({
          name: "Before update",
          username: "before-update",
          private: true,
        }),
      }),
      state.env,
    );
    const createdBody = (await created.json()) as { app_id: string };
    const originalKeys = [...state.assets.objects.keys()];
    const patchSigned = await createSignedAuthContext(
      {
        uid: "persona-owner",
        authority: "better-auth",
        requestId: "persona-update",
      },
      "jobs",
      "PATCH",
      `/v1/personas/${createdBody.app_id}`,
      "persona-mutation-secret",
    );
    if (!patchSigned) throw new Error("missing patch signed context");
    const patchHeaders = {
      "x-omi-auth-context": patchSigned.encoded,
      "x-omi-internal-signature": patchSigned.signature,
    };

    const withoutImage = await jobs.fetch(
      new Request(`https://jobs.test/v1/personas/${createdBody.app_id}`, {
        method: "PATCH",
        headers: patchHeaders,
        body: updateForm({
          name: "After update",
          username: "after-update",
          connected_accounts: ["omi"],
          private: false,
        }),
      }),
      state.env,
    );
    expect(withoutImage.status).toBe(200);
    await expect(withoutImage.json()).resolves.toMatchObject({
      status: "ok",
      app_id: createdBody.app_id,
      username: "after-update",
    });
    expect(state.assets.objects.size).toBe(1);
    expect([...state.assets.objects.keys()]).toEqual(originalKeys);

    const replacement = await jobs.fetch(
      new Request(`https://jobs.test/v1/personas/${createdBody.app_id}`, {
        method: "PATCH",
        headers: patchHeaders,
        body: updateForm({ name: "Rotated update", username: "rotated" }, PNG),
      }),
      state.env,
    );
    expect(replacement.status).toBe(200);
    expect(state.assets.objects.size).toBe(1);
    expect([...state.assets.objects.keys()]).not.toEqual(originalKeys);
    const row = state.database.database
      .prepare("SELECT owner_uid, data_json FROM cf_app_catalog WHERE id = ?")
      .get(createdBody.app_id) as { owner_uid: string; data_json: string };
    expect(row.owner_uid).toBe("persona-owner");
    expect(JSON.parse(row.data_json)).toMatchObject({
      id: createdBody.app_id,
      uid: "persona-owner",
      name: "Rotated update",
      username: "rotated",
      capabilities: ["persona"],
    });
    expect(state.ai.run).toHaveBeenCalledTimes(3);
  });

  it("rejects a non-owner and lets the D1 deletion fence block an update", async () => {
    const state = await environment();
    const headers = {
      "x-omi-auth-context": state.signed.encoded,
      "x-omi-internal-signature": state.signed.signature,
    };
    const created = await jobs.fetch(
      new Request("https://jobs.test/v1/personas", {
        method: "POST",
        headers,
        body: form({ name: "Fenced persona", username: "fenced" }),
      }),
      state.env,
    );
    const { app_id: personaId } = (await created.json()) as { app_id: string };
    const patchSigned = await createSignedAuthContext(
      {
        uid: "persona-owner",
        authority: "better-auth",
        requestId: "persona-fence-update",
      },
      "jobs",
      "PATCH",
      `/v1/personas/${personaId}`,
      "persona-mutation-secret",
    );
    if (!patchSigned) throw new Error("missing fence signed context");
    const other = await createSignedAuthContext(
      {
        uid: "other-owner",
        authority: "better-auth",
        requestId: "persona-other",
      },
      "jobs",
      "PATCH",
      `/v1/personas/${personaId}`,
      "persona-mutation-secret",
    );
    if (!other) throw new Error("missing other signed context");
    const forbidden = await jobs.fetch(
      new Request(`https://jobs.test/v1/personas/${personaId}`, {
        method: "PATCH",
        headers: {
          "x-omi-auth-context": other.encoded,
          "x-omi-internal-signature": other.signature,
        },
        body: updateForm({ name: "attacker" }),
      }),
      state.env,
    );
    expect(forbidden.status).toBe(403);

    state.database.database
      .prepare(
        `INSERT INTO cf_account_deletion_intents
           (uid, job_id, status, phase, next_attempt_at, created_at, updated_at)
         VALUES (?, ?, 'pending', 'quiescing', 1, 1, 1)`,
      )
      .run("persona-owner", "delete-persona-owner");
    const fenced = await jobs.fetch(
      new Request(`https://jobs.test/v1/personas/${personaId}`, {
        method: "PATCH",
        headers: {
          "x-omi-auth-context": patchSigned.encoded,
          "x-omi-internal-signature": patchSigned.signature,
        },
        body: updateForm({ name: "blocked" }),
      }),
      state.env,
    );
    expect(fenced.status).toBe(503);
    const row = state.database.database
      .prepare("SELECT data_json FROM cf_app_catalog WHERE id = ?")
      .get(personaId) as { data_json: string };
    expect(JSON.parse(row.data_json).name).toBe("Fenced persona");
  });

  it("enforces username uniqueness at the catalog authority, not just the pre-check", async () => {
    const state = await environment();
    const headers = {
      "x-omi-auth-context": state.signed.encoded,
      "x-omi-internal-signature": state.signed.signature,
    };
    const created = await jobs.fetch(
      new Request("https://jobs.test/v1/personas", {
        method: "POST",
        headers,
        body: form({ name: "Unique Persona", username: "uniquename" }),
      }),
      state.env,
    );
    expect(created.status).toBe(200);

    // The route-level pre-check refuses a visible duplicate...
    const viaRoute = await jobs.fetch(
      new Request("https://jobs.test/v1/personas", {
        method: "POST",
        headers,
        body: form({ name: "Copycat", username: "uniquename" }),
      }),
      state.env,
    );
    expect(viaRoute.status).toBe(409);

    // ...and the partial unique index refuses the write itself, which is what
    // the 409 mapping in the create/update paths relies on when two creates
    // race past the pre-check simultaneously.
    expect(() =>
      state.database.database
        .prepare(
          "INSERT INTO cf_app_catalog (id, approved, status, disabled, is_popular, installs, rating_avg, rating_count, data_json, updated_at, owner_uid) VALUES ('cf_persona_racer', 0, 'under-review', 0, 0, 0, NULL, 0, ?, 1, 'someone-else')",
        )
        .run(
          JSON.stringify({ id: "cf_persona_racer", username: "uniquename" }),
        ),
    ).toThrow(/UNIQUE constraint failed/);
    expect(() =>
      state.database.database
        .prepare(
          "INSERT INTO cf_app_catalog (id, approved, status, disabled, is_popular, installs, rating_avg, rating_count, data_json, updated_at, owner_uid) VALUES ('cf_app_plain', 1, 'approved', 0, 0, 0, NULL, 0, ?, 1, NULL)",
        )
        .run(JSON.stringify({ id: "cf_app_plain", name: "No username app" })),
    ).not.toThrow();
  });
});
