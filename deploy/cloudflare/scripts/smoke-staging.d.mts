export function resolveEdgeUrl(raw?: string): string;
export function resolveWebUrl(raw?: string): string;
export function parseTokenPayload(raw: string): string;
export function assertAuthenticatedSmokeConfigured(
  env?: Record<string, string | undefined>,
): void;
export function expectFenceOrRateLimit(
  label: string,
  response: Response,
  expected: number,
): void;
export function runSmoke(options?: {
  edgeUrl?: string;
  webUrl?: string;
  token?: string | null;
  nativeTts?: boolean;
  fetchImpl?: (input: string, init?: RequestInit) => Promise<Response>;
}): Promise<Record<string, string | number>>;
