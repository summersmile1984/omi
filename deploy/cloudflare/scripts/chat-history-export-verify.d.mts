export type ChatHistoryExportVerification = {
  verified: boolean;
  export_sha256: string;
  export_bytes: number;
  plan: import("./chat-history-reconcile.mjs").ChatHistoryPlan;
};

export function verifyChatHistoryExport(
  bytes: Uint8Array,
  options?: {
    expectedSha256?: string | null;
    maxEntities?: number;
    fencedUids?: string[];
  },
): ChatHistoryExportVerification;

export function signChatHistoryPlan(
  plan: import("./chat-history-reconcile.mjs").ChatHistoryPlan,
  signingSecret: string,
): { batchId: string; signature: string };

export function applyChatHistoryPlan(
  plan: import("./chat-history-reconcile.mjs").ChatHistoryPlan,
  options: {
    endpoint: string;
    adminKey: string;
    signingSecret: string;
    fetchImpl?: typeof fetch;
  },
): Promise<{
  batch_id: string;
  status: "applied";
  manifest_sha256: string;
  entry_count: number;
  applied_count: number;
  already_applied_count: number;
}>;
