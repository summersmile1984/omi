-- Make the source data projection a separate authority from Firebase identity.
--
-- 0123 proves only that a Firebase anonymous credential belongs to the source
-- identity.  It does not prove that Firestore apps/memories were exported,
-- projected, or re-encrypted.  These fields stay unverified until a reviewed
-- export/import workflow records a content-bound revision.  The Jobs executor
-- must not claim an owner migration from identity evidence alone.
ALTER TABLE cf_app_owner_migration_sources
  ADD COLUMN data_projection_status TEXT NOT NULL DEFAULT 'unverified'
  CHECK (data_projection_status IN ('unverified', 'verified'));

ALTER TABLE cf_app_owner_migration_sources
  ADD COLUMN data_projection_revision TEXT
  CHECK (
    data_projection_revision IS NULL OR
    (length(data_projection_revision) = 64 AND
     data_projection_revision NOT GLOB '*[^0-9a-f]*')
  );

ALTER TABLE cf_app_owner_migration_sources
  ADD COLUMN memory_reencryption_status TEXT NOT NULL DEFAULT 'unverified'
  CHECK (memory_reencryption_status IN ('unverified', 'completed', 'not_required'));

ALTER TABLE cf_app_owner_migration_sources
  ADD COLUMN memory_reencryption_revision TEXT
  CHECK (
    memory_reencryption_revision IS NULL OR
    (length(memory_reencryption_revision) = 64 AND
     memory_reencryption_revision NOT GLOB '*[^0-9a-f]*')
  );

CREATE INDEX IF NOT EXISTS cf_app_owner_migration_sources_data_projection_idx
  ON cf_app_owner_migration_sources(
    data_projection_status,
    memory_reencryption_status,
    target_uid,
    updated_at
  );

-- A verified data projection must carry a content-bound revision.  A memory
-- result may claim `not_required` only for a genuinely empty source; a
-- non-empty source must carry an independent re-encryption revision.
CREATE TRIGGER IF NOT EXISTS validate_app_owner_data_projection_insert
BEFORE INSERT ON cf_app_owner_migration_sources
WHEN (
  NEW.data_projection_status = 'verified' AND
  NEW.data_projection_revision IS NULL
)
OR (
  NEW.memory_reencryption_status = 'not_required' AND
  NEW.memory_projection_count <> 0
)
OR (
  NEW.memory_reencryption_status = 'completed' AND
  NEW.memory_reencryption_revision IS NULL
)
BEGIN
  SELECT RAISE(ABORT, 'invalid app owner data projection attestation');
END;

CREATE TRIGGER IF NOT EXISTS validate_app_owner_data_projection_update
BEFORE UPDATE ON cf_app_owner_migration_sources
WHEN (
  NEW.data_projection_status = 'verified' AND
  NEW.data_projection_revision IS NULL
)
OR (
  NEW.memory_reencryption_status = 'not_required' AND
  NEW.memory_projection_count <> 0
)
OR (
  NEW.memory_reencryption_status = 'completed' AND
  NEW.memory_reencryption_revision IS NULL
)
BEGIN
  SELECT RAISE(ABORT, 'invalid app owner data projection attestation');
END;

-- Once an import is attested, its content/re-encryption evidence cannot be
-- rewritten underneath a queued or running migration.  Revocation remains
-- possible through projection_status and is guarded by the existing deletion
-- fence triggers.
CREATE TRIGGER IF NOT EXISTS validate_app_owner_data_projection_immutable
BEFORE UPDATE ON cf_app_owner_migration_sources
WHEN (
  OLD.data_projection_status = 'verified' AND (
    NEW.data_projection_status IS NOT OLD.data_projection_status OR
    NEW.data_projection_revision IS NOT OLD.data_projection_revision
  )
)
OR (
  OLD.memory_reencryption_status IN ('completed', 'not_required') AND (
    NEW.memory_reencryption_status IS NOT OLD.memory_reencryption_status OR
    NEW.memory_reencryption_revision IS NOT OLD.memory_reencryption_revision
  )
)
BEGIN
  SELECT RAISE(ABORT, 'app owner data projection attestation immutable');
END;
