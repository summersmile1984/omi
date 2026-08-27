import { describe, expect, it } from "vitest";
import {
  decodeAuthContext,
  encodeAuthContext,
  signAuthContext,
  verifyAuthContextSignature,
} from "../workers/shared/auth-context";

describe("auth context", () => {
  it("round-trips unicode-safe context and rejects malformed values", async () => {
    const context = { uid: "user-测试", authority: "better-auth" as const, requestId: "req-1" };
    const encoded = encodeAuthContext(context);
    expect(decodeAuthContext(encoded)).toEqual(context);
    expect(decodeAuthContext(`${encoded}!`)).toBeNull();

    const signature = await signAuthContext(encoded, "test-secret");
    expect(signature).toBeTruthy();
    expect(await signAuthContext(encoded, undefined)).toBeNull();
    expect(await verifyAuthContextSignature(encoded, signature, "test-secret")).toBe(true);
    expect(await verifyAuthContextSignature(encoded, signature, "wrong-secret")).toBe(false);
    expect(await verifyAuthContextSignature(encoded, null, "test-secret")).toBe(false);
  });
});
