export type MemoryArchiveEntry = {
  uid: string;
  memory_id: string;
  source_fingerprint: string;
  source_row_sha256: string;
  import_id: string;
  plan_hash: string;
  account_generation: number;
  row: Record<string, unknown>;
  action: "stage" | "blocked";
  status: "planned" | "blocked";
  last_error: string | null;
};

export type MemoryArchivePlan = {
  mode: "dry-run";
  schema_version: 1;
  source: {
    kind: "firestore";
    collection: "users/{uid}/memories";
    export_sha256: string;
    exported_at?: string;
  };
  manifest_sha256: string;
  total: number;
  staged: number;
  blocked: number;
  entries: MemoryArchiveEntry[];
  apply_request: {
    manifest_sha256: string;
    source: MemoryArchivePlan["source"];
    entries: MemoryArchiveEntry[];
  };
};

export function planMemoryArchiveReconciliation(
  input: unknown,
  options?: { fencedUids?: string[] },
): MemoryArchivePlan;
