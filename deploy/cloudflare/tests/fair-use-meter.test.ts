import { describe, expect, it, vi } from "vitest";
import {
  recordFairUseUsage,
  speechMsFromTranscription,
} from "../workers/shared/fair-use-meter";

describe("fair-use speech meter", () => {
  it("unions overlapping segments and ignores invalid intervals", () => {
    expect(
      speechMsFromTranscription({
        segments: [
          { start: 0, end: 1.5, text: "one" },
          { start: 1, end: 2, text: "overlap" },
          { start: 3.25, end: 4, text: "two" },
          { start: 5, end: 4, text: "invalid" },
          { start: 6, end: 7, text: "" },
        ],
      }),
    ).toBe(2_750);
  });

  it("uses word intervals when segments are empty", () => {
    expect(
      speechMsFromTranscription({
        segments: [],
        words: [{ start: 0.1, end: 0.6, word: "hi" }],
      }),
    ).toBe(500);
  });

  it("writes an exact revisioned idempotent source", async () => {
    const run = vi.fn(async () => ({ success: true }));
    const bind = vi.fn((..._values: unknown[]) => ({ run }));
    const prepare = vi.fn((_sql: string) => ({ bind }));
    vi.spyOn(Date, "now").mockReturnValue(200_000);

    await recordFairUseUsage({ prepare } as unknown as D1Database, {
      uid: "user-1",
      sourceKind: "realtime",
      sourceId: "realtime:connection-1",
      occurredAt: 100,
      speechMs: 1_250,
      revision: 3,
    });

    expect(prepare.mock.calls[0][0]).toContain(
      "ON CONFLICT(uid, source_kind, source_id)",
    );
    expect(prepare.mock.calls[0][0]).toContain("excluded.revision >=");
    expect(bind).toHaveBeenCalledWith(
      "user-1",
      "realtime",
      "realtime:connection-1",
      100,
      1_250,
      0,
      200,
      3,
    );
    expect(run).toHaveBeenCalledOnce();
  });
});
