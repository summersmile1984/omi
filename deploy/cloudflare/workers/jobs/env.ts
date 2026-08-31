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
    | "recording_delete"
    | "app_delete"
    | "stripe_webhook"
    | "conversation_finalize"
    | "conversation_reprocess"
    | "conversation_merge"
    | "audio_merge"
    | "hume_webhook"
    | "limitless_import"
    | "memory_short_term_lifecycle";
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
  API_CORE?: Fetcher;
  APP_DB: D1Database;
  ASSETS: R2Bucket;
  CHAT_FILES?: R2Bucket;
  CONVERSATION_RECORDINGS: R2Bucket;
  SPEECH_PROFILES: R2Bucket;
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
  OPENAI_API_KEY?: string;
  SYNC_CONTENT_ID_SECRET?: string;
  LEGACY_AUDIO_ENCRYPTION_SECRET?: string;
  STRIPE_SECRET_KEY?: string;
  STRIPE_WEBHOOK_SECRET?: string;
  STRIPE_WEBHOOK_SECRET_PREVIOUS?: string;
  STRIPE_CONNECT_WEBHOOK_SECRET?: string;
  STRIPE_CONNECT_WEBHOOK_SECRET_PREVIOUS?: string;
  STRIPE_CONNECT_REFRESH_SECRET?: string;
  PUBLIC_API_BASE_URL?: string;
  PUBLIC_SHARE_BASE_URL?: string;
  WORKERS_AI_ASR_MODEL?: string;
  WORKERS_AI_SYNC_SUMMARY_MODEL?: string;
  WORKERS_AI_FAIR_USE_MODEL?: string;
  WORKERS_AI_VECTOR_MODEL?: string;
  WORKERS_AI_X_MEMORY_MODEL?: string;
  FIREBASE_SERVICE_ACCOUNT_JSON?: string;
  ADMIN_KEY?: string;
  APPS_ADMIN_KEY?: string;
  X_OAUTH_CLIENT_ID?: string;
  X_OAUTH_CLIENT_SECRET?: string;
  X_OAUTH_REDIRECT_URI?: string;
  X_OAUTH_SCOPES?: string;
  X_TOKEN_ENCRYPTION_SECRET?: string;
  TASK_INTEGRATION_TOKEN_ENCRYPTION_SECRET?: string;
  TODOIST_CLIENT_ID?: string;
  TODOIST_CLIENT_SECRET?: string;
  ASANA_CLIENT_ID?: string;
  ASANA_CLIENT_SECRET?: string;
  GOOGLE_TASKS_CLIENT_ID?: string;
  GOOGLE_TASKS_CLIENT_SECRET?: string;
  /** Shared Google OAuth client used by Better Auth and Google integrations. */
  GOOGLE_CLIENT_ID?: string;
  GOOGLE_CLIENT_SECRET?: string;
  GOOGLE_CALENDAR_CLIENT_ID?: string;
  GOOGLE_CALENDAR_CLIENT_SECRET?: string;
  GOOGLE_CALENDAR_TOKEN_ENCRYPTION_SECRET?: string;
  CLICKUP_CLIENT_ID?: string;
  CLICKUP_CLIENT_SECRET?: string;
  RAPID_API_HOST?: string;
  RAPID_API_KEY?: string;
  HUME_WEBHOOK_SIGNING_KEY?: string;
};
