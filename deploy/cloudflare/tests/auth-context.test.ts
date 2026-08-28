import { describe, expect, it } from "vitest";
import {
  createSignedAuthContext,
  decodeAuthContext,
  signAuthContext,
  verifyAuthContextSignature,
  verifyRequestAuthContext,
} from "../workers/shared/auth-context";

const identity = {
  uid: "user-测试",
  authority: "better-auth" as const,
  requestId: "req-1",
};

describe("auth context", () => {
  it("round-trips a request-bound context and rejects malformed values", async () => {
    const signed = await createSignedAuthContext(
      identity,
      "api-core",
      "GET",
      "/v1/conversations",
      "test-secret",
      100,
    );
    expect(signed).toBeTruthy();
    expect(decodeAuthContext(signed?.encoded || "")).toEqual(signed?.context);
    expect(decodeAuthContext(`${signed?.encoded}!`)).toBeNull();

    expect(await signAuthContext(signed?.encoded || "", undefined)).toBeNull();
    expect(
      await verifyAuthContextSignature(
        signed?.encoded || "",
        signed?.signature || null,
        "test-secret",
      ),
    ).toBe(true);
    expect(
      await verifyAuthContextSignature(
        signed?.encoded || "",
        signed?.signature || null,
        "wrong-secret",
      ),
    ).toBe(false);
  });

  it("rejects expired, cross-audience, cross-method, and cross-path replay", async () => {
    const signed = await createSignedAuthContext(
      identity,
      "api-core",
      "GET",
      "/v1/conversations",
      "test-secret",
      100,
    );
    const request = (method: string, path: string) =>
      new Request(`https://core.test${path}`, {
        method,
        headers: {
          "x-omi-auth-context": signed?.encoded || "",
          "x-omi-internal-signature": signed?.signature || "",
        },
      });

    expect(
      await verifyRequestAuthContext(
        request("GET", "/v1/conversations"),
        "api-core",
        "test-secret",
        120,
      ),
    ).toMatchObject(identity);
    expect(
      await verifyRequestAuthContext(
        request("GET", "/v1/conversations"),
        "api-ai",
        "test-secret",
        120,
      ),
    ).toBeNull();
    expect(
      await verifyRequestAuthContext(
        request("POST", "/v1/conversations"),
        "api-core",
        "test-secret",
        120,
      ),
    ).toBeNull();
    expect(
      await verifyRequestAuthContext(
        request("GET", "/v1/conversations/other"),
        "api-core",
        "test-secret",
        120,
      ),
    ).toBeNull();
    expect(
      await verifyRequestAuthContext(
        request("GET", "/v1/conversations"),
        "api-core",
        "test-secret",
        161,
      ),
    ).toBeNull();
  });
});
