export type PhoneHistoryEntry = {
  uid: string;
  sourceRecordId: string;
  phoneNumberId: string;
  importId: string;
  phoneNumberHash: string | null;
  phoneNumberCiphertext: string | null;
  proofSha256: string | null;
  proofAttestedAt: number | null;
  sourceFingerprint: string | null;
  sourceExportSha256: string;
  twilioSid: string | null;
  friendlyName: string | null;
  verifiedAt: number | null;
  isPrimary: number;
  accountGeneration: number | null;
  createdAt: number | null;
  updatedAt: number | null;
  sourceRowSha256: string;
  planHash: string;
  action: "stage" | "blocked";
  status: "planned" | "blocked";
  lastError: string | null;
};

export type PhoneHistoryPlan = {
  mode: "dry-run";
  schema_version: 1;
  source: {
    kind: "firestore";
    collection: "users/{uid}/phone_numbers";
    ciphertext_scheme: "cloudflare-phone-aes-gcm-v1";
    proof_scheme: "sha256-v1";
    export_sha256: string;
    exported_at?: string;
  };
  manifest_sha256: string;
  max_rows: number;
  total: number;
  stage: number;
  blocked: number;
  entries: PhoneHistoryEntry[];
};

export function planPhoneHistory(
  input: unknown,
  options?: { maxRows?: number; fencedUids?: string[]; now?: number },
): PhoneHistoryPlan;
export function renderPhoneHistoryLedgerSql(plan: PhoneHistoryPlan, now?: number): string;
export function renderPhoneHistoryVerifySql(plan: PhoneHistoryPlan): string;
export function verifyPhoneHistory(
  plan: PhoneHistoryPlan,
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
