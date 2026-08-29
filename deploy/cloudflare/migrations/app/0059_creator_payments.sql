CREATE TABLE IF NOT EXISTS cf_creator_payment_profiles (
  uid TEXT PRIMARY KEY NOT NULL,
  stripe_account_id TEXT UNIQUE,
  stripe_charges_enabled INTEGER NOT NULL DEFAULT 0 CHECK (stripe_charges_enabled IN (0, 1)),
  stripe_payouts_enabled INTEGER NOT NULL DEFAULT 0 CHECK (stripe_payouts_enabled IN (0, 1)),
  stripe_details_submitted INTEGER NOT NULL DEFAULT 0 CHECK (stripe_details_submitted IN (0, 1)),
  stripe_onboarding_complete INTEGER NOT NULL DEFAULT 0 CHECK (stripe_onboarding_complete IN (0, 1)),
  paypal_email TEXT,
  paypalme_url TEXT,
  default_payment_method TEXT CHECK (default_payment_method IS NULL OR default_payment_method IN ('stripe', 'paypal')),
  stripe_event_id TEXT,
  updated_at INTEGER NOT NULL,
  CHECK (
    stripe_account_id IS NULL
    OR (stripe_account_id LIKE 'acct_%' AND length(stripe_account_id) BETWEEN 12 AND 160)
  ),
  CHECK (paypal_email IS NULL OR length(paypal_email) <= 254),
  CHECK (paypalme_url IS NULL OR length(paypalme_url) <= 2048),
  CHECK (stripe_event_id IS NULL OR length(stripe_event_id) BETWEEN 8 AND 160)
);

CREATE INDEX IF NOT EXISTS cf_creator_payment_profiles_stripe_idx
  ON cf_creator_payment_profiles(stripe_account_id);

CREATE TABLE IF NOT EXISTS cf_stripe_connect_events (
  event_id TEXT PRIMARY KEY NOT NULL,
  event_type TEXT NOT NULL,
  account_id TEXT NOT NULL,
  uid_hint TEXT,
  payload_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('processed', 'ignored')),
  processed_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  CHECK (length(event_id) BETWEEN 8 AND 160),
  CHECK (account_id LIKE 'acct_%' AND length(account_id) BETWEEN 12 AND 160),
  CHECK (length(payload_sha256) = 64)
);

CREATE INDEX IF NOT EXISTS cf_stripe_connect_events_uid_idx
  ON cf_stripe_connect_events(uid_hint, created_at);

CREATE TRIGGER IF NOT EXISTS adf_i_creator_payment_profiles
BEFORE INSERT ON cf_creator_payment_profiles
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_creator_payment_profiles
BEFORE UPDATE ON cf_creator_payment_profiles
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_stripe_connect_events
BEFORE INSERT ON cf_stripe_connect_events
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid_hint
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid_hint
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_stripe_connect_events
BEFORE UPDATE ON cf_stripe_connect_events
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid_hint, NEW.uid_hint)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid_hint, NEW.uid_hint)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
