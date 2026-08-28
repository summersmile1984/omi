import { describe, expect, it } from "vitest";
import {
  createRealtimeBootstrap,
  verifyRealtimeBootstrap,
} from "../workers/shared/realtime-bootstrap";

describe("realtime bootstrap", () => {
  it("accepts a signed bootstrap only inside its short lifetime", async () => {
    const bootstrap = await createRealtimeBootstrap(
      "request-1",
      "test-secret",
      1_000,
    );
    expect(
      await verifyRealtimeBootstrap(
        bootstrap?.encoded || "",
        bootstrap?.signature || "",
        "test-secret",
        1_015,
      ),
    ).toMatchObject({ requestId: "request-1", issuedAt: 1_000 });
    expect(
      await verifyRealtimeBootstrap(
        bootstrap?.encoded || "",
        bootstrap?.signature || "",
        "test-secret",
        1_031,
      ),
    ).toBeNull();
  });

  it("rejects a bootstrap signed by another service secret", async () => {
    const bootstrap = await createRealtimeBootstrap(
      "request-1",
      "test-secret",
      1_000,
    );
    expect(
      await verifyRealtimeBootstrap(
        bootstrap?.encoded || "",
        bootstrap?.signature || "",
        "other-secret",
        1_001,
      ),
    ).toBeNull();
  });
});
