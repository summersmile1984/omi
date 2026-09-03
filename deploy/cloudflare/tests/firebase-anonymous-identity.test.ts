import { describe, expect, it, vi } from "vitest";
import {
  attestFirebaseAnonymousIdentity,
  FirebaseAnonymousIdentityError,
} from "../workers/auth/firebase-anonymous-identity";

const ENV = {
  FIREBASE_API_KEY: "AIzaSy-test-key",
  FIREBASE_PROJECT_ID: "omi-test-project",
  FIREBASE_IDENTITY_PROJECTION_SECRET:
    "firebase-identity-projection-secret-0123456789",
};

function base64Url(value: unknown): string {
  return btoa(JSON.stringify(value))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}

function token(
  overrides: Record<string, unknown> = {},
): string {
  const claims = {
    aud: ENV.FIREBASE_PROJECT_ID,
    iss: `https://securetoken.google.com/${ENV.FIREBASE_PROJECT_ID}`,
    sub: "firebase-anonymous-user",
    user_id: "firebase-anonymous-user",
    iat: 1_000,
    auth_time: 1_000,
    exp: 2_000,
    firebase: { sign_in_provider: "anonymous" },
    ...overrides,
  };
  return `${base64Url({ alg: "RS256", typ: "JWT" })}.${base64Url(claims)}.signature`;
}

function lookupResponse(overrides: Record<string, unknown> = {}): Response {
  return Response.json({
    users: [
      {
        localId: "firebase-anonymous-user",
        disabled: false,
        providerUserInfo: [],
        validSince: "900",
        ...overrides,
      },
    ],
  });
}

describe("Firebase anonymous identity attestation", () => {
  it("accepts an Identity Toolkit-validated anonymous credential and returns keyed evidence", async () => {
    const fetcher = vi.fn(async () => lookupResponse());
    const result = await attestFirebaseAnonymousIdentity(
      token(),
      "firebase-anonymous-user",
      ENV,
      fetcher,
      1_100,
    );

    expect(result).toMatchObject({
      sourceRef: expect.stringMatching(/^fb-anon-[0-9a-f]{64}$/),
      sourceUidHash: expect.stringMatching(/^[0-9a-f]{64}$/),
      sourceProofHash: expect.stringMatching(/^[0-9a-f]{64}$/),
      sourceCredentialGeneration: 900,
      attestedAt: 1_100,
      expiresAt: 2_000,
    });
    expect(result.sourceRef).toBe(`fb-anon-${result.sourceUidHash}`);
    expect(fetcher).toHaveBeenCalledWith(
      expect.stringContaining("accounts:lookup?key="),
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("rejects provider-linked, disabled, mismatched, and revoked identities", async () => {
    await expect(
      attestFirebaseAnonymousIdentity(
        token(),
        "different-user",
        ENV,
        async () => lookupResponse(),
        1_100,
      ),
    ).rejects.toMatchObject({ code: "source_identity_mismatch" });

    await expect(
      attestFirebaseAnonymousIdentity(
        token({ auth_time: 800 }),
        "firebase-anonymous-user",
        ENV,
        async () => lookupResponse(),
        1_100,
      ),
    ).rejects.toMatchObject({ code: "source_identity_revoked" });

    await expect(
      attestFirebaseAnonymousIdentity(
        token(),
        "firebase-anonymous-user",
        ENV,
        async () => lookupResponse({ providerUserInfo: [{ providerId: "google.com" }] }),
        1_100,
      ),
    ).rejects.toMatchObject({ code: "source_identity_rejected" });

    await expect(
      attestFirebaseAnonymousIdentity(
        token(),
        "firebase-anonymous-user",
        ENV,
        async () => lookupResponse({ disabled: true }),
        1_100,
      ),
    ).rejects.toMatchObject({ code: "source_identity_rejected" });
  });

  it("fails closed on provider errors, malformed claims, and invalid configuration", async () => {
    await expect(
      attestFirebaseAnonymousIdentity(
        token(),
        "firebase-anonymous-user",
        ENV,
        async () => new Response("upstream unavailable", { status: 503 }),
        1_100,
      ),
    ).rejects.toMatchObject({ code: "bridge_unavailable" });

    await expect(
      attestFirebaseAnonymousIdentity(
        token({ firebase: { sign_in_provider: "password" } }),
        "firebase-anonymous-user",
        ENV,
        async () => lookupResponse(),
        1_100,
      ),
    ).rejects.toBeInstanceOf(FirebaseAnonymousIdentityError);

    await expect(
      attestFirebaseAnonymousIdentity(
        token(),
        "firebase-anonymous-user",
        { ...ENV, FIREBASE_IDENTITY_PROJECTION_SECRET: "short" },
        async () => lookupResponse(),
        1_100,
      ),
    ).rejects.toMatchObject({ code: "bridge_unavailable" });
  });
});
