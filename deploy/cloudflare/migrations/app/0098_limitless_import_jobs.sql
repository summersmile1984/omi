-- Cloudflare owner for Limitless ZIP imports.
--
-- The staged ZIP is an input artifact only.  A Queue consumer must claim the
-- row, validate the archive, and commit canonical conversation projections
-- before the object is removed.  The request fingerprint is uid-scoped so a
-- retried upload returns the same job instead of creating duplicate
-- conversations.
CREATE TABLE IF NOT EXISTS cf_import_jobs (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  job_id TEXT NOT NULL CHECK (length(job_id) BETWEEN 1 AND 128),
  source_type TEXT NOT NULL CHECK (source_type = 'limitless'),
  source_object_key TEXT NOT NULL CHECK (length(source_object_key) BETWEEN 1 AND 512),
  source_filename TEXT NOT NULL CHECK (length(source_filename) BETWEEN 1 AND 512),
  language_code TEXT NOT NULL DEFAULT 'en' CHECK (length(language_code) BETWEEN 2 AND 32),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
  account_generation INTEGER NOT NULL DEFAULT 0 CHECK (account_generation >= 0),
  status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')),
  total_files INTEGER NOT NULL DEFAULT 0 CHECK (total_files >= 0),
  processed_files INTEGER NOT NULL DEFAULT 0 CHECK (processed_files >= 0),
  conversations_created INTEGER NOT NULL DEFAULT 0 CHECK (conversations_created >= 0),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_token TEXT,
  lease_until INTEGER,
  started_at INTEGER,
  completed_at INTEGER,
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, job_id),
  UNIQUE (uid, request_fingerprint)
);

CREATE INDEX IF NOT EXISTS cf_import_jobs_uid_created_idx
  ON cf_import_jobs(uid, created_at DESC, job_id DESC);

CREATE INDEX IF NOT EXISTS cf_import_jobs_dispatch_idx
  ON cf_import_jobs(status, lease_until, updated_at);

CREATE TRIGGER IF NOT EXISTS adf_i_import_jobs
BEFORE INSERT ON cf_import_jobs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_import_jobs
BEFORE UPDATE ON cf_import_jobs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
