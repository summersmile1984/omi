export type ChatFileReconciliationEntry = {
  uid: string;
  importId: string;
  sourceFileId: string;
  sourceObjectUri: string;
  sourceGeneration: string | null;
  checksum: string | null;
  providerFileId: string | null;
  name: string;
  mimeType: string;
  size: number | null;
  storageKey: string;
  requestFingerprint: string | null;
  createdAt: number;
  updatedAt: number;
  action: "stage" | "blocked";
  status: "planned" | "blocked";
  lastError: string | null;
  planHash: string;
};

export type ChatFileReconciliationPlan = {
  mode: "dry-run";
  maxRows: number;
  total: number;
  stage: number;
  blocked: number;
  entries: ChatFileReconciliationEntry[];
};

export function planChatFileReconciliation(
  records: unknown[],
  options?: {
    maxRows?: number;
    now?: number;
    fencedUids?: string[];
  },
): ChatFileReconciliationPlan;
export function renderChatFileLedgerSql(
  plan: ChatFileReconciliationPlan,
  now?: number,
): string;
export function renderChatFileR2Plan(
  plan: ChatFileReconciliationPlan,
): Array<{
  source_object_uri: string;
  destination_key: string;
  checksum_sha256: string;
  size: number;
  provider_file_id: string;
  status: "not_started";
}>;
