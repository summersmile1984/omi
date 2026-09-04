-- One immutable user/session -> conversation binding. It intentionally survives
-- conversation deletion so reconnect cannot recreate a deliberately deleted ID.
CREATE TABLE cf_live_recording_sessions (
  uid TEXT NOT NULL,
  recording_session_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  initial_token TEXT NOT NULL,
  owner_token TEXT NOT NULL,
  lifecycle_sequence INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, recording_session_id),
  UNIQUE (uid, conversation_id)
);

CREATE TRIGGER IF NOT EXISTS adf_i_live_recording_sessions
BEFORE INSERT ON cf_live_recording_sessions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_live_recording_sessions
BEFORE UPDATE ON cf_live_recording_sessions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
