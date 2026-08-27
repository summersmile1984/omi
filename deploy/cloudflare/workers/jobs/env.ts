export type JobMessage = {
  jobId: string;
  uid: string;
  kind: "probe" | "transcribe";
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
  INTERNAL_ASSERTION_SECRET?: string;
  WORKERS_AI_ASR_MODEL?: string;
};
