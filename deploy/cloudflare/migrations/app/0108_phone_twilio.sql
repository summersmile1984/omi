-- Cloudflare authority for caller-ID verification and Voice SDK calls.
-- Phone numbers are encrypted by the Jobs worker; the SHA-256 hash is the
-- only queryable representation and is unique across accounts, matching
-- Twilio's globally-owned outgoing caller-ID resource.
CREATE TABLE IF NOT EXISTS cf_phone_numbers (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  phone_number_hash TEXT NOT NULL CHECK (length(phone_number_hash) = 64),
  phone_number_ciphertext TEXT NOT NULL CHECK (length(phone_number_ciphertext) <= 4096),
  friendly_name TEXT,
  twilio_sid TEXT UNIQUE,
  verified_at INTEGER NOT NULL CHECK (verified_at >= 0),
  is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
  account_generation INTEGER NOT NULL DEFAULT 0 CHECK (account_generation >= 0),
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
  PRIMARY KEY (uid, id),
  UNIQUE (uid, phone_number_hash),
  UNIQUE (phone_number_hash)
);

CREATE INDEX IF NOT EXISTS cf_phone_numbers_uid_primary_idx
  ON cf_phone_numbers(uid, is_primary DESC, verified_at ASC);

CREATE TABLE IF NOT EXISTS cf_phone_verifications (
  verification_id TEXT PRIMARY KEY,
  uid TEXT NOT NULL,
  phone_number_hash TEXT NOT NULL UNIQUE,
  phone_number_ciphertext TEXT NOT NULL CHECK (length(phone_number_ciphertext) <= 4096),
  twilio_validation_sid TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending', 'failed', 'expired')),
  account_generation INTEGER NOT NULL DEFAULT 0 CHECK (account_generation >= 0),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  expires_at INTEGER NOT NULL CHECK (expires_at > 0),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 256),
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= 0)
);

CREATE INDEX IF NOT EXISTS cf_phone_verifications_uid_idx
  ON cf_phone_verifications(uid, status, expires_at);

CREATE TABLE IF NOT EXISTS cf_phone_call_usage (
  uid TEXT NOT NULL,
  period_id TEXT NOT NULL CHECK (period_id GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  calls INTEGER NOT NULL DEFAULT 0 CHECK (calls >= 0),
  account_generation INTEGER NOT NULL DEFAULT 0 CHECK (account_generation >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
  PRIMARY KEY (uid, period_id)
);

-- Twilio may retry a signed TwiML webhook.  Keep only a bounded hash of the
-- provider call identifier so a retry cannot reserve another free-tier slot.
-- `pending` is short-lived: a crashed request can be retried after its lease
-- expires, while a completed call remains idempotent for its account
-- generation.
CREATE TABLE IF NOT EXISTS cf_phone_call_attempts (
  uid TEXT NOT NULL,
  call_id_hash TEXT NOT NULL CHECK (length(call_id_hash) = 64),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  status TEXT NOT NULL CHECK (status IN ('pending', 'completed')),
  expires_at INTEGER NOT NULL CHECK (expires_at > 0),
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
  PRIMARY KEY (uid, call_id_hash)
);

CREATE INDEX IF NOT EXISTS cf_phone_call_attempts_expiry_idx
  ON cf_phone_call_attempts(expires_at);

CREATE TABLE IF NOT EXISTS cf_phone_call_policy (
  id TEXT PRIMARY KEY CHECK (id = 'default'),
  free_monthly_limit INTEGER CHECK (free_monthly_limit IS NULL OR free_monthly_limit >= 0),
  free_max_duration_seconds INTEGER CHECK (free_max_duration_seconds IS NULL OR free_max_duration_seconds >= 0),
  free_allowed_countries_json TEXT NOT NULL DEFAULT '[]',
  paid_monthly_limit INTEGER CHECK (paid_monthly_limit IS NULL OR paid_monthly_limit >= 0),
  paid_max_duration_seconds INTEGER CHECK (paid_max_duration_seconds IS NULL OR paid_max_duration_seconds >= 0),
  paid_allowed_countries_json TEXT NOT NULL DEFAULT '[]',
  updated_at INTEGER NOT NULL CHECK (updated_at >= 0)
);

-- Preserve the legacy default: phone calling is paid-only until operators
-- explicitly enable a free-tier monthly allowance in D1.
INSERT OR IGNORE INTO cf_phone_call_policy (
  id, free_monthly_limit, free_max_duration_seconds,
  free_allowed_countries_json, paid_monthly_limit,
  paid_max_duration_seconds, paid_allowed_countries_json, updated_at
) VALUES ('default', 0, 300, '[]', NULL, NULL, '[]', strftime('%s', 'now'));

CREATE TRIGGER IF NOT EXISTS adf_i_phone_numbers
BEFORE INSERT ON cf_phone_numbers
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_phone_numbers
BEFORE UPDATE ON cf_phone_numbers
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_phone_verifications
BEFORE INSERT ON cf_phone_verifications
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_phone_verifications
BEFORE UPDATE ON cf_phone_verifications
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_phone_call_usage
BEFORE INSERT ON cf_phone_call_usage
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_phone_call_usage
BEFORE UPDATE ON cf_phone_call_usage
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_phone_call_attempts
BEFORE INSERT ON cf_phone_call_attempts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_phone_call_attempts
BEFORE UPDATE ON cf_phone_call_attempts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
