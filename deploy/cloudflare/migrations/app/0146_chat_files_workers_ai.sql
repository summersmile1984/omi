-- Make the canonical chat-file authority provider-neutral.
--
-- New Cloudflare clients keep file bytes in the uid-scoped CHAT_FILES R2
-- bucket and use Workers AI for questions.  The old OpenAI provider value is
-- retained only for explicitly reviewed compatibility rows; it is no longer
-- the only legal provider value.
PRAGMA foreign_keys = OFF;

-- This trigger references cf_chat_files directly.  SQLite otherwise keeps
-- the trigger definition alive while the table is rebuilt and rejects the
-- next schema change with "no such table".
DROP TRIGGER IF EXISTS validate_chat_file_history_apply;

CREATE TABLE cf_chat_files_workers_ai (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  file_id TEXT NOT NULL CHECK (length(file_id) BETWEEN 1 AND 128),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
  provider TEXT NOT NULL CHECK (provider IN ('openai', 'cloudflare-workers-ai')),
  provider_file_id TEXT CHECK (provider_file_id IS NULL OR length(provider_file_id) BETWEEN 1 AND 256),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 512),
  mime_type TEXT NOT NULL CHECK (length(mime_type) BETWEEN 1 AND 200),
  size INTEGER NOT NULL CHECK (size > 0),
  checksum_sha256 TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
  storage_key TEXT NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 512),
  thumbnail_key TEXT,
  status TEXT NOT NULL CHECK (status IN ('staging', 'ready', 'failed', 'deleted')),
  thumbnail_status TEXT NOT NULL CHECK (thumbnail_status IN ('not_applicable', 'unsupported', 'ready')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  PRIMARY KEY (uid, file_id),
  UNIQUE (uid, request_fingerprint),
  UNIQUE (provider_file_id)
);

INSERT INTO cf_chat_files_workers_ai (
  uid, file_id, request_fingerprint, provider, provider_file_id, name,
  mime_type, size, checksum_sha256, storage_key, thumbnail_key, status,
  thumbnail_status, created_at, updated_at, last_error
)
SELECT uid, file_id, request_fingerprint, provider, provider_file_id, name,
  mime_type, size, checksum_sha256, storage_key, thumbnail_key, status,
  thumbnail_status, created_at, updated_at, last_error
FROM cf_chat_files;

DROP TABLE cf_chat_files;
ALTER TABLE cf_chat_files_workers_ai RENAME TO cf_chat_files;

CREATE INDEX cf_chat_files_uid_created_idx
  ON cf_chat_files(uid, created_at DESC, file_id DESC);
CREATE INDEX cf_chat_files_thumbnail_idx
  ON cf_chat_files(uid, thumbnail_key);

CREATE TRIGGER adf_i_chat_files
BEFORE INSERT ON cf_chat_files
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER adf_u_chat_files
BEFORE UPDATE ON cf_chat_files
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

-- Restore the historical review/apply guard after the table rebuild.  It is
-- intentionally kept identical to 0137 so a migration cannot turn a
-- reviewed provider-object import into an unchecked canonical write.
CREATE TRIGGER validate_chat_file_history_apply
BEFORE INSERT ON cf_chat_file_history_applies
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
   OR NOT EXISTS (
     SELECT 1 FROM cf_account_cutover AS c
     WHERE c.uid = NEW.uid AND c.state = 'new'
       AND c.checkpoint_phase = 'completed'
       AND c.destination_backend_bound = 1
       AND c.account_generation = NEW.account_generation
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_chat_file_history_review_items AS i
     WHERE i.review_id = NEW.review_id AND i.uid = NEW.uid
       AND i.import_id = NEW.import_id AND i.file_id = NEW.file_id
       AND i.provider_file_id = NEW.provider_file_id
       AND i.checksum_sha256 = NEW.checksum_sha256
       AND i.plan_hash = NEW.plan_hash
       AND i.account_generation = NEW.account_generation
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_chat_file_import_ledger AS l
     WHERE l.uid = NEW.uid AND l.import_id = NEW.import_id
       AND l.plan_hash = NEW.plan_hash AND l.status = 'applied'
       AND l.account_generation = NEW.account_generation
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_chat_files AS f
     WHERE f.uid = NEW.uid AND f.file_id = NEW.file_id
       AND f.provider_file_id = NEW.provider_file_id
       AND f.checksum_sha256 = NEW.checksum_sha256
       AND f.status = 'ready'
   )
BEGIN
  SELECT RAISE(ABORT, 'chat file history authority changed');
END;

PRAGMA foreign_keys = ON;
