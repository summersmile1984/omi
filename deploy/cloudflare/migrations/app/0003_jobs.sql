CREATE TABLE IF NOT EXISTS cf_jobs (
  job_id TEXT PRIMARY KEY NOT NULL,
  uid TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_jobs_uid_updated_idx ON cf_jobs(uid, updated_at);
