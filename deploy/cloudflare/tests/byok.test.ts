import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import edge from "../workers/edge/index";
import {
  activateByok,
  deactivateByok,
  parseByokActivationPayload,
  validateByokHeaders,
} from "../workers/edge/byok";
import type { EdgeEnv } from "../workers/edge/env";
import type { AuthContext } from "../workers/shared/auth-context";
import { verifyRequestAuthContext } from "../workers/shared/auth-context";

function sqliteValue(value: unknown) {
  return value as never;
}

class SqliteD1 {
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
    const build = (args: unknown[] = []) => ({
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...args.map(sqliteValue)) as
          T | undefined) ?? null,
      all: async <T>() => ({
        success: true as const,
        results: this.database
          .prepare(sql)
          .all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0 },
      }),
      run: async () => {
        const result = this.database.prepare(sql).run(...args.map(sqliteValue));
        return {
          success: true as const,
          results: [],
          meta: { changes: Number(result.changes) },
        };
      },
    });
    return build();
  }

  close() {
    this.database.close();
  }
}

const databases: SqliteD1[] = [];
const pepper = "byok-test-pepper-which-is-at-least-32-bytes";
const identity: AuthContext = {
  uid: "byok-user",
  authority: "better-auth",
  requestId: "byok-request",
};
const keys = {
  openai: "sk-openai-user-key",
  anthropic: "sk-ant-anthropic-user-key",
  gemini: "gemini-user-key",
  deepgram: "deepgram-user-key",
};
const fingerprints = Object.fromEntries(
  Object.entries(keys).map(([provider, value]) => [
    provider,
    createHash("sha256").update(value).digest("hex"),
  ]),
) as Record<keyof typeof keys, string>;

function environment(options: { configured?: boolean } = {}) {
  const database = new SqliteD1();
  databases.push(database);
  return {
    database,
    env: {
      APP_DB: database as unknown as D1Database,
      ...(options.configured === false
        ? {}
        : { BYOK_FINGERPRINT_PEPPER: pepper }),
    } as unknown as EdgeEnv,
  };
}

function keyHeaders(values = keys) {
  return new Headers(
    Object.fromEntries(
      Object.entries(values).map(([provider, value]) => [
        `x-byok-${provider}`,
        value,
      ]),
    ),
  );
}

const fetcher = (handler: (request: Request) => Promise<Response> | Response) =>
  ({ fetch: handler }) as Fetcher;

function edgeEnvironment(database: SqliteD1) {
  const forwarded: Request[] = [];
  const secret = "byok-internal-assertion-secret";
  const env = {
    APP_DB: database as unknown as D1Database,
    BYOK_FINGERPRINT_PEPPER: pepper,
    INTERNAL_ASSERTION_SECRET: secret,
    AUTH: fetcher(() =>
      Response.json({ uid: identity.uid, authority: "better-auth" }),
    ),
    API_CORE: fetcher((request) => {
      if (new URL(request.url).pathname === "/v1/account/cutover/control") {
        return Response.json({
          state: "new",
          client_action: "none",
          product_traffic_allowed: true,
          migration: { destination_backend_bound: true },
        });
      }
      return Response.json({ status: "unexpected" });
    }),
    API_AI: fetcher((request) => {
      forwarded.push(request);
      return Response.json({ status: "ok" });
    }),
    RATE_LIMITS: {
      idFromName: (name: string) => name,
      get: () => ({
        fetch: () =>
          Response.json({
            allowed: true,
            limit: 120,
            remaining: 119,
            retryAfter: 0,
            resetAt: Date.now() + 3_600_000,
          }),
      }),
    } as unknown as DurableObjectNamespace,
  } as unknown as EdgeEnv;
  return { env, forwarded, secret };
}

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
});

describe("Cloudflare BYOK authority", () => {
  it("validates the exact four-provider enrollment contract", async () => {
    const missing = parseByokActivationPayload({
      fingerprints: { openai: "a".repeat(64) },
    });
    expect(missing).toBeInstanceOf(Response);
    if (missing instanceof Response) {
      expect(missing.status).toBe(400);
      await expect(missing.json()).resolves.toEqual({
        detail:
          "Missing fingerprints for providers: ['anthropic', 'gemini', 'deepgram']",
      });
    }

    const unknown = parseByokActivationPayload({
      fingerprints: { ...fingerprints, mistral: "a".repeat(64) },
    });
    expect(unknown).toBeInstanceOf(Response);
    if (unknown instanceof Response) {
      await expect(unknown.json()).resolves.toEqual({
        detail: "Unknown provider: mistral",
      });
    }

    const uppercase = parseByokActivationPayload({
      fingerprints: { ...fingerprints, openai: "A".repeat(64) },
    });
    expect(uppercase).toBeInstanceOf(Response);
    if (uppercase instanceof Response) expect(uppercase.status).toBe(400);
    expect(parseByokActivationPayload({ fingerprints })).toEqual({
      fingerprints,
    });
  });

  it("stores only HMAC fingerprints, validates raw keys, and deactivates", async () => {
    const { database, env } = environment();
    const activated = await activateByok(
      env,
      identity.uid,
      fingerprints,
      1_000,
    );
    expect(activated.status).toBe(200);
    await expect(activated.json()).resolves.toEqual({ active: true });

    const stored = database.database
      .prepare(
        "SELECT active, openai_fingerprint, anthropic_fingerprint, gemini_fingerprint, " +
          "deepgram_fingerprint, last_seen_at FROM cf_user_byok_enrollments WHERE uid = ?",
      )
      .get(identity.uid) as Record<string, string | number>;
    expect(stored.active).toBe(1);
    expect(stored.last_seen_at).toBe(1_000);
    expect(stored.openai_fingerprint).toMatch(/^[a-f0-9]{64}$/);
    expect(stored.openai_fingerprint).not.toBe(fingerprints.openai);
    expect(JSON.stringify(stored)).not.toContain(keys.openai);

    const headers = keyHeaders();
    const valid = await validateByokHeaders(env, identity, headers, {
      now: 1_001,
    });
    expect(valid).toEqual({ context: { ...identity, byokActive: true } });
    expect(headers.get("x-byok-openai")).toBe(keys.openai);

    const deactivated = await deactivateByok(env, identity.uid, 1_002);
    await expect(deactivated.json()).resolves.toEqual({ active: false });
    expect(
      database.database
        .prepare(
          "SELECT active, openai_fingerprint, anthropic_fingerprint, gemini_fingerprint, " +
            "deepgram_fingerprint FROM cf_user_byok_enrollments WHERE uid = ?",
        )
        .get(identity.uid),
    ).toEqual({
      active: 0,
      openai_fingerprint: null,
      anthropic_fingerprint: null,
      gemini_fingerprint: null,
      deepgram_fingerprint: null,
    });
  });

  it("rejects enrolled key drift but strips inactive, expired, and recovery headers", async () => {
    const { database, env } = environment();
    await activateByok(env, identity.uid, fingerprints, 1_000);

    const mismatchedHeaders = keyHeaders({ ...keys, openai: "wrong-key" });
    const mismatch = await validateByokHeaders(
      env,
      identity,
      mismatchedHeaders,
      { now: 1_001 },
    );
    expect(mismatch.response?.status).toBe(403);
    await expect(mismatch.response?.json()).resolves.toEqual({
      detail: "BYOK key fingerprint mismatch for provider: openai",
    });
    expect(mismatchedHeaders.get("x-byok-openai")).toBeNull();

    const recoveryHeaders = keyHeaders({ ...keys, openai: "wrong-key" });
    await expect(
      validateByokHeaders(env, identity, recoveryHeaders, {
        now: 1_001,
        recoverInvalid: true,
      }),
    ).resolves.toEqual({ context: identity });
    expect(recoveryHeaders.get("x-byok-anthropic")).toBeNull();

    database.database
      .prepare(
        "UPDATE cf_user_byok_enrollments SET last_seen_at = 1 WHERE uid = ?",
      )
      .run(identity.uid);
    const expiredHeaders = keyHeaders();
    await expect(
      validateByokHeaders(env, identity, expiredHeaders, {
        now: 7 * 24 * 60 * 60 + 2,
      }),
    ).resolves.toEqual({ context: identity });
    expect(expiredHeaders.get("x-byok-gemini")).toBeNull();

    await deactivateByok(env, identity.uid, 2_000);
    const inactiveHeaders = keyHeaders();
    await expect(
      validateByokHeaders(env, identity, inactiveHeaders, { now: 2_001 }),
    ).resolves.toEqual({ context: identity });
    expect(inactiveHeaders.get("x-byok-deepgram")).toBeNull();
  });

  it("fails closed when enrollment state or the HMAC pepper is unavailable", async () => {
    const noDatabaseHeaders = keyHeaders();
    const noDatabase = await validateByokHeaders(
      {} as EdgeEnv,
      identity,
      noDatabaseHeaders,
    );
    expect(noDatabase.response?.status).toBe(503);
    expect(noDatabaseHeaders.get("x-byok-openai")).toBeNull();

    const { database, env } = environment({ configured: false });
    database.database
      .prepare(
        "INSERT INTO cf_user_byok_enrollments " +
          "(uid, active, openai_fingerprint, anthropic_fingerprint, gemini_fingerprint, " +
          "deepgram_fingerprint, last_seen_at, updated_at) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
      )
      .run(
        identity.uid,
        fingerprints.openai,
        fingerprints.anthropic,
        fingerprints.gemini,
        fingerprints.deepgram,
        1_000,
        1_000,
      );
    const noPepperHeaders = keyHeaders();
    const noPepper = await validateByokHeaders(env, identity, noPepperHeaders, {
      now: 1_001,
    });
    expect(noPepper.response?.status).toBe(503);
    expect(noPepperHeaders.get("x-byok-openai")).toBeNull();
  });

  it("owns enrollment at the Edge and forwards only validated BYOK authority", async () => {
    const { database } = environment();
    const { env, forwarded, secret } = edgeEnvironment(database);
    const authorization = { authorization: "Bearer opaque-session" };
    const activate = await edge.fetch(
      new Request("https://edge.test/v1/users/me/byok-active", {
        method: "POST",
        headers: { ...authorization, "content-type": "application/json" },
        body: JSON.stringify({ fingerprints }),
      }),
      env,
    );
    expect(activate.status).toBe(200);
    expect(activate.headers.get("cache-control")).toBe("no-store");
    await expect(activate.json()).resolves.toEqual({ active: true });

    const valid = await edge.fetch(
      new Request("https://edge.test/v2/messages", {
        method: "POST",
        headers: { ...authorization, ...Object.fromEntries(keyHeaders()) },
        body: JSON.stringify({ text: "BYOK request" }),
      }),
      env,
    );
    expect(valid.status).toBe(200);
    expect(forwarded).toHaveLength(1);
    expect(forwarded[0].headers.get("x-byok-openai")).toBe(keys.openai);
    await expect(
      verifyRequestAuthContext(forwarded[0], "api-ai", secret),
    ).resolves.toMatchObject({ uid: identity.uid, byokActive: true });

    const mismatch = await edge.fetch(
      new Request("https://edge.test/v2/messages", {
        method: "POST",
        headers: {
          ...authorization,
          ...Object.fromEntries(keyHeaders({ ...keys, openai: "wrong-key" })),
        },
        body: JSON.stringify({ text: "forged request" }),
      }),
      env,
    );
    expect(mismatch.status).toBe(403);
    await expect(mismatch.json()).resolves.toEqual({
      detail: "BYOK key fingerprint mismatch for provider: openai",
    });
    expect(forwarded).toHaveLength(1);

    const deactivate = await edge.fetch(
      new Request("https://edge.test/v1/users/me/byok-active", {
        method: "DELETE",
        headers: authorization,
      }),
      env,
    );
    await expect(deactivate.json()).resolves.toEqual({ active: false });
    const inactive = await edge.fetch(
      new Request("https://edge.test/v2/messages", {
        method: "POST",
        headers: { ...authorization, ...Object.fromEntries(keyHeaders()) },
        body: JSON.stringify({ text: "managed request" }),
      }),
      env,
    );
    expect(inactive.status).toBe(200);
    expect(forwarded).toHaveLength(2);
    expect(forwarded[1].headers.get("x-byok-openai")).toBeNull();
    await expect(
      verifyRequestAuthContext(forwarded[1], "api-ai", secret),
    ).resolves.not.toHaveProperty("byokActive");
  });
});
