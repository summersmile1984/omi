CREATE TABLE IF NOT EXISTS cf_google_calendar_integrations (
  uid TEXT PRIMARY KEY,
  connected INTEGER NOT NULL DEFAULT 0 CHECK (connected IN (0, 1)),
  access_token_enc TEXT,
  refresh_token_enc TEXT,
  token_expires_at INTEGER,
  granted_scopes_json TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  CHECK (length(uid) BETWEEN 1 AND 256),
  CHECK (length(granted_scopes_json) <= 4000),
  CHECK (connected = 0 OR access_token_enc IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS cf_google_calendar_oauth_states (
  state_hash TEXT PRIMARY KEY CHECK (
    length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
  ),
  uid TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  CHECK (length(uid) BETWEEN 1 AND 256)
);

CREATE INDEX IF NOT EXISTS cf_google_calendar_oauth_states_expiry_idx
  ON cf_google_calendar_oauth_states(expires_at);

CREATE TRIGGER IF NOT EXISTS adf_i_google_calendar_integrations
BEFORE INSERT ON cf_google_calendar_integrations
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_google_calendar_integrations
BEFORE UPDATE ON cf_google_calendar_integrations
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_google_calendar_oauth_states
BEFORE INSERT ON cf_google_calendar_oauth_states
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_google_calendar_oauth_states
BEFORE UPDATE ON cf_google_calendar_oauth_states
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;
