-- Dormant authority for migrating an app from a legacy anonymous Firebase
-- owner to a Better Auth account.
--
-- The source row is written only by the verified export/import workflow.  It
-- stores a hash of the Firebase anonymous proof, never the credential itself.
-- Keeping the proof and imported projection state in D1 gives a future Jobs
-- consumer a replayable admission record without allowing a caller to turn
-- an old_id into ownership by assertion alone.
CREATE TABLE IF NOT EXISTS cf_app_owner_migration_sources (
  source_uid TEXT PRIMARY KEY NOT NULL
    CHECK (length(source_uid) BETWEEN 1 AND 256),
  source_provider TEXT NOT NULL CHECK (source_provider = 'firebase-anonymous'),
  source_proof_hash TEXT NOT NULL
    CHECK (length(source_proof_hash) = 64 AND source_proof_hash NOT GLOB '*[^0-9a-f]*'),
  source_projection_revision TEXT NOT NULL
    CHECK (length(source_projection_revision) BETWEEN 1 AND 256),
  projection_status TEXT NOT NULL
    CHECK (projection_status IN ('imported', 'revoked', 'conflict')),
  app_projection_count INTEGER NOT NULL DEFAULT 0 CHECK (app_projection_count >= 0),
  memory_projection_count INTEGER NOT NULL DEFAULT 0 CHECK (memory_projection_count >= 0),
  imported_at INTEGER NOT NULL CHECK (imported_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= imported_at)
);

CREATE TABLE IF NOT EXISTS cf_app_owner_migration_jobs (
  job_id TEXT PRIMARY KEY NOT NULL
    CHECK (length(job_id) BETWEEN 1 AND 128),
  source_uid TEXT NOT NULL
    CHECK (length(source_uid) BETWEEN 1 AND 256),
  target_uid TEXT NOT NULL
    CHECK (length(target_uid) BETWEEN 1 AND 256),
  source_proof_hash TEXT NOT NULL
    CHECK (length(source_proof_hash) = 64 AND source_proof_hash NOT GLOB '*[^0-9a-f]*'),
  source_projection_revision TEXT NOT NULL
    CHECK (length(source_projection_revision) BETWEEN 1 AND 256),
  target_account_generation INTEGER NOT NULL CHECK (target_account_generation >= 0),
  idempotency_key TEXT NOT NULL
    CHECK (length(idempotency_key) BETWEEN 1 AND 128),
  request_fingerprint TEXT NOT NULL
    CHECK (length(request_fingerprint) = 64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_token TEXT CHECK (lease_token IS NULL OR length(lease_token) BETWEEN 1 AND 128),
  lease_until INTEGER CHECK (lease_until IS NULL OR lease_until >= 0),
  next_attempt_at INTEGER NOT NULL CHECK (next_attempt_at >= 0),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2_048),
  result_json TEXT CHECK (
    result_json IS NULL OR (length(result_json) <= 65_536 AND json_valid(result_json))
  ),
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
  CHECK (source_uid <> target_uid),
  FOREIGN KEY (source_uid) REFERENCES cf_app_owner_migration_sources(source_uid)
    ON DELETE RESTRICT,
  -- A verified anonymous source may be consumed by one destination only.
  -- This closes a replay/cross-owner race even when two Better Auth users
  -- present the same imported proof concurrently.
  UNIQUE (source_uid),
  UNIQUE (target_uid, idempotency_key)
);

CREATE INDEX IF NOT EXISTS cf_app_owner_migration_jobs_dispatch_idx
  ON cf_app_owner_migration_jobs(status, next_attempt_at, lease_until, updated_at);

CREATE INDEX IF NOT EXISTS cf_app_owner_migration_jobs_source_idx
  ON cf_app_owner_migration_jobs(source_uid, status, updated_at DESC, job_id);

CREATE INDEX IF NOT EXISTS cf_app_owner_migration_jobs_target_idx
  ON cf_app_owner_migration_jobs(target_uid, status, created_at DESC, job_id);

-- A source migration is identity-bearing for both participants.  A deletion
-- intent or tombstone on either side must stop a new admission and prevent a
-- late queue delivery from changing the job's state.  DELETE remains allowed
-- to the account-deletion owner so residual cleanup can remove these rows.
CREATE TRIGGER IF NOT EXISTS adf_i_app_owner_migration_sources
BEFORE INSERT ON cf_app_owner_migration_sources
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.source_uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.source_uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_app_owner_migration_sources
BEFORE UPDATE ON cf_app_owner_migration_sources
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.source_uid, NEW.source_uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.source_uid, NEW.source_uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_app_owner_migration_jobs
BEFORE INSERT ON cf_app_owner_migration_jobs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (NEW.source_uid, NEW.target_uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (NEW.source_uid, NEW.target_uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_app_owner_migration_jobs
BEFORE UPDATE ON cf_app_owner_migration_jobs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.source_uid, OLD.target_uid, NEW.source_uid, NEW.target_uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.source_uid, OLD.target_uid, NEW.source_uid, NEW.target_uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
