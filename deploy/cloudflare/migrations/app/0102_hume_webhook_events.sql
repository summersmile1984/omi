CREATE TABLE IF NOT EXISTS cf_hume_webhook_events (
  event_id TEXT PRIMARY KEY CHECK (length(event_id) BETWEEN 6 AND 320),
  job_id TEXT NOT NULL UNIQUE CHECK (length(job_id) BETWEEN 1 AND 256),
  callback_status TEXT NOT NULL CHECK (length(callback_status) BETWEEN 1 AND 64),
  payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'queued', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_hume_webhook_events_retention_idx
  ON cf_hume_webhook_events(status, updated_at);
