CREATE TABLE IF NOT EXISTS cf_x_connections (
  uid TEXT PRIMARY KEY,
  connected INTEGER NOT NULL DEFAULT 0 CHECK (connected IN (0, 1)),
  access_token_enc TEXT,
  refresh_token_enc TEXT,
  token_expires_at INTEGER,
  scope TEXT,
  handle TEXT,
  x_user_id TEXT,
  syncing INTEGER NOT NULL DEFAULT 0 CHECK (syncing IN (0, 1)),
  sync_token TEXT,
  sync_started_at INTEGER,
  last_synced_at INTEGER,
  last_sync_source TEXT CHECK (
    last_sync_source IS NULL OR last_sync_source IN ('oauth', 'rapidapi')
  ),
  post_count INTEGER NOT NULL DEFAULT 0 CHECK (post_count >= 0),
  memory_count INTEGER NOT NULL DEFAULT 0 CHECK (memory_count >= 0),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  CHECK (length(uid) BETWEEN 1 AND 256),
  CHECK (connected = 0 OR access_token_enc IS NOT NULL),
  CHECK (syncing = 0 OR (sync_token IS NOT NULL AND length(sync_token) BETWEEN 1 AND 100)),
  CHECK (handle IS NULL OR length(handle) BETWEEN 1 AND 100),
  CHECK (x_user_id IS NULL OR length(x_user_id) BETWEEN 1 AND 100),
  CHECK (scope IS NULL OR length(scope) <= 2000)
);

CREATE INDEX IF NOT EXISTS cf_x_connections_sync_idx
  ON cf_x_connections(connected, syncing, last_synced_at, updated_at);

CREATE TABLE IF NOT EXISTS cf_x_oauth_states (
  state_hash TEXT PRIMARY KEY CHECK (
    length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
  ),
  uid TEXT NOT NULL,
  verifier_enc TEXT NOT NULL,
  success_redirect_url TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  CHECK (length(uid) BETWEEN 1 AND 256),
  CHECK (length(success_redirect_url) BETWEEN 1 AND 1024)
);

CREATE INDEX IF NOT EXISTS cf_x_oauth_states_expiry_idx
  ON cf_x_oauth_states(expires_at);

CREATE TRIGGER IF NOT EXISTS adf_i_x_connections
BEFORE INSERT ON cf_x_connections
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_x_connections
BEFORE UPDATE ON cf_x_connections
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_x_oauth_states
BEFORE INSERT ON cf_x_oauth_states
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_x_oauth_states
BEFORE UPDATE ON cf_x_oauth_states
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;
