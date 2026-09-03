import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { Hono } from "hono";
import type { JobsEnv } from "../workers/jobs/env";
import {
  registerTwitterOwnershipRoutes,
  verifyLatestTwitterTweet,
} from "../workers/jobs/twitter-ownership";
import { createSignedAuthContext } from "../workers/shared/auth-context";

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

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
        (this.database.prepare(sql).get(...(args as never[])) as T | undefined) ??
        null,
      all: async <T>() => ({
        results: this.database.prepare(sql).all(...(args as never[])) as T[],
      }),
      run: async () => {
        const result = this.database.prepare(sql).run(...(args as never[]));
        return { meta: { changes: Number(result.changes) } };
      },
    });
    return build();
  }

  close() {
    this.database.close();
  }
}

const databases: SqliteD1[] = [];
const SECRET = "twitter-ownership-test-secret";

function environment(provider: (request: RequestInfo | URL) => Response) {
  const database = new SqliteD1();
  databases.push(database);
  database.database
    .prepare(
      "INSERT INTO cf_account_cutover (uid, account_generation, updated_at) VALUES (?, 0, 1000)",
    )
    .run("uid-1");
  database.database
    .prepare(
      "INSERT INTO cf_account_cutover (uid, account_generation, updated_at) VALUES (?, 0, 1000)",
    )
    .run("uid-2");
  const fetchImpl = vi.fn(async (request: RequestInfo | URL) => provider(request));
  const env = {
    APP_DB: database,
    RAPID_API_HOST: "twitter-api.example.test",
    RAPID_API_KEY: "rapid-key",
    TWITTER_OWNERSHIP_STAGING_ENABLED: "true",
  } as unknown as JobsEnv;
  return { database, env, fetchImpl };
}

function appFor(
  uid: string,
  fetchImpl: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>,
) {
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerTwitterOwnershipRoutes(
    app,
    async () =>
      ({
        uid,
        authority: "better-auth",
        requestId: `twitter-ownership-${uid}`,
        version: 1,
        audience: "jobs",
        assertionId: `assertion-${uid}`,
        issuedAt: 1,
        expiresAt: 2,
        method: "GET",
        path: "/v2/cf/personas/twitter/verify-ownership",
      }) as never,
    { fetchImpl },
  );
  return app;
}

async function headers(uid: string) {
  const signed = await createSignedAuthContext(
    { uid, authority: "better-auth", requestId: `request-${uid}` },
    "jobs",
    "GET",
    "/v2/cf/personas/twitter/verify-ownership",
    SECRET,
  );
  return {
    "x-omi-auth-context": signed!.encoded,
    "x-omi-internal-signature": signed!.signature,
  };
}

function requestUrl() {
  return "https://jobs.test/v2/cf/personas/twitter/verify-ownership?username=alice&handle=%40omi";
}

function exactRequestUrl(personaId?: string, handle = "@omi") {
  const query = new URLSearchParams({ username: "alice", handle });
  if (personaId) query.set("persona_id", personaId);
  return `https://jobs.test/v1/personas/twitter/verify-ownership?${query}`;
}

function requestPath(request: RequestInfo | URL) {
  return new URL(request instanceof Request ? request.url : request).pathname;
}

describe("dormant Cloudflare Twitter ownership seam", () => {
  it("is feature-gated before auth or provider access", async () => {
    const database = new SqliteD1();
    databases.push(database);
    const fetchImpl = vi.fn(async () => Response.json({ timeline: [] }));
    const env = {
      APP_DB: database,
      TWITTER_OWNERSHIP_STAGING_ENABLED: "false",
    } as unknown as JobsEnv;
    const app = appFor("uid-1", fetchImpl);
    const response = await app.request(requestUrl(), {}, env);
    expect(response.status).toBe(404);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("records no-tweet evidence once and returns the same result on replay", async () => {
    const { database, env, fetchImpl } = environment(() =>
      Response.json({ timeline: [] }),
    );
    const app = appFor("uid-1", fetchImpl);
    const first = await app.request(
      requestUrl(),
      { headers: { ...(await headers("uid-1")) } },
      env,
    );
    expect(first.status).toBe(200);
    const firstBody = await first.json();
    expect(firstBody).toMatchObject({
      tweet: "",
      verified: false,
      status: "unverified",
      claim_status: null,
    });
    const replay = await app.request(
      requestUrl(),
      { headers: { ...(await headers("uid-1")) } },
      env,
    );
    expect(replay.status).toBe(200);
    expect(await replay.json()).toEqual(firstBody);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(
      database.database
        .prepare(
          "SELECT status, attempts, provider, provider_request_fingerprint, provider_response_fingerprint FROM cf_twitter_ownership_transactions",
        )
        .get(),
    ).toMatchObject({
      status: "unverified",
      attempts: 1,
      provider: "rapidapi-timeline",
    });
  });

  it("claims a verified handle for one uid and rejects a different uid", async () => {
    const { database, env, fetchImpl } = environment(() =>
      Response.json({
        timeline: [
          {
            tweet_id: "tweet-1",
            text: "Verifying my clone(alice)",
            created_at: "2026-08-31T00:00:00Z",
          },
        ],
      }),
    );
    const firstApp = appFor("uid-1", fetchImpl);
    const first = await firstApp.request(
      requestUrl(),
      { headers: { ...(await headers("uid-1")) } },
      env,
    );
    expect(first.status).toBe(200);
    expect(await first.json()).toMatchObject({
      verified: true,
      status: "verified",
      claim_status: "claimed",
    });

    const secondApp = appFor("uid-2", fetchImpl);
    const second = await secondApp.request(
      requestUrl(),
      { headers: { ...(await headers("uid-2")) } },
      env,
    );
    expect(second.status).toBe(409);
    await expect(second.json()).resolves.toMatchObject({
      error: "twitter_handle_already_claimed",
    });
    expect(
      database.database
        .prepare("SELECT uid, tweet_id FROM cf_twitter_ownership_claims WHERE handle = 'omi'")
        .get(),
    ).toEqual({ uid: "uid-1", tweet_id: "tweet-1" });
  });

  it("rejects late writes after an account deletion fence", async () => {
    const { database, env, fetchImpl } = environment(() =>
      Response.json({
        timeline: [
          { tweet_id: "tweet-1", text: "Verifying my clone(alice)" },
        ],
      }),
    );
    database.database
      .prepare(
        "INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES ('uid-1', 'delete-1', 'pending', 'quiescing', 1000, 1000, 1000)",
      )
      .run();
    const app = appFor("uid-1", fetchImpl);
    const response = await app.request(
      requestUrl(),
      { headers: { ...(await headers("uid-1")) } },
      env,
    );
    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toMatchObject({
      error: "account_deletion_fenced",
    });
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(() =>
      database.database
        .prepare(
          "INSERT INTO cf_twitter_ownership_claims (handle, uid, account_generation, transaction_id, username, tweet_id, claimed_at, updated_at) VALUES ('omi', 'uid-1', 0, 'tx', 'alice', 'tweet', 1000, 1000)",
        )
        .run(),
    ).toThrow("account deletion fence");
  });

  it("bounds the timeline response and preserves a provider fingerprint", async () => {
    const { env } = environment(() => Response.json({ timeline: [] }));
    const result = await verifyLatestTwitterTweet(
      env,
      "alice",
      "omi",
      { fetchImpl: async () => Response.json({ timeline: [] }) },
    );
    expect(result).toMatchObject({
      tweet: "",
      verified: false,
      tweetId: null,
      providerResponseFingerprint: expect.stringMatching(/^[a-f0-9]{64}$/),
    });
  });

  it("keeps the exact owner fail-closed when the explicit gate or provider secret is absent", async () => {
    const { database, fetchImpl } = environment(() =>
      Response.json({ timeline: [] }),
    );
    const env = {
      APP_DB: database,
      TWITTER_OWNERSHIP_EXACT_STAGING_ENABLED: "false",
    } as unknown as JobsEnv;
    const app = appFor("uid-1", fetchImpl);
    const gated = await app.request(
      exactRequestUrl(),
      { headers: { ...(await headers("uid-1")) } },
      env,
    );
    expect(gated.status).toBe(503);
    expect(fetchImpl).not.toHaveBeenCalled();

    const missingSecretEnv = {
      ...env,
      TWITTER_OWNERSHIP_EXACT_STAGING_ENABLED: "true",
      RAPID_API_HOST: undefined,
      RAPID_API_KEY: undefined,
    } as unknown as JobsEnv;
    const unavailable = await app.request(
      exactRequestUrl(),
      { headers: { ...(await headers("uid-1")) } },
      missingSecretEnv,
    );
    expect(unavailable.status).toBe(503);
    await expect(unavailable.json()).resolves.toMatchObject({
      error: "provider_not_configured",
    });
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_app_catalog")
        .get(),
    ).toEqual({ count: 0 });
  });

  it("verifies the exact owner and atomically projects a D1 Persona on replay", async () => {
    const { database, env, fetchImpl } = environment((request) => {
      const pathname = requestPath(request);
      if (pathname.endsWith("/screenname.php")) {
        return Response.json({
          name: "Omi",
          profile: "omi",
          rest_id: "twitter-1",
          avatar: "https://cdn.example/omi.jpg",
          desc: "A profile",
          friends: 10,
          sub_count: 20,
          id: "twitter-1",
          status: "ok",
        });
      }
      return Response.json({
        timeline: [{ tweet_id: "tweet-1", text: "Verifying my clone(alice)" }],
      });
    });
    const exactEnv = {
      ...env,
      TWITTER_OWNERSHIP_EXACT_STAGING_ENABLED: "true",
    } as unknown as JobsEnv;
    const app = appFor("uid-1", fetchImpl);
    const first = await app.request(
      exactRequestUrl(),
      { headers: { ...(await headers("uid-1")) } },
      exactEnv,
    );
    expect(first.status).toBe(200);
    const firstBody = (await first.json()) as { persona_id?: string; tweet?: string; verified?: boolean };
    expect(firstBody).toMatchObject({ tweet: "Verifying my clone(alice)", verified: true });
    expect(firstBody.persona_id).toMatch(/^twitter-persona-/);
    const replay = await app.request(
      exactRequestUrl(),
      { headers: { ...(await headers("uid-1")) } },
      exactEnv,
    );
    expect(await replay.json()).toEqual(firstBody);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    const row = database.database
      .prepare("SELECT owner_uid, data_json FROM cf_app_catalog")
      .get() as { owner_uid: string; data_json: string };
    expect(row.owner_uid).toBe("uid-1");
    expect(JSON.parse(row.data_json)).toMatchObject({
      username: "alice",
      connected_accounts: ["twitter"],
      twitter: { username: "omi" },
    });
  });

  it("attaches only to the owning Persona and rejects a cross-uid write", async () => {
    const { database, env, fetchImpl } = environment((request) => {
      if (requestPath(request).endsWith("/screenname.php")) {
        return Response.json({
          name: "Omi",
          profile: "omi",
          avatar: "https://cdn.example/omi.jpg",
          desc: "A profile",
          status: "ok",
        });
      }
      return Response.json({
        timeline: [{ tweet_id: "tweet-1", text: "Verifying my clone(alice)" }],
      });
    });
    database.database
      .prepare(
        `INSERT INTO cf_app_catalog
          (id, approved, status, disabled, is_popular, installs, rating_count,
           data_json, updated_at, owner_uid)
         VALUES ('persona-1', 1, 'approved', 0, 0, 0, 0, ?, 1000, 'uid-1')`,
      )
      .run(
        JSON.stringify({
          id: "persona-1",
          username: "alice",
          capabilities: ["persona"],
          connected_accounts: ["omi"],
        }),
      );
    const exactEnv = {
      ...env,
      TWITTER_OWNERSHIP_EXACT_STAGING_ENABLED: "true",
    } as unknown as JobsEnv;
    const first = await appFor("uid-1", fetchImpl).request(
      exactRequestUrl("persona-1"),
      { headers: { ...(await headers("uid-1")) } },
      exactEnv,
    );
    expect(first.status).toBe(200);
    expect(await first.json()).toMatchObject({ persona_id: "persona-1", verified: true });
    const crossUid = await appFor("uid-2", fetchImpl).request(
      exactRequestUrl("persona-1", "@newhandle"),
      { headers: { ...(await headers("uid-2")) } },
      exactEnv,
    );
    expect(crossUid.status).toBe(403);
    await expect(crossUid.json()).resolves.toMatchObject({ error: "persona_owner_mismatch" });
    expect(
      database.database.prepare("SELECT owner_uid FROM cf_app_catalog WHERE id = 'persona-1'").get(),
    ).toEqual({ owner_uid: "uid-1" });
  });
});
