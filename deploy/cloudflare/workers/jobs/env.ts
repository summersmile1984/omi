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
    | "app_owner_migration"
    | "stripe_webhook"
    | "conversation_finalize"
    | "conversation_reprocess"
    | "conversation_merge"
    | "audio_merge"
    | "audio_merge_legacy"
    | "task_intelligence_evaluate"
    | "wrapped_generate"
    | "hume_webhook"
    | "data_protection_migration"
    | "limitless_import"
    | "chat_assistant_poll"
    | "memory_short_term_lifecycle";
  payload: Record<string, unknown>;
};

export type WorkersAiBinding = {
  run(model: string, input: Record<string, unknown>): Promise<unknown>;
};

/** Narrow Images binding surface used for private chat-file thumbnails. */
export type ImagesTransformBinding = {
  input(stream: ReadableStream<Uint8Array>): {
    transform(options: {
      width: number;
      height: number;
      fit: "scale-down" | "contain" | "pad" | "squeeze" | "cover" | "crop";
    }): {
      output(options: {
        format: "jpeg" | "png" | "webp";
        quality?: number;
      }): Promise<{
        response(options?: { headers?: HeadersInit }): Response;
        contentType(): string;
        image(options?: { encoding?: "base64" }): ReadableStream<Uint8Array>;
      }>;
    };
  };
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
  /** Optional until the account has Cloudflare Images transformations enabled. */
  IMAGES?: ImagesTransformBinding;
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
  /** Explicit opt-in for the direct OpenAI Assistants continuity adapter. */
  CHAT_ASSISTANT_PROVIDER_STAGING_ENABLED?: string;
  /** OpenAI Assistant id used by the explicit staging adapter. */
  OPENAI_ASSISTANT_ID?: string;
  /** HMAC key used by the unauthenticated, short-lived private thumbnail URL. */
  CHAT_FILE_THUMBNAIL_SECRET?: string;
  /** Explicit opt-in while the legacy upload owner is being cut over. */
  LEGACY_CHAT_FILES_STAGING_ENABLED?: string;
  /** Cloudflare-owned exact Chat compatibility routes in isolated staging. */
  CHAT_COMPATIBILITY_CLOUDFLARE_ENABLED?: string;
  /** Explicit opt-in for legacy-shaped attachment SSE/JSON envelopes. */
  CHAT_ATTACHMENT_ENVELOPE_STAGING_ENABLED?: string;
  /** Workers AI chat model and quota pricing used by exact compatibility routes. */
  WORKERS_AI_CHAT_MODEL?: string;
  FREE_CHAT_QUESTIONS_PER_MONTH?: string;
  WORKERS_AI_CHAT_INPUT_USD_PER_MILLION?: string;
  WORKERS_AI_CHAT_OUTPUT_USD_PER_MILLION?: string;
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
  WORKERS_AI_WRAPPED_MODEL?: string;
  FIREBASE_SERVICE_ACCOUNT_JSON?: string;
  /** Firebase Web API key/domain/project used by the legacy app-consent page and token verifier. */
  FIREBASE_API_KEY?: string;
  FIREBASE_AUTH_DOMAIN?: string;
  FIREBASE_PROJECT_ID?: string;
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
  /** Explicit staging gate for the namespaced external MCP OAuth seam. */
  MCP_APP_OAUTH_STAGING_ENABLED?: string;
  /** Explicit staging gate for the exact legacy /v1/apps/mcp owner. */
  MCP_APP_LEGACY_EXACT_STAGING_ENABLED?: string;
  /** AES-GCM key material for external MCP app OAuth envelopes. */
  MCP_APP_TOKEN_ENCRYPTION_SECRET?: string;
  /** Explicit staging gate for the Better Auth app-consent OAuth seam. */
  EXTERNAL_APP_OAUTH_STAGING_ENABLED?: string;
  /** Explicit opt-in for the exact legacy /v1/oauth/* owner. */
  LEGACY_EXTERNAL_APP_OAUTH_STAGING_ENABLED?: string;
  /** Dormant namespaced Twitter ownership evidence seam; exact legacy owner stays fail-closed. */
  TWITTER_OWNERSHIP_STAGING_ENABLED?: string;
  /** Explicit opt-in for the exact Twitter ownership staging owner. */
  TWITTER_OWNERSHIP_EXACT_STAGING_ENABLED?: string;
  /** Dormant app-owner migration admission seam; exact legacy owner stays fail-closed. */
  APP_OWNER_MIGRATION_STAGING_ENABLED?: string;
  /** Explicit opt-in for the legacy /v1/apps/migrate-owner compatibility adapter. */
  APP_OWNER_MIGRATION_EXACT_STAGING_ENABLED?: string;
  /** Auth-verified anonymous Firebase source projection into hash-only App D1 evidence. */
  FIREBASE_IDENTITY_PROJECTION_STAGING_ENABLED?: string;
  /** Explicit executor gate; no API Core migration executor is enabled by default. */
  APP_OWNER_MIGRATION_EXECUTOR_STAGING_ENABLED?: string;
  /** Reviewed source app/memory projection attestation gate; disabled by default. */
  APP_OWNER_MIGRATION_DATA_ATTESTATION_STAGING_ENABLED?: string;
  HUME_WEBHOOK_SIGNING_KEY?: string;
  /** Twilio REST credentials and Voice SDK token configuration. */
  TWILIO_ACCOUNT_SID?: string;
  TWILIO_AUTH_TOKEN?: string;
  TWILIO_API_KEY_SID?: string;
  TWILIO_API_KEY_SECRET?: string;
  TWILIO_TWIML_APP_SID?: string;
  /** AES-GCM key material for caller-ID numbers retained in D1. */
  PHONE_DATA_ENCRYPTION_SECRET?: string;
  /** Explicit operator gate for reviewed historical phone-number promotion. */
  PHONE_HISTORY_IMPORT_STAGING_ENABLED?: string;
  /** Explicit operator gate for reviewed historical chat-session/message promotion. */
  CHAT_HISTORY_IMPORT_STAGING_ENABLED?: string;
  /** HMAC secret for content-bound chat-history apply plans. */
  CHAT_HISTORY_IMPORT_SIGNING_SECRET?: string;
  /** Explicit operator gate for reviewed historical chat-file promotion. */
  CHAT_FILE_HISTORY_IMPORT_STAGING_ENABLED?: string;
  /** HMAC secret for content-bound chat-file review/apply plans. */
  CHAT_FILE_HISTORY_IMPORT_SIGNING_SECRET?: string;
  /** HMAC secret for external provider-object existence attestations. */
  CHAT_FILE_HISTORY_PROVIDER_ATTESTATION_SECRET?: string;
  /** Explicit operator gate for reviewed historical Wrapped result promotion. */
  WRAPPED_HISTORY_IMPORT_STAGING_ENABLED?: string;
  /** Explicit operator gate for reviewed Firestore Archive projection import. */
  MEMORY_ARCHIVE_IMPORT_STAGING_ENABLED?: string;
  /** HMAC secret for content-bound reviewed Archive projection plans. */
  MEMORY_ARCHIVE_IMPORT_SIGNING_SECRET?: string;
  /** Explicit operator gate for reviewed historical desktop manifest promotion. */
  DESKTOP_RELEASE_HISTORY_IMPORT_STAGING_ENABLED?: string;
  /** Explicit operator gate for reviewed public Persona/App history promotion. */
  PERSONA_APP_HISTORY_IMPORT_STAGING_ENABLED?: string;
  /** HMAC secret for content-bound public Persona/App history plans. */
  PERSONA_APP_HISTORY_IMPORT_SIGNING_SECRET?: string;
  /** Explicit operator gate for the D1-indexed Queue DLQ replay boundary. */
  DLQ_REPLAY_STAGING_ENABLED?: string;
  /** HMAC secret for bounded, content-bound DLQ replay requests. */
  DLQ_REPLAY_SIGNING_SECRET?: string;
  /** Explicit operator gate for reviewed Hume request_id task projections. */
  HUME_TASK_PROJECTION_STAGING_ENABLED?: string;
  /** Explicit operator gate for staging-only encrypted payload preparation. */
  DATA_PROTECTION_EXECUTOR_STAGING_ENABLED?: string;
  /** UTF-8 master secret matching backend/utils/encryption.py. */
  DATA_PROTECTION_ENCRYPTION_SECRET?: string;
};
