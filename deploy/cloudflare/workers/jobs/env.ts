export type JobMessage = {
  jobId: string;
  uid: string;
  kind: "probe";
  payload: Record<string, unknown>;
};

export type JobsEnv = {
  APP_DB: D1Database;
  JOBS: Queue<JobMessage>;
  INTERNAL_ASSERTION_SECRET?: string;
};
