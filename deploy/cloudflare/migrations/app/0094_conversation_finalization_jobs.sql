-- Durable Cloudflare owner for explicit conversation finalization.
-- The transcript remains in cf_conversations; this table owns admission,
-- retry/lease state, and the terminal result of the asynchronous enrichment.
ALTER TABLE cf_conversations ADD COLUMN finalization_job_id TEXT;
ALTER TABLE cf_conversations ADD COLUMN finalization_revision INTEGER;
ALTER TABLE cf_conversations ADD COLUMN finalization_status TEXT
  CHECK (finalization_status IS NULL OR finalization_status IN ('queued', 'running', 'completed', 'failed'));

CREATE TABLE IF NOT EXISTS cf_conversation_finalization_jobs (
  uid TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  job_id TEXT NOT NULL PRIMARY KEY,
  finalization_revision INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_until INTEGER,
  next_attempt_at INTEGER NOT NULL,
  last_error TEXT,
  result_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (uid, conversation_id, finalization_revision),
  FOREIGN KEY (uid, conversation_id) REFERENCES cf_conversations(uid, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_conversation_finalization_jobs_ready_idx
  ON cf_conversation_finalization_jobs(status, next_attempt_at, lease_until, updated_at);

CREATE INDEX IF NOT EXISTS cf_conversation_finalization_jobs_uid_idx
  ON cf_conversation_finalization_jobs(uid, conversation_id, finalization_revision DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_conversation_finalization_jobs
BEFORE INSERT ON cf_conversation_finalization_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_conversation_finalization_jobs
BEFORE UPDATE ON cf_conversation_finalization_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
