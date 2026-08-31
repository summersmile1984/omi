-- D1 projection linking the Cloudflare chat-file authority to a chat session.
--
-- This table deliberately stores the canonical file id, not a caller supplied
-- OpenAI id.  The reader always joins cf_chat_files and admits only ready rows;
-- provider ids remain an implementation detail of the future chat provider
-- adapter.  The explicit foreign keys also make session/file deletion remove
-- stale links when D1 foreign-key enforcement is enabled.
CREATE TABLE IF NOT EXISTS cf_chat_session_files (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 256),
  file_id TEXT NOT NULL CHECK (length(file_id) BETWEEN 1 AND 128),
  source_message_id TEXT CHECK (source_message_id IS NULL OR length(source_message_id) BETWEEN 1 AND 256),
  attached_at INTEGER NOT NULL,
  PRIMARY KEY (uid, session_id, file_id),
  FOREIGN KEY (uid, session_id) REFERENCES cf_chat_sessions(uid, id) ON DELETE CASCADE,
  FOREIGN KEY (uid, file_id) REFERENCES cf_chat_files(uid, file_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_chat_session_files_uid_file_idx
  ON cf_chat_session_files(uid, file_id, attached_at DESC);

CREATE INDEX IF NOT EXISTS cf_chat_session_files_uid_session_idx
  ON cf_chat_session_files(uid, session_id, attached_at ASC, file_id ASC);

CREATE TRIGGER IF NOT EXISTS adf_i_chat_session_files
BEFORE INSERT ON cf_chat_session_files
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_session_files
BEFORE UPDATE ON cf_chat_session_files
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
