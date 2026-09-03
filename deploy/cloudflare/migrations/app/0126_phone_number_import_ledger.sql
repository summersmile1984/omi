-- Dry-run/reconciliation ledger for historical verified Phone/Twilio numbers.
--
-- This table is not phone authority and does not contact Twilio.  It records a
-- bounded, idempotent plan that an operator may review before a separate
-- executor promotes a row into cf_phone_numbers.  The ciphertext is already
-- in the Cloudflare AES-GCM format; this migration never stores or accepts a
-- plaintext E.164 value.
CREATE TABLE IF NOT EXISTS cf_phone_number_import_ledger (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  import_id TEXT NOT NULL CHECK (length(import_id) = 64 AND import_id NOT GLOB '*[^0-9a-f]*'),
  source_record_id TEXT NOT NULL CHECK (length(source_record_id) BETWEEN 1 AND 128),
  phone_number_id TEXT NOT NULL CHECK (length(phone_number_id) BETWEEN 1 AND 128),
  phone_number_hash TEXT NOT NULL CHECK (length(phone_number_hash) = 64 AND phone_number_hash NOT GLOB '*[^0-9a-f]*'),
  phone_number_ciphertext TEXT NOT NULL CHECK (length(phone_number_ciphertext) BETWEEN 29 AND 4096 AND phone_number_ciphertext NOT GLOB '*+*'),
  proof_sha256 TEXT NOT NULL CHECK (length(proof_sha256) = 64 AND proof_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
  source_export_sha256 TEXT NOT NULL CHECK (length(source_export_sha256) = 64 AND source_export_sha256 NOT GLOB '*[^0-9a-f]*'),
  twilio_sid TEXT,
  friendly_name TEXT,
  verified_at INTEGER NOT NULL CHECK (verified_at > 0),
  is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  action TEXT NOT NULL CHECK (action IN ('stage', 'blocked')),
  status TEXT NOT NULL CHECK (status IN ('planned', 'blocked', 'applied', 'failed')),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  created_at INTEGER NOT NULL CHECK (created_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  PRIMARY KEY (uid, import_id),
  UNIQUE (uid, source_record_id),
  UNIQUE (phone_number_hash),
  UNIQUE (twilio_sid),
  CHECK (action = 'blocked' OR (status = 'planned' AND last_error IS NULL))
);

CREATE INDEX IF NOT EXISTS cf_phone_number_import_ledger_uid_status_idx
  ON cf_phone_number_import_ledger(uid, status, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_phone_number_import_ledger
BEFORE INSERT ON cf_phone_number_import_ledger
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_phone_number_import_ledger
BEFORE UPDATE ON cf_phone_number_import_ledger
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
