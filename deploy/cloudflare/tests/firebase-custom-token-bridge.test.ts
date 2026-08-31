import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { describe, expect, it, vi } from "vitest";
import {
  exchangeFirebaseCustomToken,
  exchangeFirebaseProviderCredential,
  FirebaseCustomTokenBridgeError,
  issueFirebaseCustomToken,
  parseFirebaseServiceAccount,
  verifyFirebaseIdToken,
} from "../workers/auth/firebase-custom-token-bridge";

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
        (this.database.prepare(sql).get(...(args as never[])) as
          T | undefined) ?? null,
      run: async () => {
        const result = this.database.prepare(sql).run(...(args as never[]));
        return {
          success: true as const,
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

function base64(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function base64UrlDecode(value: string): string {
  const padded =
    value.replaceAll("-", "+").replaceAll("_", "/") +
    "===".slice((value.length + 3) % 4);
  return atob(padded);
}

async function serviceAccount() {
  const keys = await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2_048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  );
  const der = new Uint8Array(
    await crypto.subtle.exportKey("pkcs8", keys.privateKey),
  );
  const pem = `-----BEGIN PRIVATE KEY-----\n${base64(der)}\n-----END PRIVATE KEY-----`;
  return {
    FIREBASE_PROJECT_ID: "omi-test-project",
    FIREBASE_SERVICE_ACCOUNT_JSON: JSON.stringify({
      project_id: "omi-test-project",
      client_email: "firebase-admin@omi-test-project.iam.gserviceaccount.com",
      private_key: pem,
      private_key_id: "key-1",
    }),
  };
}

function databaseWithIdentity() {
  const database = new SqliteD1();
  database.database.exec(`
    INSERT INTO user (id, name, email, emailVerified, createdAt, updatedAt)
    VALUES ('better-user', 'Better User', 'better@example.test', 1, 1, 1);
    INSERT INTO auth_identity_imports
      (id, sourceSha256, configFingerprint, canonicalSha256, userCount,
       accountCount, status, startedAt, completedAt)
    VALUES ('firebase', '${"a".repeat(64)}', 'config', '${"b".repeat(64)}', 1,
       1, 'completed', 1, 2);
    INSERT INTO cf_firebase_identity_projection
      (firebaseUid, betterAuthUserId, providersJson, sourceImportId, status,
       sourceUpdatedAt, updatedAt, sourceRecordSha256)
    VALUES ('firebase-user', 'better-user', '["credential"]', 'firebase',
       'imported', 1, 1, '${"c".repeat(64)}');
    INSERT INTO cf_auth_deletion_fences
      (uid, generation, status, startedAt, completedAt)
    VALUES ('better-user', 4, 'clear', 1, NULL);
  `);
  return database;
}

describe("Firebase custom-token bridge", () => {
  it("verifies an ID token through Identity Toolkit and admits the imported projection", async () => {
    const database = databaseWithIdentity();
    try {
      const fetcher = async (url: string, request: RequestInit) => {
        expect(url).toBe(
          "https://identitytoolkit.googleapis.com/v1/accounts:lookup?key=AIza-test-key",
        );
        expect(request.method).toBe("POST");
        expect(JSON.parse(String(request.body))).toEqual({
          idToken: "firebase-id-token",
        });
        return Response.json({
          users: [{ localId: "firebase-user", displayName: "Imported User" }],
        });
      };
      await expect(
        verifyFirebaseIdToken(
          database,
          "firebase-id-token",
          { FIREBASE_API_KEY: "AIza-test-key" },
          fetcher as unknown as typeof fetch,
        ),
      ).resolves.toEqual({
        firebaseUid: "firebase-user",
        betterAuthUserId: "better-user",
        displayName: "Imported User",
        accountCreatedAt: 1,
      });
    } finally {
      database.close();
    }
  });

  it("exchanges a provider credential through Firebase Identity Toolkit", async () => {
    const fetcher = async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp?key=AIza-test-key",
      );
      expect(init?.method).toBe("POST");
      const payload = JSON.parse(String(init?.body)) as {
        postBody: string;
        requestUri: string;
        returnIdpCredential: boolean;
        returnSecureToken: boolean;
      };
      expect(payload.requestUri).toBe("http://localhost");
      expect(payload.returnIdpCredential).toBe(true);
      expect(payload.returnSecureToken).toBe(true);
      const postBody = new URLSearchParams(payload.postBody);
      expect(postBody.get("providerId")).toBe("google.com");
      expect(postBody.get("id_token")).toBe("provider-id-token");
      expect(postBody.get("access_token")).toBe("provider-access-token");
      return Response.json({
        localId: "firebase-user",
        idToken: "firebase-id-token",
        refreshToken: "firebase-refresh-token",
        expiresIn: "3600",
      });
    };
    await expect(
      exchangeFirebaseProviderCredential(
        "google",
        "provider-id-token",
        "provider-access-token",
        { FIREBASE_API_KEY: "AIza-test-key" },
        fetcher,
      ),
    ).resolves.toEqual({
      localId: "firebase-user",
      idToken: "firebase-id-token",
      refreshToken: "firebase-refresh-token",
      expiresIn: 3_600,
    });
  });

  it("rejects malformed service-account credentials without exposing key data", () => {
    expect(parseFirebaseServiceAccount(undefined)).toBeNull();
    expect(parseFirebaseServiceAccount("not-json")).toBeNull();
    expect(
      parseFirebaseServiceAccount(
        JSON.stringify({
          project_id: "bad",
          client_email: "admin@example.test",
          private_key:
            "-----BEGIN PRIVATE KEY-----bad-----END PRIVATE KEY-----",
        }),
      ),
    ).toBeNull();
  });

  it("mints a signed short-lived token only for the imported generation", async () => {
    const database = databaseWithIdentity();
    try {
      const env = await serviceAccount();
      const result = await issueFirebaseCustomToken(
        database,
        "better-user",
        env,
        1_700_000_000,
        4,
      );
      expect(result).toMatchObject({
        firebaseUid: "firebase-user",
        accountGeneration: 4,
        expiresAt: 1_700_000_300,
      });
      const [header, encodedPayload, signature] = result.token.split(".");
      expect(header).toBeTruthy();
      expect(encodedPayload).toBeTruthy();
      expect(signature).toBeTruthy();
      const payload = JSON.parse(base64UrlDecode(encodedPayload));
      expect(payload).toMatchObject({
        iss: "firebase-admin@omi-test-project.iam.gserviceaccount.com",
        sub: "firebase-admin@omi-test-project.iam.gserviceaccount.com",
        uid: "firebase-user",
        claims: {
          omi_account_generation: 4,
          omi_bridge: "firebase-v1",
          jti: result.issuanceId,
        },
      });
      expect(
        database.database
          .prepare("SELECT status, tokenHash FROM cf_firebase_bridge_issuances")
          .get(),
      ).toMatchObject({ status: "issued" });
      const issuance = database.database
        .prepare("SELECT tokenHash FROM cf_firebase_bridge_issuances")
        .get() as { tokenHash?: string } | undefined;
      expect(issuance?.tokenHash).not.toContain(result.token);
    } finally {
      database.close();
    }
  });

  it("fails closed for generation mismatch, revoked identity, and deletion", async () => {
    const database = databaseWithIdentity();
    try {
      const env = await serviceAccount();
      database.database.exec(
        "UPDATE cf_firebase_identity_projection SET sourceRecordSha256 = NULL",
      );
      await expect(
        issueFirebaseCustomToken(database, "better-user", env, 1_700_000_000),
      ).rejects.toMatchObject({ code: "identity_not_admitted" });
      database.database.exec(
        `UPDATE cf_firebase_identity_projection SET sourceRecordSha256 = '${"c".repeat(64)}'`,
      );
      await expect(
        issueFirebaseCustomToken(
          database,
          "better-user",
          env,
          1_700_000_000,
          3,
        ),
      ).rejects.toMatchObject({ code: "account_generation_conflict" });

      database.database.exec(
        "UPDATE cf_auth_deletion_fences SET status = 'deleting' WHERE uid = 'better-user'",
      );
      await expect(
        issueFirebaseCustomToken(database, "better-user", env, 1_700_000_000),
      ).rejects.toMatchObject({ code: "deletion_fence_active" });

      database.database.exec(
        "UPDATE cf_auth_deletion_fences SET status = 'clear'; UPDATE cf_firebase_identity_projection SET status = 'revoked';",
      );
      await expect(
        issueFirebaseCustomToken(database, "better-user", env, 1_700_000_000),
      ).rejects.toMatchObject({ code: "identity_not_admitted" });
    } finally {
      database.close();
    }
  });

  it("exchanges through the pinned Firebase REST endpoint and validates the response", async () => {
    const fetcher = async (url: string, request: RequestInit) => {
      expect(url).toBe(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=AIza-test-key",
      );
      expect(request.method).toBe("POST");
      expect(request.headers).toMatchObject({
        "content-type": "application/json",
      });
      expect(JSON.parse(String(request.body))).toEqual({
        token: "custom-token",
        returnSecureToken: true,
      });
      return Response.json({
        idToken: "firebase-id-token",
        refreshToken: "firebase-refresh-token",
        expiresIn: "3600",
        localId: "firebase-user",
      });
    };
    await expect(
      exchangeFirebaseCustomToken(
        "custom-token",
        { FIREBASE_API_KEY: "AIza-test-key" },
        fetcher as unknown as typeof fetch,
      ),
    ).resolves.toEqual({
      idToken: "firebase-id-token",
      refreshToken: "firebase-refresh-token",
      expiresIn: 3_600,
      localId: "firebase-user",
    });

    await expect(
      exchangeFirebaseCustomToken(
        "custom-token",
        {},
        fetcher as unknown as typeof fetch,
      ),
    ).rejects.toMatchObject({ code: "provider_unavailable" });
    await expect(
      exchangeFirebaseCustomToken(
        "custom-token",
        { FIREBASE_API_KEY: "AIza-test-key" },
        async () =>
          Response.json(
            { error: { message: "secret provider detail" } },
            { status: 400 },
          ),
      ),
    ).rejects.toBeInstanceOf(FirebaseCustomTokenBridgeError);
  });

  it("bounds Firebase token exchange responses before parsing provider data", async () => {
    const oversized = new Response("provider body must not be read", {
      status: 200,
      headers: { "content-length": "32001" },
    });
    const text = vi.spyOn(oversized, "text");
    await expect(
      exchangeFirebaseCustomToken(
        "custom-token",
        { FIREBASE_API_KEY: "AIza-test-key" },
        async () => oversized,
      ),
    ).rejects.toMatchObject({ code: "provider_unavailable" });
    expect(text).not.toHaveBeenCalled();

    await expect(
      exchangeFirebaseCustomToken(
        "custom-token",
        { FIREBASE_API_KEY: "AIza-test-key" },
        async () =>
          new Response(
            JSON.stringify({
              idToken: "i".repeat(8_193),
              refreshToken: "refresh-token",
              localId: "firebase-user",
              expiresIn: "3600",
            }),
          ),
      ),
    ).rejects.toMatchObject({ code: "provider_unavailable" });

    await expect(
      exchangeFirebaseCustomToken(
        "custom-token",
        { FIREBASE_API_KEY: "AIza-test-key" },
        async () =>
          new Response("x".repeat(32_001), {
            status: 200,
          }),
      ),
    ).rejects.toMatchObject({ code: "provider_unavailable" });
  });
});
