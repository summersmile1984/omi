CREATE TABLE IF NOT EXISTS cf_developer_api_keys (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  key_id TEXT PRIMARY KEY NOT NULL CHECK (length(key_id) BETWEEN 1 AND 64),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
  key_hash TEXT NOT NULL UNIQUE CHECK (length(key_hash) = 64 AND key_hash NOT GLOB '*[^0-9a-f]*'),
  key_prefix TEXT NOT NULL CHECK (
    key_prefix = 'omi_dev_legacy'
    OR (
      length(key_prefix) = 19
      AND substr(key_prefix, 1, 8) = 'omi_dev_'
      AND substr(key_prefix, 13, 3) = '...'
      AND substr(key_prefix, 9, 4) NOT GLOB '*[^0-9a-f]*'
      AND substr(key_prefix, 16, 4) NOT GLOB '*[^0-9a-f]*'
    )
  ),
  app_id TEXT NOT NULL DEFAULT 'developer_api' CHECK (app_id = 'developer_api'),
  scopes_json TEXT NOT NULL CHECK (json_valid(scopes_json) AND json_type(scopes_json) = 'array'),
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  last_used_at INTEGER CHECK (last_used_at IS NULL OR last_used_at >= created_at)
);

CREATE INDEX IF NOT EXISTS cf_developer_api_keys_uid_created_idx
  ON cf_developer_api_keys(uid, created_at DESC, key_id ASC);

CREATE TRIGGER IF NOT EXISTS adf_i_developer_api_keys
BEFORE INSERT ON cf_developer_api_keys
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_developer_api_keys
BEFORE UPDATE ON cf_developer_api_keys
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
