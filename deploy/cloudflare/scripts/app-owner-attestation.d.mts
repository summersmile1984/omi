export type AppOwnerDataAttestation = {
  source_uid: string;
  source_uid_hash: string;
  source_proof_hash: string;
  source_projection_revision: string;
  target_uid: string;
  target_account_generation: number;
  data_projection_revision: string;
  app_projection_count: number;
  memory_projection_count: number;
  memory_reencryption_status: "completed" | "not_required";
  memory_reencryption_revision: string | null;
};

export type AppOwnerDataAttestationReview = {
  schema_version: 1;
  kind: "app_owner_data_attestation_review";
  status: "ready_for_review";
  attestation: AppOwnerDataAttestation;
  evidence: {
    persona: Record<string, unknown>;
    chat: Record<string, unknown>;
    contentBoundRevisionInput: Record<string, unknown>;
  };
  safety: {
    firestore_connected: false;
    d1_connected: false;
    admin_endpoint_called: false;
    memory_reencryption_performed: false;
  };
};

export function buildAppOwnerDataAttestation(options: {
  persona: unknown;
  chat: unknown;
  sourceUid: string;
  sourceProofHash: string;
  sourceProjectionRevision: string;
  targetUid?: string;
  targetAccountGeneration?: number | string;
  memoryProjectionCount: number | string;
  memoryReencryptionStatus: "completed" | "not_required";
  memoryReencryptionRevision?: string | null;
}): AppOwnerDataAttestationReview;

export function renderAppOwnerDataAttestationSql(
  review: AppOwnerDataAttestationReview,
): string;

export function parseAppOwnerAttestationJson(
  raw: Uint8Array,
  label?: string,
): unknown;
