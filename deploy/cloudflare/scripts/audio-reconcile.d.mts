export type AudioReconciliationEntry = {
  uid: string;
  conversationId: string;
  importId: string;
  sourceObjectUri: string;
  sourceGeneration: string | null;
  sourceObjectName: string;
  checksum: string | null;
  size: number | null;
  sourceKind: "pcm" | "opus" | null;
  encrypted: boolean | null;
  batch: boolean | null;
  startTimestamp: number | null;
  endTimestamp: number | null;
  destinationKey: string;
  planHash: string;
  action: "stage" | "blocked";
  status: "planned" | "blocked";
  lastError: string | null;
  createdAt: number;
  updatedAt: number;
};

export type AudioReconciliationPlan = {
  mode: "dry-run";
  maxRows: number;
  total: number;
  stage: number;
  blocked: number;
  entries: AudioReconciliationEntry[];
};

export function planAudioReconciliation(
  records: unknown[],
  options?: { maxRows?: number; now?: number; fencedUids?: string[] },
): AudioReconciliationPlan;
export function renderAudioLedgerSql(plan: AudioReconciliationPlan, now?: number): string;
export function renderAudioR2Plan(plan: AudioReconciliationPlan): Array<{
  source_object_uri: string;
  source_generation: string;
  destination_key: string;
  checksum_sha256: string;
  size: number;
  source_kind: "pcm" | "opus";
  encrypted: boolean;
  batch: boolean;
  start_timestamp: number;
  end_timestamp: number;
  if_generation_match: string;
  status: "not_started";
}>;
