-- Staging-only authority boundary for data-protection migration requests.
--
-- The source rows currently do not carry an encrypted payload representation
-- for conversations or chat messages, and the Cloudflare profile has no
-- policy-equivalent encryption executor.  These tables intentionally model
-- the future D1 admission/receipt contract without claiming that an accepted
-- row changed data.  Until a cutover projection sets executor_state=ready,
-- the API boundary rejects every write fail-closed.
CREATE TABLE IF NOT EXISTS cf_data_protection_migration_control (
  uid TEXT PRIMARY KEY NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
  source TEXT NOT NULL CHECK (source = 'cloudflare_data_protection_projection'),
  enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
  executor_state TEXT NOT NULL CHECK (executor_state IN ('unavailable', 'ready')),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  source_revision TEXT NOT NULL CHECK (length(source_revision) BETWEEN 1 AND 256),
  updated_at INTEGER NOT NULL CHECK (updated_at >= 0)
);

-- One row is the durable idempotency receipt for a future Queue admission.
-- No request handler may create a completed row: the Queue executor must own
-- status transitions and the encrypted source-row writes atomically.
CREATE TABLE IF NOT EXISTS cf_data_protection_migration_runs (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  run_id TEXT NOT NULL CHECK (length(run_id) BETWEEN 1 AND 256),
  operation TEXT NOT NULL CHECK (operation IN ('start', 'single', 'batch', 'finalize')),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
  payload_json TEXT NOT NULL CHECK (
    length(payload_json) BETWEEN 2 AND 65536
    AND json_valid(payload_json)
    AND json_type(payload_json) = 'object'
  ),
  target_level TEXT NOT NULL CHECK (target_level = 'enhanced'),
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_token TEXT,
  lease_until INTEGER,
  next_attempt_at INTEGER NOT NULL CHECK (next_attempt_at >= 0),
  result_json TEXT CHECK (result_json IS NULL OR (length(result_json) <= 65536 AND json_valid(result_json))),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
  PRIMARY KEY (uid, run_id),
  UNIQUE (uid, request_fingerprint),
  CHECK (lease_token IS NULL OR length(lease_token) BETWEEN 1 AND 128),
  CHECK (lease_until IS NULL OR lease_until >= 0)
);

CREATE INDEX IF NOT EXISTS cf_data_protection_migration_runs_dispatch_idx
  ON cf_data_protection_migration_runs(status, next_attempt_at, lease_until);

CREATE INDEX IF NOT EXISTS cf_data_protection_migration_runs_uid_idx
  ON cf_data_protection_migration_runs(uid, account_generation, created_at DESC, run_id DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_data_protection_migration_control
BEFORE INSERT ON cf_data_protection_migration_control
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_data_protection_migration_control
BEFORE UPDATE ON cf_data_protection_migration_control
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
              AND expires_at > unixepoch())
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_data_protection_migration_runs
BEFORE INSERT ON cf_data_protection_migration_runs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_data_protection_migration_runs
BEFORE UPDATE ON cf_data_protection_migration_runs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
              AND expires_at > unixepoch())
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
