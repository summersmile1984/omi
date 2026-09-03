-- Dry-run/reconciliation ledger for historical Firestore/GCS chat files.
--
-- A ledger row is never the chat-file authority.  It records a bounded,
-- idempotent copy plan so an operator can verify the source checksum and
-- provider object before writing cf_chat_files or CHAT_FILES.  In particular,
-- a planned row must not be interpreted as a ready file.
CREATE TABLE IF NOT EXISTS cf_chat_file_import_ledger (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  import_id TEXT NOT NULL CHECK (length(import_id) = 64),
  source_file_id TEXT NOT NULL CHECK (length(source_file_id) BETWEEN 1 AND 128),
  source_object_uri TEXT NOT NULL CHECK (length(source_object_uri) BETWEEN 1 AND 1024),
  source_generation TEXT CHECK (source_generation IS NULL OR length(source_generation) BETWEEN 1 AND 256),
  checksum_sha256 TEXT CHECK (checksum_sha256 IS NULL OR length(checksum_sha256) = 64),
  provider_file_id TEXT CHECK (provider_file_id IS NULL OR length(provider_file_id) BETWEEN 1 AND 256),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 512),
  mime_type TEXT NOT NULL CHECK (length(mime_type) BETWEEN 1 AND 200),
  size INTEGER CHECK (size IS NULL OR (size > 0 AND size <= 52428800)),
  desired_storage_key TEXT NOT NULL CHECK (length(desired_storage_key) BETWEEN 1 AND 512),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64),
  action TEXT NOT NULL CHECK (action IN ('stage', 'blocked')),
  status TEXT NOT NULL CHECK (status IN ('planned', 'blocked', 'applied', 'failed')),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, import_id),
  UNIQUE (uid, source_file_id, checksum_sha256),
  UNIQUE (provider_file_id)
);

CREATE INDEX IF NOT EXISTS cf_chat_file_import_ledger_uid_status_idx
  ON cf_chat_file_import_ledger(uid, status, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_chat_file_import_ledger
BEFORE INSERT ON cf_chat_file_import_ledger
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_file_import_ledger
BEFORE UPDATE ON cf_chat_file_import_ledger
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
