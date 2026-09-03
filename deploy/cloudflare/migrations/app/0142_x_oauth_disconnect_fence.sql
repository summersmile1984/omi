-- Prevent an in-flight X OAuth callback from restoring a connection after the
-- user disconnects X while the provider token exchange is in progress.
-- Keep the fence separate from the connection row so disconnect can advance
-- the generation even when no connection has ever been stored.
CREATE TABLE IF NOT EXISTS cf_x_oauth_fences (
  uid TEXT PRIMARY KEY,
  oauth_generation INTEGER NOT NULL DEFAULT 0 CHECK (oauth_generation >= 0),
  updated_at INTEGER NOT NULL,
  CHECK (length(uid) BETWEEN 1 AND 256)
);

ALTER TABLE cf_x_oauth_states
  ADD COLUMN oauth_generation INTEGER NOT NULL DEFAULT 0 CHECK (oauth_generation >= 0);

CREATE INDEX IF NOT EXISTS cf_x_oauth_states_generation_idx
  ON cf_x_oauth_states(uid, oauth_generation);

-- Preserve pending states/connections created before this migration.  Those
-- rows start at generation zero and are still valid until consumed or expired.
INSERT OR IGNORE INTO cf_x_oauth_fences (uid, oauth_generation, updated_at)
  SELECT uid, 0, updated_at FROM cf_x_connections;
INSERT OR IGNORE INTO cf_x_oauth_fences (uid, oauth_generation, updated_at)
  SELECT uid, 0, created_at FROM cf_x_oauth_states;

CREATE TRIGGER IF NOT EXISTS adf_i_x_oauth_fences
BEFORE INSERT ON cf_x_oauth_fences
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_x_oauth_fences
BEFORE UPDATE ON cf_x_oauth_fences
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;
