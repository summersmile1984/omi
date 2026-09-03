export type WrappedHistoryEntry = {
  uid: string;
  year: number;
  jobId: string;
  requestFingerprint: string;
  sourceFingerprint: string;
  accountGeneration: number | null;
  resultJson: string | null;
  resultSha256: string | null;
  createdAt: number | null;
  updatedAt: number | null;
  sourceRowSha256: string;
  action: "stage" | "blocked";
  status: "planned" | "blocked";
  lastError: string | null;
};

export type WrappedHistoryPlan = {
  mode: "dry-run";
  schema_version: 1;
  source: {
    kind: "firestore";
    collection: "users/{uid}/wrapped/{year}";
    export_sha256: string | null;
    exported_at?: string;
  };
  manifest_sha256: string;
  max_rows: number;
  total: number;
  stage: number;
  blocked: number;
  entries: WrappedHistoryEntry[];
};

export function planWrappedHistory(
  input: unknown,
  options?: { maxRows?: number; fencedUids?: string[] },
): WrappedHistoryPlan;
export function renderWrappedHistorySql(
  plan: WrappedHistoryPlan,
  now?: number,
): string;
export function renderWrappedHistoryVerifySql(plan: WrappedHistoryPlan): string;
export function verifyWrappedHistory(
  plan: WrappedHistoryPlan,
  actualInput: unknown,
): {
  status: "passed" | "failed";
  manifest_sha256: string;
  checked: number;
  blocked: number;
  missing: string[];
  mismatched: Array<{ key: string; reasons: string[] }>;
  fenced_present: string[];
  duplicate_actual: string[];
};
