-- Resumable R2 uploads for user-scoped Cloudflare assets.
--
-- R2 owns the multipart bytes; D1 owns the authenticated upload lease and
-- part ledger so retries, completion and account deletion remain uid-scoped.
CREATE TABLE IF NOT EXISTS cf_asset_multipart_uploads (
  uid TEXT NOT NULL,
  upload_id TEXT NOT NULL,
  object_key TEXT NOT NULL,
  storage_key TEXT NOT NULL,
  content_type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  expected_size INTEGER,
  expected_checksum_sha256 TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL CHECK (state IN ('pending', 'completed', 'aborted')),
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, upload_id),
  UNIQUE (uid, idempotency_key),
  UNIQUE (uid, object_key, state)
);

CREATE INDEX IF NOT EXISTS cf_asset_multipart_uploads_expiry_idx
  ON cf_asset_multipart_uploads(expires_at, updated_at);

CREATE TABLE IF NOT EXISTS cf_asset_multipart_parts (
  uid TEXT NOT NULL,
  upload_id TEXT NOT NULL,
  part_number INTEGER NOT NULL CHECK (part_number BETWEEN 1 AND 10000),
  size INTEGER NOT NULL CHECK (size > 0),
  etag TEXT NOT NULL,
  checksum_sha256 TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, upload_id, part_number),
  FOREIGN KEY (uid, upload_id)
    REFERENCES cf_asset_multipart_uploads(uid, upload_id)
    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_asset_multipart_parts_upload_idx
  ON cf_asset_multipart_parts(uid, upload_id, part_number);

-- 0052 predates these tables, so install the account-deletion fence here too.
CREATE TRIGGER IF NOT EXISTS adf_i_asset_multipart_uploads
BEFORE INSERT ON cf_asset_multipart_uploads
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_asset_multipart_uploads
BEFORE UPDATE ON cf_asset_multipart_uploads
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_asset_multipart_parts
BEFORE INSERT ON cf_asset_multipart_parts
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_asset_multipart_parts
BEFORE UPDATE ON cf_asset_multipart_parts
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
