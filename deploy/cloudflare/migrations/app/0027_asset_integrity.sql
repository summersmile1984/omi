ALTER TABLE cf_asset_objects ADD COLUMN checksum_sha256 TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS cf_asset_objects_uid_checksum_idx
  ON cf_asset_objects(uid, checksum_sha256);
