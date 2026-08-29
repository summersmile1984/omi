ALTER TABLE cf_app_subscriptions
  ADD COLUMN app_delete_verified_at INTEGER
  CHECK (app_delete_verified_at IS NULL OR app_delete_verified_at >= 0);

CREATE TABLE IF NOT EXISTS cf_app_deletion_fences (
  app_id TEXT PRIMARY KEY NOT NULL,
  job_id TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL,
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_app_deletion_fences_created_idx
  ON cf_app_deletion_fences(created_at, app_id);

CREATE TRIGGER IF NOT EXISTS cf_app_deletion_fence_owner
BEFORE INSERT ON cf_app_deletion_fences
WHEN NOT EXISTS (
  SELECT 1
  FROM cf_app_catalog c
  JOIN cf_jobs j ON j.job_id = NEW.job_id
  WHERE c.id = NEW.app_id
    AND c.owner_uid = j.uid
    AND j.kind = 'app_delete'
    AND json_extract(j.payload_json, '$.app_id') = NEW.app_id
)
BEGIN
  SELECT RAISE(ABORT, 'app deletion owner mismatch');
END;

CREATE TRIGGER IF NOT EXISTS cf_app_deletion_catalog_update_fence
BEFORE UPDATE ON cf_app_catalog
WHEN EXISTS (
  SELECT 1 FROM cf_app_deletion_fences WHERE app_id = OLD.id
)
BEGIN
  SELECT RAISE(ABORT, 'app deletion fence');
END;
