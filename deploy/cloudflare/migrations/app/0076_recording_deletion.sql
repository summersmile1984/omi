CREATE TABLE IF NOT EXISTS cf_recording_deletion_intents (
  uid TEXT PRIMARY KEY NOT NULL,
  job_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_token TEXT,
  lease_until INTEGER,
  next_attempt_at INTEGER NOT NULL,
  settled_at INTEGER,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  CHECK (lease_token IS NULL OR length(lease_token) <= 64),
  CHECK (last_error IS NULL OR length(last_error) <= 128)
);

CREATE INDEX IF NOT EXISTS cf_recording_deletion_dispatch_idx
  ON cf_recording_deletion_intents(status, next_attempt_at, lease_until);

-- Account deletion remains the stronger lifecycle authority. A recording
-- cleanup may not be created or revived after the account fence exists.
CREATE TRIGGER IF NOT EXISTS adf_i_recording_deletion_intents
BEFORE INSERT ON cf_recording_deletion_intents
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_recording_deletion_intents
BEFORE UPDATE ON cf_recording_deletion_intents
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

-- While cleanup is active, storage cannot be re-enabled and no durable sync
-- or playback metadata can race the final residual scan. DELETE remains
-- allowed so the cleanup worker can drain each surface idempotently.
CREATE TRIGGER IF NOT EXISTS rdf_i_user_privacy_settings
BEFORE INSERT ON cf_user_privacy_settings
WHEN NEW.store_recording_permission = 1
AND EXISTS (
  SELECT 1 FROM cf_recording_deletion_intents WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'recording deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS rdf_u_user_privacy_settings
BEFORE UPDATE ON cf_user_privacy_settings
WHEN NEW.store_recording_permission = 1
AND EXISTS (
  SELECT 1 FROM cf_recording_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'recording deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS rdf_i_sync_jobs
BEFORE INSERT ON cf_sync_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_recording_deletion_intents WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'recording deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS rdf_u_sync_jobs
BEFORE UPDATE ON cf_sync_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_recording_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'recording deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS rdf_i_sync_job_files
BEFORE INSERT ON cf_sync_job_files
WHEN EXISTS (
  SELECT 1 FROM cf_recording_deletion_intents WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'recording deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS rdf_u_sync_job_files
BEFORE UPDATE ON cf_sync_job_files
WHEN EXISTS (
  SELECT 1 FROM cf_recording_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'recording deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS rdf_i_sync_playback_objects
BEFORE INSERT ON cf_sync_playback_objects
WHEN EXISTS (
  SELECT 1 FROM cf_recording_deletion_intents WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'recording deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS rdf_u_sync_playback_objects
BEFORE UPDATE ON cf_sync_playback_objects
WHEN EXISTS (
  SELECT 1 FROM cf_recording_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'recording deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS rdf_i_audio_asset_objects
BEFORE INSERT ON cf_asset_objects
WHEN NEW.content_type LIKE 'audio/%'
AND EXISTS (
  SELECT 1 FROM cf_recording_deletion_intents WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'recording deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS rdf_u_audio_asset_objects
BEFORE UPDATE ON cf_asset_objects
WHEN NEW.content_type LIKE 'audio/%'
AND EXISTS (
  SELECT 1 FROM cf_recording_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'recording deletion fence');
END;
