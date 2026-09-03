-- Cloudflare-owned metadata for the private chat-file staging boundary.
--
-- The provider id is deliberately retained with the uid and object key.  A
-- provider delete can therefore be retried without trusting a caller-supplied
-- OpenAI id, while account deletion can prove that every object key is in the
-- account's private prefix.
CREATE TABLE IF NOT EXISTS cf_chat_files (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  file_id TEXT NOT NULL CHECK (length(file_id) BETWEEN 1 AND 128),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
  provider TEXT NOT NULL CHECK (provider = 'openai'),
  -- Null until the external upload commits; a shared sentinel would violate
  -- the provider-id uniqueness constraint for concurrent staging rows.
  provider_file_id TEXT CHECK (provider_file_id IS NULL OR length(provider_file_id) BETWEEN 1 AND 256),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 512),
  mime_type TEXT NOT NULL CHECK (length(mime_type) BETWEEN 1 AND 200),
  size INTEGER NOT NULL CHECK (size > 0),
  checksum_sha256 TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
  storage_key TEXT NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 512),
  status TEXT NOT NULL CHECK (status IN ('staging', 'ready', 'failed', 'deleted')),
  thumbnail_status TEXT NOT NULL CHECK (thumbnail_status IN ('not_applicable', 'unsupported', 'ready')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  PRIMARY KEY (uid, file_id),
  UNIQUE (uid, request_fingerprint),
  UNIQUE (provider_file_id)
);

CREATE INDEX IF NOT EXISTS cf_chat_files_uid_created_idx
  ON cf_chat_files(uid, created_at DESC, file_id DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_chat_files
BEFORE INSERT ON cf_chat_files
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_files
BEFORE UPDATE ON cf_chat_files
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
