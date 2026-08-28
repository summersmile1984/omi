export type JobMessage = {
  jobId: string;
  uid: string;
  kind: "probe" | "transcribe" | "sync_local_files" | "legacy_audio_rebuild";
  payload: Record<string, unknown>;
};

export type WorkersAiBinding = {
  run(model: string, input: Record<string, unknown>): Promise<unknown>;
};

export type JobsEnv = {
  APP_DB: D1Database;
  ASSETS: R2Bucket;
  AI: WorkersAiBinding;
  JOBS: Queue<JobMessage>;
  SYNC_FRESH: Queue<JobMessage>;
  SYNC_BACKFILL: Queue<JobMessage>;
  INTERNAL_ASSERTION_SECRET?: string;
  SYNC_CONTENT_ID_SECRET?: string;
  LEGACY_AUDIO_ENCRYPTION_SECRET?: string;
  WORKERS_AI_ASR_MODEL?: string;
  WORKERS_AI_SYNC_SUMMARY_MODEL?: string;
  WORKERS_AI_FAIR_USE_MODEL?: string;
  FIREBASE_SERVICE_ACCOUNT_JSON?: string;
};
