import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { describe, expect, it, vi } from "vitest";
import type { AuthEnv } from "../workers/auth/env";
import {
  pkceChallengeForVerifier,
} from "../workers/auth/legacy-compatibility";
import {
  registerNativeAuthCompatibilityRoutes,
  type NativeAuthCompatibilityDependencies,
} from "../workers/auth/native-auth-compatibility";

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    this.database.exec("PRAGMA foreign_keys = ON");
    const migrations = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/auth",
    );
    for (const filename of readdirSync(migrations)
      .filter((value) => value.endsWith(".sql"))
      .sort()) {
      this.database.exec(readFileSync(path.join(migrations, filename), "utf8"));
    }
  }

  prepare(sql: string) {
    const build = (args: unknown[] = []) => ({
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...(args as never[])) as T | undefined) ??
        null,
      run: async () => {
        const result = this.database.prepare(sql).run(...(args as never[]));
        return { success: true as const, meta: { changes: Number(result.changes) } };
      },
    });
    return build();
  }

  close() {
    this.database.close();
  }
}

const BASE_URL = "https://auth.example.test";
const ENCRYPTION_SECRET = "native-auth-transaction-secret-32-bytes";
const VERIFIER = "v".repeat(43);
const REDIRECT_URI = "omi://auth/callback";

function testEnv(database: SqliteD1, overrides: Partial<AuthEnv> = {}): AuthEnv {
  return {
    AUTH_DB: database as unknown as D1Database,
    BETTER_AUTH_SECRET: "better-auth-secret",
    LEGACY_AUTH_COMPAT_STAGING_ENABLED: "true",
    LEGACY_AUTH_TRANSACTION_ENCRYPTION_SECRET: ENCRYPTION_SECRET,
    NATIVE_AUTH_PUBLIC_BASE_URL: BASE_URL,
    GOOGLE_CLIENT_ID: "google-client-id",
    GOOGLE_CLIENT_SECRET: "google-client-secret",
    ...overrides,
  };
}

function harness(
  fetchImpl: NativeAuthCompatibilityDependencies["fetchImpl"],
  overrides: Partial<AuthEnv> = {},
) {
  const database = new SqliteD1();
  let now = 1_700_000_000;
  const app = new Hono<{ Bindings: AuthEnv }>();
  registerNativeAuthCompatibilityRoutes(app, {
    fetchImpl,
    now: () => now,
  });
  return {
    app,
    database,
    env: testEnv(database, overrides),
    advance(seconds: number) {
      now += seconds;
    },
  };
}

async function authorize(
  fixture: ReturnType<typeof harness>,
  provider = "google",
  state = "caller-state-123456",
) {
  const challenge = await pkceChallengeForVerifier(VERIFIER);
  const url = new URL(`${BASE_URL}/v2/cf/auth/authorize`);
  url.search = new URLSearchParams({
    provider,
    redirect_uri: REDIRECT_URI,
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  }).toString();
  const response = await fixture.app.request(url, {}, fixture.env);
  expect(response.status).toBe(302);
  const location = response.headers.get("location");
  expect(location).toBeTruthy();
  return new URL(location!);
}

async function callbackAndCode(
  fixture: ReturnType<typeof harness>,
  providerState: URL,
  providerCode = "provider-code-1234567890",
) {
  const callback = new URL(`${BASE_URL}/v2/cf/auth/callback/google`);
  callback.search = new URLSearchParams({
    code: providerCode,
    state: providerState.searchParams.get("state") || "",
  }).toString();
  const response = await fixture.app.request(callback, {}, fixture.env);
  expect(response.status).toBe(200);
  const html = await response.text();
  expect(html).not.toContain("<script");
  const href = html.match(/href="([^"]+)"/)?.[1];
  expect(href).toBeTruthy();
  const redirect = new URL(href!.replaceAll("&amp;", "&"));
  return redirect.searchParams.get("code")!;
}

function tokenRequest(
  fixture: ReturnType<typeof harness>,
  code: string,
  overrides: Record<string, string> = {},
) {
  const form = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: REDIRECT_URI,
    code_verifier: VERIFIER,
    ...overrides,
  });
  return fixture.app.request(
    `${BASE_URL}/v2/cf/auth/token`,
    {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: form.toString(),
    },
    fixture.env,
  );
}

describe("namespaced native auth compatibility seam", () => {
  it("closes authorize → Google callback → single-use provider token", async () => {
    const providerFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("https://oauth2.googleapis.com/token");
      expect(init?.method).toBe("POST");
      const body = new URLSearchParams(String(init?.body));
      expect(body.get("client_id")).toBe("google-client-id");
      expect(body.get("client_secret")).toBe("google-client-secret");
      return Response.json({
        id_token: "google-id-token-secret",
        access_token: "google-access-token-secret",
        expires_in: 3_600,
      });
    });
    const fixture = harness(providerFetch);
    try {
      const providerState = await authorize(fixture);
      expect(providerState.hostname).toBe("accounts.google.com");
      // Omi's PKCE challenge is bound to the D1-issued Omi code. The provider
      // exchange follows the legacy wire and therefore does not advertise a
      // provider challenge that cannot be redeemed later.
      expect(providerState.searchParams.get("code_challenge_method")).toBeNull();
      const code = await callbackAndCode(fixture, providerState);
      const response = await tokenRequest(fixture, code);
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({
        provider: "google",
        id_token: "google-id-token-secret",
        access_token: "google-access-token-secret",
        provider_id: "google.com",
        token_type: "Bearer",
        expires_in: 3_600,
      });
      expect(providerFetch).toHaveBeenCalledTimes(1);

      const replay = await tokenRequest(fixture, code);
      expect(replay.status).toBe(400);
      expect(await replay.json()).toEqual({ error: "invalid_or_expired_code" });
      const rows = fixture.database.database
        .prepare(
          "SELECT encryptedPayload, metadataEnvelopeEnc FROM cf_legacy_auth_transactions",
        )
        .all() as Array<Record<string, unknown>>;
      expect(JSON.stringify(rows)).not.toContain("google-id-token-secret");
      expect(JSON.stringify(rows)).not.toContain("google-access-token-secret");
      expect(rows.every((row) => String(row.encryptedPayload || row.metadataEnvelopeEnc).startsWith("v1."))).toBe(true);
    } finally {
      fixture.database.close();
    }
  });

  it("rejects redirect and PKCE mismatches without consuming the code", async () => {
    const providerFetch = vi.fn(async () =>
      Response.json({ id_token: "id-token", access_token: "access-token" }),
    );
    const fixture = harness(providerFetch);
    try {
      const providerState = await authorize(fixture);
      const code = await callbackAndCode(fixture, providerState);
      expect(
        (await tokenRequest(fixture, code, { redirect_uri: "omi://other/callback" })).status,
      ).toBe(400);
      expect(
        (await tokenRequest(fixture, code, { code_verifier: "wrong-verifier-123456789012345678901234567890" })).status,
      ).toBe(400);
      expect((await tokenRequest(fixture, code)).status).toBe(200);
    } finally {
      fixture.database.close();
    }
  });

  it("accepts Apple's form-post callback and keeps the client secret out of D1", async () => {
    const providerFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("https://appleid.apple.com/auth/token");
      const body = new URLSearchParams(String(init?.body));
      expect(body.get("client_secret")).toBe("apple-client-secret");
      return Response.json({ id_token: "apple-id-token-secret" });
    });
    const fixture = harness(providerFetch, {
      APPLE_CLIENT_ID: "apple-client-id",
      APPLE_CLIENT_SECRET: "apple-client-secret",
    });
    try {
      const providerState = await authorize(fixture, "apple", "apple-state-123456");
      const callback = new URL(`${BASE_URL}/v2/cf/auth/callback/apple`);
      const form = new URLSearchParams({
        code: "apple-code-1234567890",
        state: providerState.searchParams.get("state") || "",
        user: JSON.stringify({ name: { firstName: "Ada", lastName: "Lovelace" } }),
      });
      const callbackResponse = await fixture.app.request(
        callback,
        {
          method: "POST",
          headers: { "content-type": "application/x-www-form-urlencoded" },
          body: form.toString(),
        },
        fixture.env,
      );
      expect(callbackResponse.status).toBe(200);
      const html = await callbackResponse.text();
      const href = html.match(/href="([^"]+)"/)?.[1];
      expect(href).toBeTruthy();
      const code = new URL(href!.replaceAll("&amp;", "&")).searchParams.get("code")!;
      const response = await tokenRequest(fixture, code);
      expect(response.status).toBe(200);
      expect(await response.json()).toMatchObject({
        provider: "apple",
        id_token: "apple-id-token-secret",
        access_token: null,
        provider_id: "apple.com",
      });
      const rawRows = fixture.database.database
        .prepare("SELECT encryptedPayload FROM cf_legacy_auth_transactions")
        .all() as Array<Record<string, unknown>>;
      expect(JSON.stringify(rawRows)).not.toContain("apple-id-token-secret");
    } finally {
      fixture.database.close();
    }
  });

  it("expires a pending code and fails closed when custom-token identity mapping is absent", async () => {
    const providerFetch = vi.fn(async () =>
      Response.json({ id_token: "id-token", access_token: "access-token" }),
    );
    const fixture = harness(providerFetch);
    try {
      const providerState = await authorize(fixture);
      const code = await callbackAndCode(fixture, providerState);
      expect((await tokenRequest(fixture, code, { use_custom_token: "true" })).status).toBe(503);
      fixture.advance(301);
      expect((await tokenRequest(fixture, code)).status).toBe(400);
    } finally {
      fixture.database.close();
    }
  });

  it("consumes denied/provider-failed callbacks and blocks replay", async () => {
    const providerFetch = vi.fn(async () =>
      new Response(JSON.stringify({ error: "denied" }), { status: 500 }),
    );
    const fixture = harness(providerFetch);
    try {
      const providerState = await authorize(fixture);
      const callback = new URL(`${BASE_URL}/v2/cf/auth/callback/google`);
      callback.search = new URLSearchParams({
        code: "provider-code-1234567890",
        state: providerState.searchParams.get("state") || "",
      }).toString();
      expect((await fixture.app.request(callback, {}, fixture.env)).status).toBe(503);
      expect((await fixture.app.request(callback, {}, fixture.env)).status).toBe(400);
      expect(providerFetch).toHaveBeenCalledTimes(1);
    } finally {
      fixture.database.close();
    }
  });

  it("is disabled outside the explicit staging gate and when secrets are missing", async () => {
    const disabled = harness(vi.fn(), { LEGACY_AUTH_COMPAT_STAGING_ENABLED: "false" });
    try {
      const challenge = await pkceChallengeForVerifier(VERIFIER);
      const url = new URL(`${BASE_URL}/v2/cf/auth/authorize`);
      url.search = new URLSearchParams({
        provider: "google",
        redirect_uri: REDIRECT_URI,
        state: "caller-state-123456",
        code_challenge: challenge,
        code_challenge_method: "S256",
      }).toString();
      expect((await disabled.app.request(url, {}, disabled.env)).status).toBe(404);
      const missing = harness(vi.fn(), {
        LEGACY_AUTH_TRANSACTION_ENCRYPTION_SECRET: undefined,
        GOOGLE_CLIENT_SECRET: undefined,
      });
      try {
        expect((await missing.app.request(url, {}, missing.env)).status).toBe(503);
      } finally {
        missing.database.close();
      }
    } finally {
      disabled.database.close();
    }
  });
});
