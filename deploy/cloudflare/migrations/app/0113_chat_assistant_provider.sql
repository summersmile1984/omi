-- OpenAI Assistants continuity projection for the explicit Cloudflare chat
-- adapter.  This is intentionally separate from cf_chat_session_files:
-- file/session ownership is D1, while provider thread/run ids are external
-- state that must be replayable and deletable by uid.
CREATE TABLE IF NOT EXISTS cf_chat_assistant_sessions (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 256),
  provider TEXT NOT NULL CHECK (provider = 'openai-assistants'),
  thread_id TEXT NOT NULL CHECK (length(thread_id) BETWEEN 1 AND 256),
  assistant_id TEXT NOT NULL CHECK (length(assistant_id) BETWEEN 1 AND 256),
  status TEXT NOT NULL CHECK (status IN ('active', 'failed', 'deleted')),
  generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  PRIMARY KEY (uid, session_id),
  UNIQUE (provider, thread_id),
  FOREIGN KEY (uid, session_id) REFERENCES cf_chat_sessions(uid, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_chat_assistant_sessions_uid_updated_idx
  ON cf_chat_assistant_sessions(uid, updated_at DESC, session_id ASC);

CREATE TABLE IF NOT EXISTS cf_chat_assistant_runs (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  run_id TEXT NOT NULL CHECK (length(run_id) BETWEEN 1 AND 128),
  session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 256),
  provider TEXT NOT NULL CHECK (provider = 'openai-assistants'),
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

CREATE INDEX IF NOT EXISTS cf_chat_assistant_runs_uid_session_idx
  ON cf_chat_assistant_runs(uid, session_id, created_at DESC, run_id DESC);

CREATE INDEX IF NOT EXISTS cf_chat_assistant_runs_dispatch_idx
  ON cf_chat_assistant_runs(status, next_attempt_at, lease_until);

CREATE TRIGGER IF NOT EXISTS adf_i_chat_assistant_sessions
BEFORE INSERT ON cf_chat_assistant_sessions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_assistant_sessions
BEFORE UPDATE ON cf_chat_assistant_sessions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_chat_assistant_runs
BEFORE INSERT ON cf_chat_assistant_runs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_assistant_runs
BEFORE UPDATE ON cf_chat_assistant_runs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
