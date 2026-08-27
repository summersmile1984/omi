export function stagingHealthTargets(overrides?: Record<string, string>): Array<{
  name: string;
  url: string;
}>;

export function verifyStagingHealth(options?: {
  fetchImpl?: (input: string, init?: RequestInit) => Promise<Response>;
  targets?: Array<{ name: string; url: string }>;
  timeoutMs?: number;
}): Promise<Record<string, number>>;
