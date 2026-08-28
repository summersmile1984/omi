CREATE TABLE IF NOT EXISTS cf_user_feedback (
  uid TEXT NOT NULL,
  feedback_type TEXT NOT NULL CHECK (feedback_type IN ('memory_summary', 'chat_message')),
  subject_id TEXT NOT NULL,
  value INTEGER NOT NULL CHECK (value IN (-1, 0, 1)),
  reason TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, feedback_type, subject_id)
);

CREATE INDEX IF NOT EXISTS cf_user_feedback_type_updated_idx
  ON cf_user_feedback(feedback_type, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_user_feedback
BEFORE INSERT ON cf_user_feedback
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_feedback
BEFORE UPDATE ON cf_user_feedback
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
