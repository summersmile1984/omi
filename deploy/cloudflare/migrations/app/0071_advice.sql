CREATE TABLE IF NOT EXISTS cf_advice (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  content TEXT NOT NULL,
  category TEXT NOT NULL,
  reasoning TEXT,
  source_app TEXT,
  confidence REAL NOT NULL DEFAULT 0.5,
  context_summary TEXT,
  current_activity TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
  is_dismissed INTEGER NOT NULL DEFAULT 0 CHECK (is_dismissed IN (0, 1)),
  PRIMARY KEY (uid, id),
  CHECK (length(content) BETWEEN 1 AND 10000),
  CHECK (length(category) BETWEEN 1 AND 100),
  CHECK (reasoning IS NULL OR length(reasoning) <= 5000),
  CHECK (source_app IS NULL OR length(source_app) <= 200),
  CHECK (confidence BETWEEN 0.0 AND 1.0),
  CHECK (context_summary IS NULL OR length(context_summary) <= 5000),
  CHECK (current_activity IS NULL OR length(current_activity) <= 500)
);

CREATE INDEX IF NOT EXISTS cf_advice_uid_created_idx
  ON cf_advice(uid, created_at DESC);

CREATE INDEX IF NOT EXISTS cf_advice_uid_dismissed_created_idx
  ON cf_advice(uid, is_dismissed, created_at DESC);

CREATE INDEX IF NOT EXISTS cf_advice_uid_category_dismissed_created_idx
  ON cf_advice(uid, category, is_dismissed, created_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_advice
BEFORE INSERT ON cf_advice
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_advice
BEFORE UPDATE ON cf_advice
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;
