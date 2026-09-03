-- Dry-run/reconciliation ledger for historical GCS private-cloud-sync chunks.
--
-- A ledger row is not audio authority and does not copy bytes.  It records a
-- bounded, idempotent plan that an operator can review only after verifying
-- the immutable GCS generation and SHA-256 before a future GCS->R2 executor.
CREATE TABLE IF NOT EXISTS cf_audio_chunk_import_ledger (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  import_id TEXT NOT NULL CHECK (length(import_id) = 64),
  conversation_id TEXT NOT NULL CHECK (length(conversation_id) BETWEEN 1 AND 128),
  source_object_uri TEXT NOT NULL CHECK (length(source_object_uri) BETWEEN 1 AND 1024),
  source_generation TEXT CHECK (source_generation IS NULL OR length(source_generation) BETWEEN 1 AND 256),
  source_object_name TEXT NOT NULL CHECK (length(source_object_name) BETWEEN 1 AND 512),
  checksum_sha256 TEXT CHECK (checksum_sha256 IS NULL OR length(checksum_sha256) = 64),
  size INTEGER CHECK (size IS NULL OR (size > 0 AND size <= 67108864)),
  source_kind TEXT CHECK (source_kind IS NULL OR source_kind IN ('pcm', 'opus')),
  encrypted INTEGER CHECK (encrypted IS NULL OR encrypted IN (0, 1)),
  is_batch INTEGER CHECK (is_batch IS NULL OR is_batch IN (0, 1)),
  start_timestamp REAL CHECK (start_timestamp IS NULL OR start_timestamp > 0),
  end_timestamp REAL CHECK (end_timestamp IS NULL OR end_timestamp >= start_timestamp),
  desired_storage_key TEXT NOT NULL CHECK (length(desired_storage_key) BETWEEN 1 AND 512),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64),
  action TEXT NOT NULL CHECK (action IN ('stage', 'blocked')),
  status TEXT NOT NULL CHECK (status IN ('planned', 'blocked', 'applied', 'failed')),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, import_id),
  CHECK (
    action = 'blocked' OR (
      source_generation IS NOT NULL AND
      checksum_sha256 IS NOT NULL AND
      size IS NOT NULL AND
      source_kind IS NOT NULL AND
      encrypted IS NOT NULL AND
      is_batch IS NOT NULL AND
      start_timestamp IS NOT NULL AND
      end_timestamp IS NOT NULL
    )
  )
);

CREATE INDEX IF NOT EXISTS cf_audio_chunk_import_ledger_target_idx
  ON cf_audio_chunk_import_ledger(uid, desired_storage_key, updated_at DESC);

CREATE INDEX IF NOT EXISTS cf_audio_chunk_import_ledger_status_idx
  ON cf_audio_chunk_import_ledger(uid, status, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_audio_chunk_import_ledger
BEFORE INSERT ON cf_audio_chunk_import_ledger
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_audio_chunk_import_ledger
BEFORE UPDATE ON cf_audio_chunk_import_ledger
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
