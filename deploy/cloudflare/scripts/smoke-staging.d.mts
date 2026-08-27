export function resolveEdgeUrl(raw?: string): string;
export function parseTokenPayload(raw: string): string;
export function runSmoke(options?: {
  edgeUrl?: string;
  token?: string | null;
  fetchImpl?: (input: string, init?: RequestInit) => Promise<Response>;
}): Promise<Record<string, string | number>>;
