import { describe, expect, it } from "vitest";
import {
  createRealtimeTicket,
  verifyRealtimeTicket,
} from "../workers/shared/realtime-ticket";

const identity = {
  uid: "user-1",
  authority: "better-auth" as const,
  requestId: "request-1",
};

describe("realtime web ticket", () => {
  it("round-trips a signed short-lived identity", async () => {
    const ticket = await createRealtimeTicket(identity, "test-secret", 100);
    expect(ticket).toBeTruthy();
    expect(await verifyRealtimeTicket(ticket || "", "test-secret", 110)).toEqual(
      identity,
    );
  });

  it("rejects forged and expired tickets", async () => {
    const ticket = await createRealtimeTicket(identity, "test-secret", 100);
    expect(
      await verifyRealtimeTicket(`${ticket}forged`, "test-secret", 110),
    ).toBeNull();
    expect(
      await verifyRealtimeTicket(ticket || "", "test-secret", 131),
    ).toBeNull();
  });
});
