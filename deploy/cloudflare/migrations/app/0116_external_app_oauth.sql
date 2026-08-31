-- Explicit Cloudflare authority for app-consent OAuth.  This is separate from
-- the auth database's dormant compatibility table: it binds a Better Auth uid
-- to the D1 app-catalog revision and double-submit CSRF hash used by the
-- namespaced staging route.  It never stores a Firebase token or raw secret.
CREATE TABLE IF NOT EXISTS cf_external_app_oauth_transactions (
  transaction_id TEXT PRIMARY KEY NOT NULL
    CHECK (length(transaction_id) BETWEEN 1 AND 128),
  app_id TEXT NOT NULL
    CHECK (length(app_id) BETWEEN 1 AND 256),
  uid TEXT NOT NULL
    CHECK (length(uid) BETWEEN 1 AND 256),
  state_hash TEXT NOT NULL UNIQUE
    CHECK (length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
  csrf_hash TEXT NOT NULL UNIQUE
    CHECK (length(csrf_hash) = 64 AND csrf_hash NOT GLOB '*[^0-9a-f]*'),
  client_state TEXT
    CHECK (client_state IS NULL OR length(client_state) <= 512),
  redirect_url TEXT NOT NULL
    CHECK (length(redirect_url) BETWEEN 9 AND 2048),
  app_catalog_revision INTEGER NOT NULL CHECK (app_catalog_revision >= 0),
  app_policy_json TEXT NOT NULL
    CHECK (json_valid(app_policy_json) AND json_type(app_policy_json) = 'object'
      AND length(app_policy_json) <= 65_536),
  setup_target_hash TEXT
    CHECK (setup_target_hash IS NULL OR
      (length(setup_target_hash) = 64 AND setup_target_hash NOT GLOB '*[^0-9a-f]*')),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'consumed', 'failed', 'expired')),
  expires_at INTEGER NOT NULL CHECK (expires_at > 0),
  created_at INTEGER NOT NULL CHECK (created_at > 0),
  consumed_at INTEGER
    CHECK (consumed_at IS NULL OR consumed_at >= created_at),
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_external_app_oauth_transactions_uid_idx
  ON cf_external_app_oauth_transactions(uid, status, expires_at, transaction_id);
CREATE INDEX IF NOT EXISTS cf_external_app_oauth_transactions_app_idx
  ON cf_external_app_oauth_transactions(app_id, status, expires_at, transaction_id);

CREATE TRIGGER IF NOT EXISTS adf_i_external_app_oauth_transactions
BEFORE INSERT ON cf_external_app_oauth_transactions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_external_app_oauth_transactions
BEFORE UPDATE ON cf_external_app_oauth_transactions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
