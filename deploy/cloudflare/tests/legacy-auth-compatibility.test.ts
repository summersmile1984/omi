import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  consumeExternalOAuthTransaction,
  consumeLegacyAuthTransaction,
  createExternalOAuthTransaction,
  createLegacyAuthTransaction,
  evaluateExternalAppAdmission,
  evaluateFirebaseIdentityAdmission,
  pkceChallengeForVerifier,
} from "../workers/auth/legacy-compatibility";

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

function identityInput(overrides: Record<string, unknown> = {}) {
  return {
    ledger: {
      id: "firebase",
      status: "completed" as const,
      sourceSha256: "source-checksum",
      configFingerprint: "config-fingerprint",
      canonicalSha256: "canonical-checksum",
    },
    projection: {
      firebaseUid: "firebase-user",
      betterAuthUserId: "firebase-user",
      providers: ["credential", "google", "apple"],
      sourceImportId: "firebase",
      status: "imported" as const,
    },
    betterAuthUserId: "firebase-user",
    requiredProviders: ["google", "apple"] as const,
    deletionFence: { status: "clear" as const },
    ...overrides,
  };
}

const VERIFIER = "A".repeat(43);
const STATE = "state-secret-123456";
const CODE = "code-secret-123456";

describe("legacy auth compatibility authority", () => {
  it("admits only a completed Firebase projection with an explicit clear fence", () => {
    expect(evaluateFirebaseIdentityAdmission(identityInput())).toEqual({ ok: true });
    expect(
      evaluateFirebaseIdentityAdmission(
        identityInput({ ledger: { ...identityInput().ledger, status: "applying" } }),
      ),
    ).toEqual({ ok: false, failure: "identity_import_incomplete" });
    expect(
      evaluateFirebaseIdentityAdmission(identityInput({ deletionFence: null })),
    ).toEqual({ ok: false, failure: "deletion_fence_unknown" });
    expect(
      evaluateFirebaseIdentityAdmission(
        identityInput({
          projection: { ...identityInput().projection, status: "conflict" },
        }),
      ),
    ).toEqual({ ok: false, failure: "identity_projection_conflict" });
    expect(
      evaluateFirebaseIdentityAdmission(
        identityInput({
          projection: { ...identityInput().projection, providers: ["credential"] },
        }),
      ),
    ).toEqual({ ok: false, failure: "identity_provider_mismatch" });
  });

  it("keeps native auth state and code secrets hash-only and single-use", async () => {
    const database = new SqliteD1();
    try {
      const challenge = await pkceChallengeForVerifier(VERIFIER);
      const created = await createLegacyAuthTransaction(database, {
        id: "auth-code-1",
        kind: "code",
        provider: "google",
        lookupSecret: CODE,
        stateSecret: STATE,
        redirectUri: "omi://auth/callback",
        codeChallenge: challenge,
        codeChallengeMethod: "S256",
        encryptedPayload: "encrypted-provider-envelope",
        createdAt: 1000,
        expiresAt: 301000,
      });
      expect(created.lookupHash).not.toContain(CODE);
      expect(
        database.database.prepare("SELECT * FROM cf_legacy_auth_transactions").get(),
      ).not.toMatchObject({ lookupHash: CODE, stateHash: STATE });

      await expect(
        consumeLegacyAuthTransaction(database, {
          lookupSecret: CODE,
          kind: "code",
          provider: "google",
          redirectUri: "omi://auth/callback",
          codeVerifier: "B".repeat(43),
          now: 2000,
        }),
      ).resolves.toBeNull();
      await expect(
        consumeLegacyAuthTransaction(database, {
          lookupSecret: CODE,
          kind: "code",
          provider: "google",
          redirectUri: "omi://auth/callback",
          codeVerifier: VERIFIER,
          now: 2000,
        }),
      ).resolves.toMatchObject({
        id: "auth-code-1",
        encryptedPayload: "encrypted-provider-envelope",
      });
      await expect(
        consumeLegacyAuthTransaction(database, {
          lookupSecret: CODE,
          kind: "code",
          provider: "google",
          redirectUri: "omi://auth/callback",
          codeVerifier: VERIFIER,
          now: 2001,
        }),
      ).resolves.toBeNull();
    } finally {
      database.close();
    }
  });

  it("binds external app consent to uid, CSRF, revision, and one-time state", async () => {
    const database = new SqliteD1();
    try {
      await createExternalOAuthTransaction(database, {
        id: "external-oauth-1",
        appId: "app-1",
        uid: "user-1",
        stateSecret: STATE,
        csrfSecret: "csrf-secret-123456",
        redirectUrl: "https://app.example.test/omi/callback",
        appCatalogRevision: 7,
        appPolicy: { private: false, paid: false, setupPolicyVersion: 1 },
        createdAt: 1000,
        expiresAt: 301000,
      });
      await expect(
        consumeExternalOAuthTransaction(database, {
          stateSecret: STATE,
          csrfSecret: "wrong-csrf-123456",
          uid: "user-1",
          now: 2000,
        }),
      ).resolves.toBeNull();
      await expect(
        consumeExternalOAuthTransaction(database, {
          stateSecret: STATE,
          csrfSecret: "csrf-secret-123456",
          uid: "other-user",
          now: 2000,
        }),
      ).resolves.toBeNull();
      await expect(
        consumeExternalOAuthTransaction(database, {
          stateSecret: STATE,
          csrfSecret: "csrf-secret-123456",
          uid: "user-1",
          now: 2000,
        }),
      ).resolves.toMatchObject({
        appId: "app-1",
        appCatalogRevision: 7,
      });
      await expect(
        consumeExternalOAuthTransaction(database, {
          stateSecret: STATE,
          csrfSecret: "csrf-secret-123456",
          uid: "user-1",
          now: 2001,
        }),
      ).resolves.toBeNull();
    } finally {
      database.close();
    }
  });

  it("requires public HTTPS app/setup targets and entitlement/setup checks", () => {
    const base = {
      uid: "user-1",
      app: {
        id: "app-1",
        enabled: true,
        externalIntegration: true,
        appHomeUrl: "https://app.example.test/home",
        private: false,
        paid: false,
        setupCompletedUrl: "https://api.example.test/setup",
      },
      entitlement: { paid: false, tester: false },
      setup: { checked: true, completed: true, targetPinned: true },
      deletionFence: { status: "clear" as const },
    };
    expect(evaluateExternalAppAdmission(base)).toEqual({ ok: true });
    expect(
      evaluateExternalAppAdmission({
        ...base,
        app: { ...base.app, setupCompletedUrl: "http://127.0.0.1/setup" },
      }),
    ).toEqual({ ok: false, failure: "external_setup_target_unsafe" });
    expect(
      evaluateExternalAppAdmission({
        ...base,
        app: { ...base.app, setupCompletedUrl: undefined },
        setup: { checked: false, completed: false, targetPinned: false },
      }),
    ).toEqual({ ok: true });
    expect(
      evaluateExternalAppAdmission({
        ...base,
        app: { ...base.app, paid: true },
      }),
    ).toEqual({ ok: false, failure: "external_app_not_entitled" });
    expect(
      evaluateExternalAppAdmission({
        ...base,
        setup: { checked: true, completed: false, targetPinned: true },
      }),
    ).toEqual({ ok: false, failure: "external_setup_incomplete" });
  });
});
