-- Device-pushed Apple Health projection for the isolated Cloudflare profile.
-- HealthKit data is supplied by the authenticated iOS client; no provider
-- credential or external OAuth grant is stored in this table.
CREATE TABLE IF NOT EXISTS cf_apple_health (
  uid TEXT PRIMARY KEY,
  connected INTEGER NOT NULL DEFAULT 0 CHECK (connected IN (0, 1)),
  health_data_json TEXT NOT NULL DEFAULT '{}' CHECK (length(health_data_json) <= 500000),
  last_synced TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  CHECK (length(uid) BETWEEN 1 AND 256),
  CHECK (last_synced IS NULL OR length(last_synced) BETWEEN 1 AND 64)
);

CREATE INDEX IF NOT EXISTS cf_apple_health_connected_idx
  ON cf_apple_health(uid, connected);

CREATE TRIGGER IF NOT EXISTS adf_i_apple_health
BEFORE INSERT ON cf_apple_health
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_apple_health
BEFORE UPDATE ON cf_apple_health
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;
