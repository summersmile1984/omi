export function resolveStagingEdgeUrl(raw?: string): string;

export function runDlqPositiveProbe(options: {
  edgeUrl?: string;
  messageId: string;
  adminKey: string;
  signingSecret: string;
  idempotencyKey?: string;
  nowSeconds?: number;
  fetchImpl?: (input: string, init?: RequestInit) => Promise<Response>;
}): Promise<{
  endpoint: string;
  messageId: string;
  idempotencyKey: string;
  status: "completed";
  requestedCount: number;
  queuedCount: number;
  skippedCount: number;
  failedCount: number;
}>;
