-- First-phase authority contract for the short-term lifecycle admin boundary.
--
-- These tables are deliberately projections, not a Firestore compatibility
-- cache.  A writer must populate the control row from an approved cutover
-- projection and must provide the current account generation.  Until the
-- control row says that both the projection and executor are ready, the Jobs
-- route returns a fail-closed 503 and does not create a run.
CREATE TABLE IF NOT EXISTS cf_memory_short_term_lifecycle_control (
  uid TEXT PRIMARY KEY NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
  source TEXT NOT NULL CHECK (source = 'cloudflare_short_term_lifecycle_projection'),
  enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
  executor_state TEXT NOT NULL CHECK (executor_state IN ('unavailable', 'ready')),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  source_revision TEXT NOT NULL CHECK (length(source_revision) BETWEEN 1 AND 256),
  updated_at INTEGER NOT NULL
);

-- A run is the durable Queue admission and lease authority.  The unique
-- request fingerprint makes an exact retry idempotent while a reused run_id
-- with different inputs is rejected by the Jobs boundary.
CREATE TABLE IF NOT EXISTS cf_memory_short_term_lifecycle_runs (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  run_id TEXT NOT NULL CHECK (length(run_id) BETWEEN 1 AND 256),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
  evaluated_at INTEGER NOT NULL CHECK (evaluated_at >= 0),
  requested_limit INTEGER NOT NULL CHECK (requested_limit BETWEEN 1 AND 1000),
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_token TEXT,
  lease_until INTEGER,
  next_attempt_at INTEGER NOT NULL,
  result_json TEXT CHECK (result_json IS NULL OR (length(result_json) <= 65536 AND json_valid(result_json))),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 256),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, run_id),
  UNIQUE (uid, request_fingerprint),
  CHECK (lease_token IS NULL OR length(lease_token) BETWEEN 1 AND 128),
  CHECK (lease_until IS NULL OR lease_until >= 0)
);

CREATE INDEX IF NOT EXISTS cf_memory_short_term_lifecycle_runs_dispatch_idx
  ON cf_memory_short_term_lifecycle_runs(status, next_attempt_at, lease_until);

-- This is the future canonical transition authority.  The current worker
-- intentionally does not write it: it still needs a D1 memory projection and
-- a policy-equivalent executor before the legacy Firestore runner can be cut.
CREATE TABLE IF NOT EXISTS cf_memory_short_term_lifecycle_transitions (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  transition_id TEXT NOT NULL CHECK (length(transition_id) BETWEEN 1 AND 256),
  memory_id TEXT NOT NULL CHECK (length(memory_id) BETWEEN 1 AND 256),
  run_id TEXT NOT NULL CHECK (length(run_id) BETWEEN 1 AND 256),
  outcome TEXT NOT NULL CHECK (outcome IN (
    'remain_short_term', 'promote_to_long_term', 'archive',
    'reject_or_hide', 'source_tombstoned'
  )),
  reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 2048),
  evaluated_at TEXT NOT NULL CHECK (length(evaluated_at) BETWEEN 1 AND 128),
  audit_metadata_json TEXT NOT NULL CHECK (
    length(audit_metadata_json) BETWEEN 2 AND 65536
    AND json_valid(audit_metadata_json)
    AND json_type(audit_metadata_json) = 'object'
  ),
  idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 512),
  fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  created_at INTEGER NOT NULL,
  PRIMARY KEY (uid, transition_id),
  UNIQUE (uid, idempotency_key)
);

CREATE INDEX IF NOT EXISTS cf_memory_short_term_lifecycle_transitions_memory_idx
  ON cf_memory_short_term_lifecycle_transitions(uid, account_generation, memory_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_memory_short_term_lifecycle_control
BEFORE INSERT ON cf_memory_short_term_lifecycle_control
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_short_term_lifecycle_control
BEFORE UPDATE ON cf_memory_short_term_lifecycle_control
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_short_term_lifecycle_runs
BEFORE INSERT ON cf_memory_short_term_lifecycle_runs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_short_term_lifecycle_runs
BEFORE UPDATE ON cf_memory_short_term_lifecycle_runs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_short_term_lifecycle_transitions
BEFORE INSERT ON cf_memory_short_term_lifecycle_transitions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_short_term_lifecycle_transitions
BEFORE UPDATE ON cf_memory_short_term_lifecycle_transitions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
