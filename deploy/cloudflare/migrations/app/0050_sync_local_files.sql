CREATE TABLE IF NOT EXISTS cf_sync_jobs (
  job_id TEXT PRIMARY KEY NOT NULL,
  uid TEXT NOT NULL,
  content_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN (
    'queued', 'running', 'completed', 'partial_failure', 'failed'
  )),
  lane TEXT NOT NULL CHECK (lane IN ('fresh', 'backfill')),
  capture_time_trust TEXT NOT NULL CHECK (capture_time_trust IN (
    'device_bound', 'legacy', 'untrusted'
  )),
  conversation_id TEXT,
  source TEXT NOT NULL DEFAULT 'omi',
  client_device_id TEXT,
  client_platform TEXT,
  recording_age_seconds INTEGER,
  total_files INTEGER NOT NULL CHECK (total_files > 0),
  total_segments INTEGER NOT NULL DEFAULT 0 CHECK (total_segments >= 0),
  processed_segments INTEGER NOT NULL DEFAULT 0 CHECK (processed_segments >= 0),
  successful_segments INTEGER NOT NULL DEFAULT 0 CHECK (successful_segments >= 0),
  failed_segments INTEGER NOT NULL DEFAULT 0 CHECK (failed_segments >= 0),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_until INTEGER,
  result_json TEXT,
  last_error TEXT,
  reason_code TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_sync_jobs_uid_updated_idx
  ON cf_sync_jobs(uid, updated_at DESC);

CREATE INDEX IF NOT EXISTS cf_sync_jobs_dispatch_idx
  ON cf_sync_jobs(status, updated_at, lane);

CREATE UNIQUE INDEX IF NOT EXISTS cf_sync_backfill_inflight_uid_idx
  ON cf_sync_jobs(uid)
  WHERE lane = 'backfill' AND status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS cf_sync_job_files (
  job_id TEXT NOT NULL,
  uid TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  filename TEXT NOT NULL,
  object_key TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL CHECK (size > 0),
  capture_at INTEGER NOT NULL,
  codec TEXT NOT NULL CHECK (codec IN ('opus', 'pcm16', 'pcm8')),
  sample_rate INTEGER NOT NULL CHECK (sample_rate IN (8000, 12000, 16000, 24000, 48000)),
  channels INTEGER NOT NULL CHECK (channels IN (1, 2)),
  frame_size INTEGER NOT NULL CHECK (frame_size > 0),
  status TEXT NOT NULL DEFAULT 'staged' CHECK (status IN ('staged', 'transcribed', 'failed')),
  transcription_json TEXT,
  speech_ms INTEGER NOT NULL DEFAULT 0 CHECK (speech_ms >= 0),
  duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
  detected_language TEXT,
  last_error TEXT,
  PRIMARY KEY (job_id, ordinal),
  UNIQUE (object_key),
  FOREIGN KEY (job_id) REFERENCES cf_sync_jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_sync_job_files_uid_idx
  ON cf_sync_job_files(uid, job_id, ordinal);

CREATE TABLE IF NOT EXISTS cf_sync_content_ledger (
  uid TEXT NOT NULL,
  content_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'retryable')),
  job_id TEXT NOT NULL,
  lane TEXT NOT NULL CHECK (lane IN ('fresh', 'backfill')),
  result_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  PRIMARY KEY (uid, content_id)
);

CREATE INDEX IF NOT EXISTS cf_sync_content_ledger_expiry_idx
  ON cf_sync_content_ledger(expires_at);

CREATE TABLE IF NOT EXISTS cf_sync_capture_claims (
  uid TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  PRIMARY KEY (uid, conversation_id)
);

CREATE INDEX IF NOT EXISTS cf_sync_capture_claims_expiry_idx
  ON cf_sync_capture_claims(expires_at);
