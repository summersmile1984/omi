-- Staging executor metadata for the smallest safe app-owner migration slice.
--
-- `owner_uid` is the only D1 app authority this executor changes.  A source
-- owner is an opaque `fb-anon-<sha256>` reference produced by the Auth
-- identity projection; raw Firebase ids are never reconstructed in D1.  The
-- marker makes a retry after a Worker crash observable and lets the executor
-- finish an already-applied CAS without guessing ownership.
ALTER TABLE cf_app_catalog
  ADD COLUMN owner_account_generation INTEGER
  CHECK (owner_account_generation IS NULL OR owner_account_generation >= 0);

ALTER TABLE cf_app_catalog
  ADD COLUMN owner_migration_job_id TEXT
  CHECK (owner_migration_job_id IS NULL OR length(owner_migration_job_id) BETWEEN 1 AND 128);

CREATE INDEX IF NOT EXISTS cf_app_catalog_owner_migration_idx
  ON cf_app_catalog(owner_migration_job_id, owner_uid, owner_account_generation, id);

-- A catalog marker may only be written by the matching queued/running
-- migration job.  This prevents a generic app mutation from claiming that a
-- catalog row was transferred without the source-proof admission record.
CREATE TRIGGER IF NOT EXISTS validate_app_owner_migration_marker
BEFORE UPDATE OF owner_uid, owner_account_generation, owner_migration_job_id
ON cf_app_catalog
WHEN NEW.owner_migration_job_id IS NOT NULL
 AND NOT EXISTS (
   SELECT 1
   FROM cf_app_owner_migration_jobs j
   WHERE j.job_id = NEW.owner_migration_job_id
     AND j.source_uid = OLD.owner_uid
     AND j.target_uid = NEW.owner_uid
     AND j.target_account_generation = NEW.owner_account_generation
     AND j.status IN ('queued', 'running')
 )
BEGIN
  SELECT RAISE(ABORT, 'app owner migration marker mismatch');
END;

-- Markers are immutable once the source owner has been replaced.  The
-- account-deletion owner can still DELETE the row; it cannot resurrect or
-- retarget a migrated app through a late update.
CREATE TRIGGER IF NOT EXISTS validate_app_owner_migration_marker_update
BEFORE UPDATE OF owner_uid, owner_account_generation, owner_migration_job_id
ON cf_app_catalog
WHEN OLD.owner_migration_job_id IS NOT NULL
 AND (
   NEW.owner_migration_job_id IS NOT OLD.owner_migration_job_id
   OR NEW.owner_uid IS NOT OLD.owner_uid
   OR NEW.owner_account_generation IS NOT OLD.owner_account_generation
 )
BEGIN
  SELECT RAISE(ABORT, 'app owner migration marker immutable');
END;
