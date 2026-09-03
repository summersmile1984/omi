-- Cloudflare staging boundary for dense legacy audio rebuilds.
--
-- This contract intentionally admits only R2-backed WAV output. The legacy
-- /v2/audio-merge-jobs/run path still owns Cloud Tasks/OIDC + MP3 semantics;
-- this table must not be used to claim parity with that path.
CREATE TABLE IF NOT EXISTS cf_audio_merge_jobs (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  job_id TEXT NOT NULL CHECK (length(job_id) BETWEEN 1 AND 128),
  conversation_id TEXT NOT NULL CHECK (length(conversation_id) BETWEEN 1 AND 128),
  audio_file_id TEXT NOT NULL CHECK (audio_file_id = 'conversation'),
  source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64),
  source_prefix TEXT NOT NULL CHECK (length(source_prefix) BETWEEN 1 AND 512),
  artifact_key TEXT NOT NULL CHECK (length(artifact_key) BETWEEN 1 AND 512),
  output_format TEXT NOT NULL CHECK (output_format = 'wav'),
  account_generation INTEGER NOT NULL DEFAULT 0 CHECK (account_generation >= 0),
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_token TEXT,
  lease_until INTEGER,
  next_attempt_at INTEGER NOT NULL,
  result_json TEXT,
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, job_id),
  UNIQUE (uid, request_fingerprint)
);

CREATE INDEX IF NOT EXISTS cf_audio_merge_jobs_ready_idx
  ON cf_audio_merge_jobs(status, next_attempt_at, lease_until, updated_at);

CREATE INDEX IF NOT EXISTS cf_audio_merge_jobs_uid_idx
  ON cf_audio_merge_jobs(uid, created_at DESC, job_id DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_audio_merge_jobs
BEFORE INSERT ON cf_audio_merge_jobs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_audio_merge_jobs
BEFORE UPDATE ON cf_audio_merge_jobs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
