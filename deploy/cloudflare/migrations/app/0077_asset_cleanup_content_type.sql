ALTER TABLE cf_asset_cleanup_tasks ADD COLUMN content_type TEXT;

CREATE INDEX IF NOT EXISTS cf_asset_cleanup_uid_content_type_idx
  ON cf_asset_cleanup_tasks(uid, content_type, created_at);
