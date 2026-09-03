export type PhoneHistoryExportVerification = {
  verified: boolean;
  export_sha256: string;
  source_export_sha256: string;
  export_bytes: number;
  plan: import("./phone-history-reconcile.mjs").PhoneHistoryPlan;
};

export function verifyPhoneHistoryExport(
  bytes: Uint8Array,
  options?: {
    expectedSha256?: string | null;
    maxRows?: number;
    fencedUids?: string[];
  },
): PhoneHistoryExportVerification;

export function applyReviewedPhoneHistoryPlan(
  plan: import("./phone-history-reconcile.mjs").PhoneHistoryPlan,
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
