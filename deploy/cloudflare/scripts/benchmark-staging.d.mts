export function parseBenchmarkInteger(raw: unknown, fallback: number, options?: { minimum?: number; maximum?: number }): number;
export function percentile(values: number[], fraction: number): number;
export function runBenchmark(options?: {
  edgeUrl?: string;
  token?: string;
  iterations?: number;
  p95BudgetMs?: number;
  fetchImpl?: (input: string, init?: RequestInit) => Promise<Response>;
}): Promise<Record<string, unknown>>;
