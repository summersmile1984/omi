import { describe, expect, it } from "vitest";
import {
  fairUseRestrictionResponse,
  readFairUseRestriction,
} from "../workers/shared/fair-use-enforcement";

function restrictionDatabase(
  row: Record<string, unknown>,
  options: { failRead?: boolean } = {},
) {
  const updates: unknown[][] = [];
  return {
    updates,
    database: {
      prepare: (sql: string) => ({
        bind: (...args: unknown[]) => ({
          first: async () => {
            if (options.failRead) throw new Error("D1 unavailable");
            return row;
          },
          run: async () => {
            updates.push([sql, ...args]);
            return { meta: { changes: 1 } };
          },
        }),
      }),
    },
  };
}

describe("fair-use cost gate", () => {
  it("blocks an active restrict stage only while default live caps remain exceeded", async () => {
    const now = 2_000_000_000;
    const blocked = restrictionDatabase({
      stage: "restrict",
      restrict_until: now + 90,
      daily_ms: 7_200_001,
      three_day_ms: 7_200_001,
      weekly_ms: 7_200_001,
    });
    const decision = await readFairUseRestriction(
      blocked.database as never,
      "user-1",
      now,
    );
    expect(decision).toMatchObject({
      blocked: true,
      reason: "fair_use_restricted",
      retryAfter: 90,
      stage: "restrict",
    });
    const response = fairUseRestrictionResponse(decision!);
    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toBe("90");
    expect(response.headers.get("x-omi-rate-limit-reason")).toBe("fair_use");

    const belowCap = restrictionDatabase({
      stage: "restrict",
      restrict_until: now + 90,
      daily_ms: 7_200_000,
      three_day_ms: 28_800_000,
      weekly_ms: 36_000_000,
    });
    expect(
      await readFairUseRestriction(belowCap.database as never, "user-1", now),
    ).toBeNull();
  });

  it("persists restrict expiry as throttle instead of stranding the account", async () => {
    const now = 2_000_000_000;
    const expired = restrictionDatabase({
      stage: "restrict",
      restrict_until: now - 1,
      daily_ms: 8_000_000,
      three_day_ms: 8_000_000,
      weekly_ms: 8_000_000,
    });
    expect(
      await readFairUseRestriction(expired.database as never, "user-1", now),
    ).toBeNull();
    expect(expired.updates).toHaveLength(1);
    expect(String(expired.updates[0][0])).toContain("stage = 'throttle'");
  });

  it("applies the 30-hour ceiling to every stage and fails open on a D1 outage", async () => {
    const now = 2_000_000_000;
    const ceiling = restrictionDatabase({
      stage: "none",
      restrict_until: null,
      daily_ms: 108_000_000,
      three_day_ms: 108_000_000,
      weekly_ms: 108_000_000,
    });
    expect(
      await readFairUseRestriction(ceiling.database as never, "user-1", now),
    ).toMatchObject({ reason: "daily_audio_ceiling", stage: "none" });

    const outage = restrictionDatabase({}, { failRead: true });
    expect(
      await readFairUseRestriction(outage.database as never, "user-1", now),
    ).toBeNull();
  });
});
