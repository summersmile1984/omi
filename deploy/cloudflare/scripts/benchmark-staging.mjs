import { readFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";
import { parseTokenPayload, resolveEdgeUrl } from "./smoke-staging.mjs";

const REQUEST_TIMEOUT_MS = 15_000;
const DEFAULT_ITERATIONS = 8;
const DEFAULT_P95_BUDGET_MS = 4_000;
const ENDPOINTS = [
  ["health", "/health", false],
  ["probe", "/v1/cf/probe", true],
  ["scores", "/v1/scores", true],
  ["focusStats", "/v1/focus-stats", true],
  ["screenActivitySummary", "/v1/screen-activity/summary", true],
  ["assistantSettings", "/v1/users/assistant-settings", true],
];

export function parseBenchmarkInteger(raw, fallback, { minimum = 1, maximum = 100 } = {}) {
  if (raw === undefined || raw === null || raw === "") return fallback;
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new Error(`benchmark integer must be between ${minimum} and ${maximum}`);
  }
  return value;
}

export function percentile(values, fraction) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(fraction * sorted.length) - 1));
  return Math.round(sorted[index] * 100) / 100;
}

async function request(fetchImpl, url, init = {}) {
  const response = await fetchImpl(url, { ...init, signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
  await response.arrayBuffer();
  return response;
}

export async function runBenchmark({
  edgeUrl = resolveEdgeUrl(),
  token,
  iterations = DEFAULT_ITERATIONS,
  p95BudgetMs = DEFAULT_P95_BUDGET_MS,
  fetchImpl = fetch,
} = {}) {
  if (!token || typeof token !== "string") throw new Error("benchmark requires a staging bearer token");
  if (!Number.isSafeInteger(iterations) || iterations < 1 || iterations > 100) {
    throw new Error("iterations must be between 1 and 100");
  }
  if (!Number.isFinite(p95BudgetMs) || p95BudgetMs <= 0) throw new Error("p95 budget must be positive");
  const base = resolveEdgeUrl(edgeUrl);
  const authHeaders = { authorization: `Bearer ${token}` };
  const metrics = {};
  for (const [name, path, authenticated] of ENDPOINTS) {
    const init = authenticated ? { headers: authHeaders } : {};
    // Remove the first request from the measured sample so Python Worker cold
    // start is visible in logs but does not make a warm-path budget flaky.
    const warm = await request(fetchImpl, `${base}${path}`, init);
    if (warm.status !== 200) throw new Error(`${name} warm-up expected HTTP 200, received HTTP ${warm.status}`);
    const samples = [];
    for (let index = 0; index < iterations; index += 1) {
      const started = performance.now();
      const response = await request(fetchImpl, `${base}${path}`, init);
      const elapsed = performance.now() - started;
      if (response.status !== 200) throw new Error(`${name} expected HTTP 200, received HTTP ${response.status}`);
      samples.push(elapsed);
    }
    const p50 = percentile(samples, 0.5);
    const p95 = percentile(samples, 0.95);
    const max = Math.round(Math.max(...samples) * 100) / 100;
    metrics[name] = { samples: samples.length, p50, p95, max, p95BudgetMs, withinBudget: p95 <= p95BudgetMs };
  }
  return {
    edgeUrl: base,
    iterations,
    p95BudgetMs,
    metrics,
    passed: Object.values(metrics).every((metric) => metric.withinBudget),
  };
}

async function readToken() {
  const direct = process.env.CLOUDFLARE_SMOKE_BEARER_TOKEN?.trim();
  if (direct) return direct;
  const tokenFile = process.env.CLOUDFLARE_SMOKE_TOKEN_FILE?.trim();
  if (!tokenFile) return null;
  return parseTokenPayload(await readFile(tokenFile, "utf8"));
}

async function main() {
  const token = await readToken();
  if (!token) throw new Error("set CLOUDFLARE_SMOKE_BEARER_TOKEN or CLOUDFLARE_SMOKE_TOKEN_FILE");
  const iterations = parseBenchmarkInteger(process.env.CLOUDFLARE_BENCHMARK_ITERATIONS, DEFAULT_ITERATIONS);
  const p95BudgetMs = Number(process.env.CLOUDFLARE_BENCHMARK_P95_MS || DEFAULT_P95_BUDGET_MS);
  const result = await runBenchmark({ token, iterations, p95BudgetMs });
  console.log(`Cloudflare staging benchmark: ${JSON.stringify(result)}`);
  if (process.env.CLOUDFLARE_BENCHMARK_ENFORCE === "1" && !result.passed) {
    throw new Error("one or more warm-path p95 budgets exceeded");
  }
}

if (process.argv[1]?.endsWith("benchmark-staging.mjs")) {
  main().catch((error) => {
    console.error(`Cloudflare staging benchmark failed: ${error instanceof Error ? error.message : "unknown error"}`);
    process.exitCode = 1;
  });
}
