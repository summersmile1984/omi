export type PersonaAppHistoryEntry = {
  sourceRef: string;
  sourceUidHash: string | null;
  uid: string;
  appId: string;
  sourceProjectionRevision: string | null;
  targetAccountGeneration: number | null;
  sourceFingerprint: string;
  sourceExportSha256: string;
  publicMetadataJson: string | null;
  privateEnvelope: {
    format: "v1.compact-aes-gcm" | "v1.aes-gcm";
    keyVersion: number | null;
    sha256: string;
  } | null;
  imageObject: {
    sourceObjectUri: string;
    sourceGeneration: string;
    checksumSha256: string;
    size: number;
    contentType: string;
    destinationKey: string;
  } | null;
  createdAt: number | null;
  updatedAt: number | null;
  requestFingerprint: string;
  idempotencyKey: string;
  sourceRowSha256: string;
  action: "stage" | "blocked";
  status: "planned" | "blocked";
  lastError: string | null;
};

export type PersonaAppHistoryPlan = {
  mode: "dry-run";
  schema_version: 1;
  source: {
    kind: "firestore";
    collection: "plugins_data";
    export_sha256: string;
    exported_at?: string;
  };
  max_rows: number;
  total: number;
  stage: number;
  blocked: number;
  entries: PersonaAppHistoryEntry[];
  manifest_sha256: string;
};

export function planPersonaAppHistory(
  input: unknown,
  options?: { maxRows?: number; fencedUids?: string[] },
): PersonaAppHistoryPlan;

export function renderPersonaAppHistoryOperations(plan: PersonaAppHistoryPlan): Array<{
  idempotency_key: string;
  d1: {
    operation: "insert_public_catalog_if_absent";
    table: "cf_app_catalog";
    key: { id: string; owner_uid: string };
    owner_account_generation: number;
    data_json_sha256: string;
  };
  private: {
    operation: "store_encrypted_envelope";
    format: "v1.compact-aes-gcm" | "v1.aes-gcm";
    sha256: string;
    key_version: number | null;
  } | null;
  r2: {
    operation: "copy_after_generation_check";
    sourceObjectUri: string;
    sourceGeneration: string;
    checksumSha256: string;
    size: number;
    contentType: string;
    destinationKey: string;
  } | null;
  guards: {
    source_export_sha256: string;
    source_row_sha256: string;
    source_projection_revision: string;
    account_generation: number;
    deletion_fence: "must_be_clear_at_apply_time";
  };
}>;

export function verifyPersonaAppHistory(
  plan: PersonaAppHistoryPlan,
  actualInput: unknown,
): {
  status: "passed" | "failed";
  manifest_sha256: string;
  checked: number;
  blocked: number;
  missing: string[];
  mismatched: Array<{ key: string; reasons: string[] }>;
  duplicate_actual: string[];
};

