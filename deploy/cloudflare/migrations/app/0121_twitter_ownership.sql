-- Dormant Cloudflare seam for the legacy Twitter ownership proof.
--
-- This stores only a bounded, replayable verification transaction and an
-- owner-scoped handle claim.  It deliberately does not project Firebase
-- provider data or mutate the Persona catalog; the exact legacy route stays
-- fail-closed until those authorities have a production bridge.
CREATE TABLE IF NOT EXISTS cf_twitter_ownership_transactions (
  uid TEXT NOT NULL,
  transaction_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  username TEXT NOT NULL CHECK (length(username) BETWEEN 1 AND 256),
  handle TEXT NOT NULL CHECK (length(handle) BETWEEN 1 AND 128),
  persona_id TEXT CHECK (persona_id IS NULL OR length(persona_id) BETWEEN 1 AND 256),
  provider TEXT NOT NULL CHECK (provider = 'rapidapi-timeline'),
  provider_request_fingerprint TEXT NOT NULL CHECK (length(provider_request_fingerprint) = 64),
  provider_response_fingerprint TEXT CHECK (provider_response_fingerprint IS NULL OR length(provider_response_fingerprint) = 64),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
  status TEXT NOT NULL CHECK (status IN ('pending', 'verified', 'unverified', 'conflict', 'failed')),
  verified INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
  tweet_id TEXT CHECK (tweet_id IS NULL OR length(tweet_id) BETWEEN 1 AND 256),
  result_json TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 512),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, transaction_id),
  UNIQUE (uid, request_fingerprint)
);

CREATE INDEX IF NOT EXISTS cf_twitter_ownership_transactions_handle_idx
  ON cf_twitter_ownership_transactions(handle, updated_at DESC);

CREATE TABLE IF NOT EXISTS cf_twitter_ownership_claims (
  handle TEXT PRIMARY KEY NOT NULL CHECK (length(handle) BETWEEN 1 AND 128),
  uid TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  transaction_id TEXT NOT NULL,
  username TEXT NOT NULL CHECK (length(username) BETWEEN 1 AND 256),
  tweet_id TEXT NOT NULL CHECK (length(tweet_id) BETWEEN 1 AND 256),
  claimed_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (uid, handle)
);

CREATE INDEX IF NOT EXISTS cf_twitter_ownership_claims_uid_idx
  ON cf_twitter_ownership_claims(uid, updated_at DESC);

-- The route-level fence protects normal requests; these triggers close the
-- race with a request admitted immediately before account deletion. DELETE is
-- intentionally left available to the account-deletion owner for purge.
CREATE TRIGGER IF NOT EXISTS adf_i_twitter_ownership_transactions
BEFORE INSERT ON cf_twitter_ownership_transactions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones
  WHERE uid = NEW.uid AND expires_at > unixepoch()
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_twitter_ownership_transactions
BEFORE UPDATE ON cf_twitter_ownership_transactions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones
  WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_twitter_ownership_claims
BEFORE INSERT ON cf_twitter_ownership_claims
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones
  WHERE uid = NEW.uid AND expires_at > unixepoch()
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_twitter_ownership_claims
BEFORE UPDATE ON cf_twitter_ownership_claims
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones
  WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
