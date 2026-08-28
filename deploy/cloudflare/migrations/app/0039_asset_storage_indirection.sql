ALTER TABLE cf_asset_objects ADD COLUMN storage_key TEXT;

UPDATE cf_asset_objects SET storage_key = object_key WHERE storage_key IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS cf_asset_objects_storage_key_idx
  ON cf_asset_objects(storage_key)
  WHERE storage_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS cf_asset_cleanup_tasks (
  storage_key TEXT PRIMARY KEY NOT NULL,
  uid TEXT NOT NULL,
  logical_key TEXT NOT NULL,
  reason TEXT NOT NULL CHECK (reason IN ('uncommitted-upload', 'superseded', 'deleted')),
  not_before INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_asset_cleanup_due_idx
  ON cf_asset_cleanup_tasks(not_before, created_at);
