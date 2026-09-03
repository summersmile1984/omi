CREATE TABLE IF NOT EXISTS cf_chat_first_deferrals (
  uid TEXT NOT NULL,
  deferral_id TEXT NOT NULL,
  continuity_key TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('task', 'goal', 'capture', 'cold_start')),
  subject_id TEXT NOT NULL,
  question_json TEXT NOT NULL CHECK (length(question_json) BETWEEN 2 AND 32000),
  created_at INTEGER NOT NULL,
  due_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'released')),
  released_intent_id TEXT,
  PRIMARY KEY (uid, deferral_id),
  UNIQUE (uid, account_generation, continuity_key)
);

CREATE INDEX IF NOT EXISTS cf_chat_first_deferrals_due_idx
  ON cf_chat_first_deferrals(uid, account_generation, state, due_at, deferral_id);

CREATE INDEX IF NOT EXISTS cf_chat_first_deferrals_subject_idx
  ON cf_chat_first_deferrals(uid, account_generation, state, subject_kind, subject_id);

CREATE TRIGGER IF NOT EXISTS adf_i_chat_first_deferrals
BEFORE INSERT ON cf_chat_first_deferrals
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_first_deferrals
BEFORE UPDATE ON cf_chat_first_deferrals
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
