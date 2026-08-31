-- D1 message projection for the explicit Cloudflare Assistants boundary.
--
-- The provider run table is deliberately not itself the chat history.  This
-- projection records the user turn and the eventual assistant turn so the
-- existing D1 chat-message reader can consume a file-backed run without
-- trusting provider state or writing Firestore.  Rows are keyed by the
-- uid-scoped run and carry the request snapshot needed for idempotent Queue
-- retries.
CREATE TABLE IF NOT EXISTS cf_chat_assistant_message_projections (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  run_id TEXT NOT NULL CHECK (length(run_id) BETWEEN 1 AND 128),
  session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 256),
  human_message_id TEXT NOT NULL CHECK (length(human_message_id) BETWEEN 1 AND 256),
  assistant_message_id TEXT NOT NULL CHECK (length(assistant_message_id) BETWEEN 1 AND 256),
  request_text TEXT NOT NULL CHECK (length(request_text) BETWEEN 1 AND 64000),
  file_ids_json TEXT NOT NULL CHECK (length(file_ids_json) BETWEEN 2 AND 8192),
  human_status TEXT NOT NULL CHECK (human_status IN ('pending', 'ready')) DEFAULT 'pending',
  assistant_status TEXT NOT NULL CHECK (assistant_status IN ('pending', 'ready')) DEFAULT 'pending',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  PRIMARY KEY (uid, run_id),
  UNIQUE (uid, human_message_id),
  UNIQUE (uid, assistant_message_id),
  FOREIGN KEY (uid, run_id) REFERENCES cf_chat_assistant_runs(uid, run_id) ON DELETE CASCADE,
  FOREIGN KEY (uid, session_id) REFERENCES cf_chat_sessions(uid, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_chat_assistant_message_projections_uid_session_idx
  ON cf_chat_assistant_message_projections(uid, session_id, created_at DESC, run_id DESC);

CREATE INDEX IF NOT EXISTS cf_chat_assistant_message_projections_pending_idx
  ON cf_chat_assistant_message_projections(uid, assistant_status, updated_at);

CREATE TRIGGER IF NOT EXISTS adf_i_chat_assistant_message_projections
BEFORE INSERT ON cf_chat_assistant_message_projections
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_assistant_message_projections
BEFORE UPDATE ON cf_chat_assistant_message_projections
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
