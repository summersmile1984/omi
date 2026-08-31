-- Hash-only Firebase anonymous identity evidence for the dormant app-owner
-- migration seam. Existing 0122 rows remain readable but are not admitted by
-- the new projection route until these fields have been populated by Auth's
-- verified Identity Toolkit bridge.

ALTER TABLE cf_app_owner_migration_sources
  ADD COLUMN source_uid_hash TEXT
  CHECK (
    source_uid_hash IS NULL OR
    (length(source_uid_hash) = 64 AND
     source_uid_hash NOT GLOB '*[^0-9a-f]*')
  );

ALTER TABLE cf_app_owner_migration_sources
  ADD COLUMN target_uid TEXT
  CHECK (target_uid IS NULL OR length(target_uid) BETWEEN 1 AND 256);

ALTER TABLE cf_app_owner_migration_sources
  ADD COLUMN target_account_generation INTEGER
  CHECK (target_account_generation IS NULL OR target_account_generation >= 0);

ALTER TABLE cf_app_owner_migration_sources
  ADD COLUMN source_credential_generation INTEGER
  CHECK (source_credential_generation IS NULL OR source_credential_generation >= 0);

ALTER TABLE cf_app_owner_migration_sources
  ADD COLUMN attestation_expires_at INTEGER
  CHECK (attestation_expires_at IS NULL OR attestation_expires_at >= 0);

CREATE UNIQUE INDEX IF NOT EXISTS cf_app_owner_migration_sources_uid_hash_idx
  ON cf_app_owner_migration_sources(source_uid_hash)
  WHERE source_uid_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS cf_app_owner_migration_sources_target_idx
  ON cf_app_owner_migration_sources(target_uid, target_account_generation, projection_status);

-- New hash-only rows must be internally coherent. `source_uid` is an opaque
-- HMAC reference, never the Firebase uid. The legacy pre-0123 rows remain
-- dormant and cannot accidentally satisfy this trigger's admitted shape.
CREATE TRIGGER IF NOT EXISTS validate_firebase_anonymous_projection_insert
BEFORE INSERT ON cf_app_owner_migration_sources
WHEN NEW.source_uid LIKE 'fb-anon-%'
 AND (
   NEW.source_uid_hash IS NULL OR
   NEW.source_uid <> ('fb-anon-' || NEW.source_uid_hash) OR
   NEW.target_uid IS NULL OR
   NEW.target_account_generation IS NULL OR
   NEW.source_credential_generation IS NULL OR
   NEW.attestation_expires_at IS NULL OR
   NEW.attestation_expires_at <= NEW.imported_at
 )
BEGIN
  SELECT RAISE(ABORT, 'invalid firebase anonymous projection');
END;

CREATE TRIGGER IF NOT EXISTS validate_firebase_anonymous_projection_update
BEFORE UPDATE ON cf_app_owner_migration_sources
WHEN NEW.source_uid LIKE 'fb-anon-%'
 AND (
   NEW.source_uid_hash IS NULL OR
   NEW.source_uid <> ('fb-anon-' || NEW.source_uid_hash) OR
   NEW.target_uid IS NULL OR
   NEW.target_account_generation IS NULL OR
   NEW.source_credential_generation IS NULL OR
   NEW.attestation_expires_at IS NULL OR
   NEW.attestation_expires_at <= NEW.imported_at
 )
BEGIN
  SELECT RAISE(ABORT, 'invalid firebase anonymous projection');
END;

-- Replace the 0122 source-only fence in a forward migration so the new target
-- identity column is covered by the canonical trigger names used by the
-- account-deletion schema guard.
DROP TRIGGER IF EXISTS adf_i_app_owner_migration_sources;
DROP TRIGGER IF EXISTS adf_u_app_owner_migration_sources;

CREATE TRIGGER IF NOT EXISTS adf_i_app_owner_migration_sources
BEFORE INSERT ON cf_app_owner_migration_sources
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents
   WHERE uid IN (NEW.source_uid, NEW.target_uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones
   WHERE uid IN (NEW.source_uid, NEW.target_uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_app_owner_migration_sources
BEFORE UPDATE ON cf_app_owner_migration_sources
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents
   WHERE uid IN (OLD.source_uid, NEW.source_uid, OLD.target_uid, NEW.target_uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones
   WHERE uid IN (OLD.source_uid, NEW.source_uid, OLD.target_uid, NEW.target_uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
