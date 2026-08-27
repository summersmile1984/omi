CREATE TABLE IF NOT EXISTS cf_asset_objects (
  uid TEXT NOT NULL,
  object_key TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size INTEGER NOT NULL,
  etag TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, object_key)
);

CREATE INDEX IF NOT EXISTS cf_asset_objects_updated_idx ON cf_asset_objects(uid, updated_at);
