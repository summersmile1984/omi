ALTER TABLE cf_user_subscriptions ADD COLUMN stripe_status TEXT;
ALTER TABLE cf_user_subscriptions ADD COLUMN stripe_event_id TEXT;

CREATE TABLE IF NOT EXISTS cf_stripe_customers (
  uid TEXT PRIMARY KEY,
  stripe_customer_id TEXT NOT NULL UNIQUE
    CHECK (length(stripe_customer_id) BETWEEN 12 AND 160),
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cf_stripe_webhook_events (
  event_id TEXT PRIMARY KEY CHECK (length(event_id) BETWEEN 12 AND 160),
  event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 160),
  object_id TEXT NOT NULL CHECK (length(object_id) BETWEEN 3 AND 160),
  uid_hint TEXT,
  customer_id TEXT,
  subscription_id TEXT,
  payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processed', 'ignored', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at INTEGER NOT NULL,
  last_error TEXT,
  processed_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_stripe_webhook_events_retry_idx
  ON cf_stripe_webhook_events(status, next_attempt_at, attempts);

CREATE INDEX IF NOT EXISTS cf_stripe_webhook_events_uid_idx
  ON cf_stripe_webhook_events(uid_hint, created_at);

CREATE TRIGGER IF NOT EXISTS adf_i_stripe_customers
BEFORE INSERT ON cf_stripe_customers
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_stripe_customers
BEFORE UPDATE ON cf_stripe_customers
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_stripe_webhook_events
BEFORE INSERT ON cf_stripe_webhook_events
WHEN NEW.uid_hint IS NOT NULL
AND (
  EXISTS (
    SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid_hint
  )
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid_hint
  )
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_stripe_webhook_events
BEFORE UPDATE ON cf_stripe_webhook_events
WHEN EXISTS (
  SELECT 1
  FROM cf_account_deletion_intents
  WHERE uid IN (OLD.uid_hint, NEW.uid_hint)
)
OR EXISTS (
  SELECT 1
  FROM cf_account_deletion_tombstones
  WHERE uid IN (OLD.uid_hint, NEW.uid_hint)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
