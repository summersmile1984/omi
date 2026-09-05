-- The product singleton is operator-owned; ratings are immutable per user/platform.
CREATE TABLE cf_csat_config (
  id TEXT PRIMARY KEY CHECK (id = 'product'),
  config_json TEXT NOT NULL CHECK (json_valid(config_json))
);

CREATE TABLE cf_csat_ratings (
  id TEXT PRIMARY KEY,
  uid TEXT NOT NULL,
  platform TEXT NOT NULL CHECK (platform IN ('macos', 'windows', 'ios', 'android')),
  app_version TEXT NOT NULL,
  score INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
  comment TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision >= 0),
  created_at INTEGER NOT NULL,
  UNIQUE (uid, platform)
);

CREATE TRIGGER IF NOT EXISTS adf_i_csat_ratings BEFORE INSERT ON cf_csat_ratings
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_csat_ratings BEFORE UPDATE ON cf_csat_ratings
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER csat_ratings_immutable BEFORE UPDATE ON cf_csat_ratings
BEGIN SELECT RAISE(ABORT, 'csat rating is create-only'); END;
