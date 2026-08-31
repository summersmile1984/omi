-- Durable Cloudflare owner for multi-conversation merge.
-- Source conversations are deliberately not foreign-keyed: a successful merge
-- removes them in the same D1 transaction that commits the replacement row.
ALTER TABLE cf_conversations ADD COLUMN merge_job_id TEXT;
ALTER TABLE cf_conversations ADD COLUMN merge_revision INTEGER;

CREATE TABLE IF NOT EXISTS cf_conversation_merge_jobs (
  uid TEXT NOT NULL,
  job_id TEXT NOT NULL PRIMARY KEY,
  source_conversation_ids_json TEXT NOT NULL,
  result_conversation_id TEXT NOT NULL,
  merge_revision INTEGER NOT NULL,
  reprocess INTEGER NOT NULL DEFAULT 1 CHECK (reprocess IN (0, 1)),
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_until INTEGER,
  next_attempt_at INTEGER NOT NULL,
  last_error TEXT,
  result_json TEXT,
  request_fingerprint TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (uid, request_fingerprint),
  CHECK (length(uid) BETWEEN 1 AND 256),
  CHECK (length(job_id) BETWEEN 1 AND 128),
  CHECK (length(source_conversation_ids_json) BETWEEN 2 AND 128000),
  CHECK (length(result_conversation_id) BETWEEN 1 AND 256)
);

CREATE INDEX IF NOT EXISTS cf_conversation_merge_jobs_ready_idx
  ON cf_conversation_merge_jobs(status, next_attempt_at, lease_until, updated_at);

CREATE INDEX IF NOT EXISTS cf_conversation_merge_jobs_uid_idx
  ON cf_conversation_merge_jobs(uid, created_at DESC, job_id);

CREATE TRIGGER IF NOT EXISTS adf_i_conversation_merge_jobs
BEFORE INSERT ON cf_conversation_merge_jobs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_conversation_merge_jobs
BEFORE UPDATE ON cf_conversation_merge_jobs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
