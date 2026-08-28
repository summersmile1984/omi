export function resolveEdgeUrl(raw?: string): string;
export function resolveWebUrl(raw?: string): string;
export function parseTokenPayload(raw: string): string;
export function runSmoke(options?: {
  edgeUrl?: string;
  webUrl?: string;
  token?: string | null;
  nativeTts?: boolean;
  fetchImpl?: (input: string, init?: RequestInit) => Promise<Response>;
}): Promise<Record<string, string | number>>;
