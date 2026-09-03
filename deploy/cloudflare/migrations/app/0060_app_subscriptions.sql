CREATE TABLE IF NOT EXISTS cf_app_subscriptions (
  uid TEXT NOT NULL,
  app_id TEXT NOT NULL,
  stripe_customer_id TEXT NOT NULL
    CHECK (stripe_customer_id LIKE 'cus_%' AND length(stripe_customer_id) BETWEEN 12 AND 160),
  stripe_subscription_id TEXT NOT NULL UNIQUE
    CHECK (stripe_subscription_id LIKE 'sub_%' AND length(stripe_subscription_id) BETWEEN 12 AND 160),
  status TEXT NOT NULL CHECK (length(status) BETWEEN 1 AND 80),
  current_period_start INTEGER
    CHECK (current_period_start IS NULL OR current_period_start >= 0),
  current_period_end INTEGER
    CHECK (current_period_end IS NULL OR current_period_end >= 0),
  cancel_at_period_end INTEGER NOT NULL DEFAULT 0
    CHECK (cancel_at_period_end IN (0, 1)),
  price_id TEXT CHECK (price_id IS NULL OR length(price_id) BETWEEN 8 AND 160),
  stripe_event_id TEXT
    CHECK (stripe_event_id IS NULL OR length(stripe_event_id) BETWEEN 12 AND 160),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, app_id),
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_app_subscriptions_uid_status_idx
  ON cf_app_subscriptions(uid, status, current_period_end);

CREATE INDEX IF NOT EXISTS cf_app_subscriptions_app_status_idx
  ON cf_app_subscriptions(app_id, status, current_period_end);

CREATE TRIGGER IF NOT EXISTS adf_i_app_subscriptions
BEFORE INSERT ON cf_app_subscriptions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_app_subscriptions
BEFORE UPDATE ON cf_app_subscriptions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
