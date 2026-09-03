-- Persist a provider-scoped generation so an OAuth callback that already
-- consumed its one-time state cannot recreate an integration after the user
-- disconnects it while the provider exchange is in flight.
CREATE TABLE IF NOT EXISTS cf_task_integration_fences (
  uid TEXT NOT NULL,
  app_key TEXT NOT NULL CHECK (
    app_key IN ('apple_reminders', 'todoist', 'asana', 'google_tasks', 'clickup')
  ),
  oauth_generation INTEGER NOT NULL DEFAULT 0 CHECK (oauth_generation >= 0),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, app_key),
  CHECK (length(uid) BETWEEN 1 AND 256)
);

CREATE INDEX IF NOT EXISTS cf_task_integration_fences_uid_idx
  ON cf_task_integration_fences(uid);

ALTER TABLE cf_task_integration_oauth_states
  ADD COLUMN oauth_generation INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS cf_task_integration_oauth_states_generation_idx
  ON cf_task_integration_oauth_states(uid, app_key, oauth_generation);

CREATE TRIGGER IF NOT EXISTS adf_i_task_integration_fences
BEFORE INSERT ON cf_task_integration_fences
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_integration_fences
BEFORE UPDATE ON cf_task_integration_fences
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;
