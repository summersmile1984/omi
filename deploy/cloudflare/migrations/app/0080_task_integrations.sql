CREATE TABLE IF NOT EXISTS cf_task_integrations (
  uid TEXT NOT NULL,
  app_key TEXT NOT NULL CHECK (
    app_key IN ('apple_reminders', 'todoist', 'asana', 'google_tasks', 'clickup')
  ),
  connected INTEGER NOT NULL DEFAULT 0 CHECK (connected IN (0, 1)),
  access_token_enc TEXT,
  refresh_token_enc TEXT,
  token_expires_at INTEGER,
  configuration_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, app_key),
  CHECK (length(uid) BETWEEN 1 AND 256),
  CHECK (length(configuration_json) <= 16000),
  CHECK (
    connected = 0
    OR app_key = 'apple_reminders'
    OR access_token_enc IS NOT NULL
  )
);

CREATE INDEX IF NOT EXISTS cf_task_integrations_connected_idx
  ON cf_task_integrations(uid, connected, app_key);

CREATE TABLE IF NOT EXISTS cf_task_integration_defaults (
  uid TEXT PRIMARY KEY,
  default_app TEXT CHECK (
    default_app IS NULL
    OR default_app IN ('apple_reminders', 'todoist', 'asana', 'google_tasks', 'clickup')
  ),
  updated_at INTEGER NOT NULL,
  CHECK (length(uid) BETWEEN 1 AND 256)
);

CREATE TABLE IF NOT EXISTS cf_task_integration_oauth_states (
  state_hash TEXT PRIMARY KEY CHECK (
    length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
  ),
  uid TEXT NOT NULL,
  app_key TEXT NOT NULL CHECK (
    app_key IN ('todoist', 'asana', 'google_tasks', 'clickup')
  ),
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  CHECK (length(uid) BETWEEN 1 AND 256)
);

CREATE INDEX IF NOT EXISTS cf_task_integration_oauth_states_expiry_idx
  ON cf_task_integration_oauth_states(expires_at);

CREATE TRIGGER IF NOT EXISTS adf_i_task_integrations
BEFORE INSERT ON cf_task_integrations
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_integrations
BEFORE UPDATE ON cf_task_integrations
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_integration_defaults
BEFORE INSERT ON cf_task_integration_defaults
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_integration_defaults
BEFORE UPDATE ON cf_task_integration_defaults
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_integration_oauth_states
BEFORE INSERT ON cf_task_integration_oauth_states
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_integration_oauth_states
BEFORE UPDATE ON cf_task_integration_oauth_states
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;
