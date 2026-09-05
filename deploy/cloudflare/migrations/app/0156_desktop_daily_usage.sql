-- Device retries report running totals; one uid/date/device row owns the maxima.
CREATE TABLE cf_desktop_daily_usage (
  uid TEXT NOT NULL,
  date TEXT NOT NULL,
  timezone TEXT NOT NULL,
  client_device_id TEXT NOT NULL,
  watching_seconds INTEGER NOT NULL CHECK (watching_seconds BETWEEN 0 AND 86400),
  listening_seconds INTEGER NOT NULL CHECK (listening_seconds BETWEEN 0 AND 86400),
  proactive_cards_shown INTEGER NOT NULL CHECK (proactive_cards_shown BETWEEN 0 AND 10000),
  proactive_cards_acted INTEGER NOT NULL CHECK (proactive_cards_acted BETWEEN 0 AND 10000),
  ptt_turns INTEGER NOT NULL CHECK (ptt_turns BETWEEN 0 AND 10000),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, date, client_device_id)
);

CREATE TRIGGER IF NOT EXISTS adf_i_desktop_daily_usage BEFORE INSERT ON cf_desktop_daily_usage
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_desktop_daily_usage BEFORE UPDATE ON cf_desktop_daily_usage
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
