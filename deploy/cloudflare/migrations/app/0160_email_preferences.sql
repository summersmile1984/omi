CREATE TABLE cf_user_email_preferences (
  uid TEXT PRIMARY KEY NOT NULL,
  lifecycle_opted_out INTEGER NOT NULL DEFAULT 0 CHECK (lifecycle_opted_out IN (0, 1)),
  lifecycle_opted_out_at INTEGER
);

CREATE TRIGGER IF NOT EXISTS adf_i_user_email_preferences BEFORE INSERT ON cf_user_email_preferences
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_email_preferences BEFORE UPDATE ON cf_user_email_preferences
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
