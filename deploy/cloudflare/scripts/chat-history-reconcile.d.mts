export type ChatHistoryPlanEntry = {
  uid: string;
  entityKind: "session" | "message";
  entityId: string;
  accountGeneration: number;
  sourceFingerprint: string;
  sourceExportSha256: string;
  sourceRowSha256: string;
  importId: string;
  row: Record<string, unknown>;
  fileIds: string[];
  action: "stage" | "blocked";
  status: "planned" | "blocked";
  lastError: string | null;
  planHash: string;
};

export type ChatHistoryPlan = {
  mode: "reviewed-plan";
  schemaVersion: number;
  source: Record<string, unknown>;
  manifestHash: string;
  total: number;
  stage: number;
  blocked: number;
  entries: ChatHistoryPlanEntry[];
};

export function planChatHistoryReconciliation(
  manifest: unknown,
  options?: { maxEntities?: number; fencedUids?: string[] },
): ChatHistoryPlan;
export function renderChatHistoryApplySql(plan: ChatHistoryPlan, now?: number): string;
export function renderChatHistoryVerifySql(plan: ChatHistoryPlan): string;
