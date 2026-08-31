export type WrappedHistoryExportVerification = {
  verified: boolean;
  export_sha256: string;
  export_bytes: number;
  plan: import("./wrapped-history-reconcile.mjs").WrappedHistoryPlan;
};

export function verifyWrappedHistoryExport(
  bytes: Uint8Array,
  options?: { expectedSha256?: string | null; maxRows?: number },
): WrappedHistoryExportVerification;

export function applyReviewedWrappedHistoryPlan(
  plan: import("./wrapped-history-reconcile.mjs").WrappedHistoryPlan,
  options: {
    endpoint: string;
    adminKey: string;
    fetchImpl?: typeof fetch;
  },
): Promise<{
  review_id: string;
  status: "applied";
  manifest_sha256: string;
  entry_count: number;
  applied_count: number;
  already_applied_count: number;
}>;
