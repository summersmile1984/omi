import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import {
  applyReviewedWrappedHistoryPlan,
  verifyWrappedHistoryExport,
} from "../scripts/wrapped-history-export-verify.mjs";

function exportObject() {
  const event = {
    date: "March 15",
    title: "A memorable day",
    description: "A bounded description.",
    story: "A bounded story.",
    emoji: "🎉",
  };
  return {
    schema_version: 1,
    source: {
      kind: "firestore",
      collection: "users/{uid}/wrapped/{year}",
      exported_at: "2026-09-01T00:00:00Z",
    },
    rows: [
      {
        uid: "export-user",
        year: 2025,
        status: "done",
        source_fingerprint: "a".repeat(64),
        account_generation: 2,
        result: {
          decision_style: {
            name: "Reflective",
            description: "Decisive after reflection.",
          },
          top_phrases: [
            { phrase: "Let's do this", context: "Starting important work." },
          ],
          memorable_days: {
            most_fun_day: event,
            most_productive_day: event,
            most_stressful_day: event,
          },
          funniest_event: event,
          most_embarrassing_event: event,
          top_buddies: [
            {
              name: "Alex",
              relationship: "Friend",
              context: "Recurring collaborator.",
              emoji: "🤝",
            },
          ],
          obsessions: {
            show: "Not mentioned",
            movie: "Not mentioned",
            book: "Not mentioned",
            celebrity: "Not mentioned",
            food: "Not mentioned",
          },
          movie_recommendations: ["Inception"],
          struggle: { title: "A hard season", description: "Kept moving." },
          personal_win: {
            title: "Steady progress",
            description: "Made progress.",
          },
        },
        created_at: 1_735_689_600,
        updated_at: 1_735_689_900,
      },
    ],
  };
}

function bytes() {
  return new TextEncoder().encode(JSON.stringify(exportObject()));
}

function checksum(value: Uint8Array) {
  return createHash("sha256").update(value).digest("hex");
}

describe("Wrapped history export verification and execution tool", () => {
  it("binds the planner manifest to the original export bytes", () => {
    const input = bytes();
    const result = verifyWrappedHistoryExport(input, {
      expectedSha256: checksum(input),
    });
    expect(result).toMatchObject({
      verified: true,
      export_bytes: input.byteLength,
      export_sha256: checksum(input),
    });
    expect(result.plan).toMatchObject({
      source: { export_sha256: checksum(input) },
      total: 1,
      stage: 1,
      blocked: 0,
    });
  });

  it("rejects an independently supplied checksum mismatch and malformed UTF-8", () => {
    const input = bytes();
    expect(() =>
      verifyWrappedHistoryExport(input, { expectedSha256: "b".repeat(64) }),
    ).toThrow("does not match");
    expect(() =>
      verifyWrappedHistoryExport(new Uint8Array([0xff, 0xfe])),
    ).toThrow("UTF-8 JSON");
  });

  it("requires explicit verified apply and performs review then apply without exposing the key", async () => {
    const input = bytes();
    const plan = verifyWrappedHistoryExport(input, {
      expectedSha256: checksum(input),
    }).plan;
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = vi.fn(
      async (url: RequestInfo | URL, init?: RequestInit) => {
        requests.push({ url: String(url), init });
        if (requests.length === 1)
          return new Response(
            JSON.stringify({
              review_id: "00000000-0000-4000-8000-000000000000",
            }),
            { status: 201 },
          );
        return new Response(
          JSON.stringify({
            status: "applied",
            manifest_sha256: plan.manifest_sha256,
            entry_count: 1,
            applied_count: 1,
            already_applied_count: 0,
          }),
          { status: 200 },
        );
      },
    ) as unknown as typeof fetch;
    await expect(
      applyReviewedWrappedHistoryPlan(
        { ...plan, blocked: 1 },
        {
          endpoint: "https://jobs.test/internal/wrapped-history/reviews",
          adminKey: "secret",
          fetchImpl,
        },
      ),
    ).rejects.toThrow("all-stage");
    const result = await applyReviewedWrappedHistoryPlan(plan, {
      endpoint: "https://jobs.test/internal/wrapped-history/reviews/",
      adminKey: "secret",
      fetchImpl,
    });
    expect(result).toMatchObject({ status: "applied", entry_count: 1 });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(requests[0].url).toBe(
      "https://jobs.test/internal/wrapped-history/reviews",
    );
    expect(requests[1].url).toContain(
      "/00000000-0000-4000-8000-000000000000/apply",
    );
    expect(requests[0].init?.headers).toMatchObject({ "secret-key": "secret" });
    expect(JSON.stringify(result)).not.toContain("secret");
  });
});
