-- Per-user daily proactive notification budget (legacy MAX_DAILY_NOTIFICATIONS,
-- default 9/day across every proactive source, #4859). One row owns a uid+UTC
-- day; the counter advances through a bounded conditional upsert so all of a
-- user's integration apps share one aggregate cap instead of N independent
-- hourly budgets. Old days are pruned by request traffic, mirroring
-- cf_integration_hourly_usage; account deletion purges the uid exhaustively.
CREATE TABLE IF NOT EXISTS cf_user_notification_daily_usage (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  day TEXT NOT NULL CHECK (
    day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
  ),
  notification_count INTEGER NOT NULL CHECK (
    notification_count BETWEEN 1 AND 1000
  ),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, day)
);

CREATE INDEX IF NOT EXISTS cf_user_notification_daily_usage_expiry_idx
  ON cf_user_notification_daily_usage(day, uid);

CREATE TRIGGER IF NOT EXISTS adf_i_user_notification_daily_usage
BEFORE INSERT ON cf_user_notification_daily_usage
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_notification_daily_usage
BEFORE UPDATE ON cf_user_notification_daily_usage
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
