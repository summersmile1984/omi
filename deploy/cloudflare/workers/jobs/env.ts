export type JobMessage = {
  jobId: string;
  uid: string;
  kind:
    | "probe"
    | "transcribe"
    | "sync_local_files"
    | "legacy_audio_rebuild"
    | "vector_project"
    | "account_delete"
    | "app_delete"
    | "stripe_webhook";
  payload: Record<string, unknown>;
};

export type WorkersAiBinding = {
  run(model: string, input: Record<string, unknown>): Promise<unknown>;
};

export type VectorizeBinding = {
  upsert(vectors: Array<Record<string, unknown>>): Promise<unknown>;
  deleteByIds(ids: string[]): Promise<unknown>;
};

export type JobsEnv = {
  AUTH: Fetcher;
  APP_DB: D1Database;
  ASSETS: R2Bucket;
  AI: WorkersAiBinding;
  MEMORY_VECTORS: VectorizeBinding;
  ACTION_ITEM_VECTORS: VectorizeBinding;
  CONVERSATION_VECTORS: VectorizeBinding;
  TRANSCRIPT_CHUNK_VECTORS: VectorizeBinding;
  X_POST_VECTORS: VectorizeBinding;
  JOBS: Queue<JobMessage>;
  SYNC_FRESH: Queue<JobMessage>;
  SYNC_BACKFILL: Queue<JobMessage>;
  INTERNAL_ASSERTION_SECRET?: string;
  SYNC_CONTENT_ID_SECRET?: string;
  LEGACY_AUDIO_ENCRYPTION_SECRET?: string;
  STRIPE_SECRET_KEY?: string;
  STRIPE_WEBHOOK_SECRET?: string;
  STRIPE_WEBHOOK_SECRET_PREVIOUS?: string;
  STRIPE_CONNECT_WEBHOOK_SECRET?: string;
  STRIPE_CONNECT_WEBHOOK_SECRET_PREVIOUS?: string;
  STRIPE_CONNECT_REFRESH_SECRET?: string;
  PUBLIC_API_BASE_URL?: string;
  WORKERS_AI_ASR_MODEL?: string;
  WORKERS_AI_SYNC_SUMMARY_MODEL?: string;
  WORKERS_AI_FAIR_USE_MODEL?: string;
  WORKERS_AI_VECTOR_MODEL?: string;
  FIREBASE_SERVICE_ACCOUNT_JSON?: string;
  APPS_ADMIN_KEY?: string;
};
