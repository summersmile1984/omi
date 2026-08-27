import { describe, expect, it } from "vitest";
import { parseBenchmarkInteger, percentile, runBenchmark } from "../scripts/benchmark-staging.mjs";

describe("staging benchmark helpers", () => {
  it("computes bounded percentile samples and validates iteration inputs", () => {
    expect(percentile([3, 1, 2, 10], 0.5)).toBe(2);
    expect(percentile([], 0.95)).toBe(0);
    expect(parseBenchmarkInteger("8", 2)).toBe(8);
    expect(() => parseBenchmarkInteger("0", 2)).toThrow("between 1 and 100");
  });

  it("warms and measures only the fixed non-mutating staging endpoints", async () => {
    const calls: string[] = [];
    const fetchImpl = async (url: string) => {
      calls.push(url);
      return new Response(null, { status: 200 });
    };
    const result = await runBenchmark({
      edgeUrl: "https://edge.example.test",
      token: "staging-token",
      iterations: 2,
      p95BudgetMs: 1000,
      fetchImpl,
    });
    expect(result.passed).toBe(true);
    expect(result.iterations).toBe(2);
    expect(Object.keys(result.metrics as Record<string, unknown>)).toEqual([
      "health",
      "probe",
      "scores",
      "focusStats",
      "screenActivitySummary",
      "assistantSettings",
    ]);
    expect(calls).toHaveLength(18);
  });
});
