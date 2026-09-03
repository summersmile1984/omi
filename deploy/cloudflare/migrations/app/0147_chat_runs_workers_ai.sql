-- Allow the same durable chat-run projection to execute on Workers AI.
--
-- OpenAI Assistants rows remain valid for explicitly enabled compatibility
-- deployments. New Cloudflare clients use the provider-neutral run state and
-- never require an external provider id.
PRAGMA foreign_keys = OFF;

DROP TRIGGER IF EXISTS adf_i_chat_assistant_runs;
DROP TRIGGER IF EXISTS adf_u_chat_assistant_runs;

CREATE TABLE cf_chat_assistant_runs_workers_ai (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  run_id TEXT NOT NULL CHECK (length(run_id) BETWEEN 1 AND 128),
  session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 256),
  provider TEXT NOT NULL CHECK (provider IN ('openai-assistants', 'cloudflare-workers-ai')),
  idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 300),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
  provider_message_id TEXT CHECK (provider_message_id IS NULL OR length(provider_message_id) BETWEEN 1 AND 256),
  provider_run_id TEXT CHECK (provider_run_id IS NULL OR length(provider_run_id) BETWEEN 1 AND 256),
  status TEXT NOT NULL CHECK (status IN ('staging', 'queued', 'in_progress', 'completed', 'failed', 'cancelled')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_token TEXT,
  lease_until INTEGER,
  next_attempt_at INTEGER NOT NULL,
  result_json TEXT,
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, run_id),
  UNIQUE (uid, idempotency_key),
  UNIQUE (provider_message_id),
  UNIQUE (provider_run_id),
  FOREIGN KEY (uid, session_id) REFERENCES cf_chat_sessions(uid, id) ON DELETE CASCADE
);

INSERT INTO cf_chat_assistant_runs_workers_ai (
  uid, run_id, session_id, provider, idempotency_key, request_fingerprint,
  provider_message_id, provider_run_id, status, attempts, lease_token,
  lease_until, next_attempt_at, result_json, last_error, created_at, updated_at
)
SELECT uid, run_id, session_id, provider, idempotency_key, request_fingerprint,
  provider_message_id, provider_run_id, status, attempts, lease_token,
  lease_until, next_attempt_at, result_json, last_error, created_at, updated_at
FROM cf_chat_assistant_runs;

DROP TABLE cf_chat_assistant_runs;
ALTER TABLE cf_chat_assistant_runs_workers_ai RENAME TO cf_chat_assistant_runs;

CREATE INDEX cf_chat_assistant_runs_uid_session_idx
  ON cf_chat_assistant_runs(uid, session_id, created_at DESC, run_id DESC);
CREATE INDEX cf_chat_assistant_runs_dispatch_idx
  ON cf_chat_assistant_runs(status, next_attempt_at, lease_until);

CREATE TRIGGER adf_i_chat_assistant_runs
BEFORE INSERT ON cf_chat_assistant_runs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER adf_u_chat_assistant_runs
BEFORE UPDATE ON cf_chat_assistant_runs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

PRAGMA foreign_keys = ON;
