CREATE TABLE IF NOT EXISTS cf_user_app_preferences (
  uid TEXT PRIMARY KEY NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  preferred_app_id TEXT NOT NULL CHECK (length(preferred_app_id) BETWEEN 1 AND 256),
  updated_at INTEGER NOT NULL,
  FOREIGN KEY (preferred_app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_user_app_preferences_app_idx
  ON cf_user_app_preferences(preferred_app_id, uid);

CREATE TRIGGER IF NOT EXISTS adf_i_user_app_preferences
BEFORE INSERT ON cf_user_app_preferences
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_app_preferences
BEFORE UPDATE ON cf_user_app_preferences
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
