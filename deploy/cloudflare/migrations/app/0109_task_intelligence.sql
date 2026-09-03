-- Cloudflare-native task-intelligence authority for Better Auth accounts.
--
-- These tables intentionally do not mirror the legacy Firestore collections.
-- Candidate identity is bound to uid + account_generation + request fingerprint,
-- while device-local snapshots are separately fenced by device_id.  The
-- evaluation/job/receipt records give the LLM boundary an auditable result and
-- leave a durable retry lease for a future queue drain.

CREATE TABLE IF NOT EXISTS cf_task_candidates (
  uid TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  device_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected', 'expired')),
  description TEXT NOT NULL,
  due_at INTEGER,
  source TEXT,
  priority TEXT,
  metadata TEXT,
  category TEXT,
  relevance_score INTEGER CHECK (relevance_score IS NULL OR (relevance_score >= 0 AND relevance_score <= 1000)),
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  request_fingerprint TEXT NOT NULL,
  resolution_reason TEXT,
  result_task_id TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  resolved_at INTEGER,
  PRIMARY KEY (uid, candidate_id),
  UNIQUE (uid, account_generation, request_fingerprint)
);

CREATE INDEX IF NOT EXISTS cf_task_candidates_pending_idx
  ON cf_task_candidates(uid, account_generation, status, relevance_score DESC, created_at DESC, candidate_id);

CREATE TABLE IF NOT EXISTS cf_task_interventions (
  uid TEXT NOT NULL,
  intervention_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  attribution_chain_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (uid, intervention_id),
  UNIQUE (uid, account_generation, request_fingerprint),
  UNIQUE (uid, attribution_chain_id)
);

CREATE TABLE IF NOT EXISTS cf_task_feedback (
  uid TEXT NOT NULL,
  feedback_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  intervention_id TEXT,
  attribution_chain_id TEXT,
  request_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (uid, feedback_id),
  UNIQUE (uid, account_generation, request_fingerprint)
);

CREATE TABLE IF NOT EXISTS cf_task_outcomes (
  uid TEXT NOT NULL,
  outcome_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  attribution_chain_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  occurred_at INTEGER NOT NULL,
  PRIMARY KEY (uid, outcome_id),
  UNIQUE (uid, account_generation, request_fingerprint)
);

CREATE TABLE IF NOT EXISTS cf_task_context_snapshots (
  uid TEXT NOT NULL,
  device_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  snapshot_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  generated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, device_id),
  UNIQUE (uid, account_generation, snapshot_id)
);

CREATE TABLE IF NOT EXISTS cf_task_open_loop_snapshots (
  uid TEXT NOT NULL,
  device_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  snapshot_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  generated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, device_id),
  UNIQUE (uid, account_generation, snapshot_id)
);

CREATE TABLE IF NOT EXISTS cf_task_intelligence_jobs (
  uid TEXT NOT NULL,
  job_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  device_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_token TEXT,
  lease_until INTEGER,
  next_attempt_at INTEGER NOT NULL,
  last_error TEXT,
  result_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, job_id),
  UNIQUE (uid, account_generation, request_fingerprint)
);

CREATE INDEX IF NOT EXISTS cf_task_intelligence_jobs_retry_idx
  ON cf_task_intelligence_jobs(status, next_attempt_at, updated_at);

CREATE TABLE IF NOT EXISTS cf_task_llm_receipts (
  uid TEXT NOT NULL,
  receipt_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  evaluation_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  provider TEXT NOT NULL,
  model_version TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  response_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
  created_at INTEGER NOT NULL,
  PRIMARY KEY (uid, receipt_id),
  UNIQUE (uid, job_id)
);

CREATE TABLE IF NOT EXISTS cf_task_evaluations (
  uid TEXT NOT NULL,
  evaluation_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  device_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  projection_json TEXT NOT NULL,
  decisions_json TEXT NOT NULL DEFAULT '[]',
  generated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  PRIMARY KEY (uid, evaluation_id),
  UNIQUE (uid, job_id)
);

CREATE INDEX IF NOT EXISTS cf_task_evaluations_read_idx
  ON cf_task_evaluations(uid, account_generation, device_id, generated_at DESC);

-- The route-level deletion check closes the normal request path, while these
-- D1 triggers close the race with an already-admitted request or queue retry.
-- DELETE remains available to the account-deletion owner so a fenced account
-- can be purged without allowing any late task-intelligence resurrection.
CREATE TRIGGER IF NOT EXISTS adf_i_task_candidates
BEFORE INSERT ON cf_task_candidates
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_candidates
BEFORE UPDATE ON cf_task_candidates
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_interventions
BEFORE INSERT ON cf_task_interventions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_interventions
BEFORE UPDATE ON cf_task_interventions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_feedback
BEFORE INSERT ON cf_task_feedback
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_feedback
BEFORE UPDATE ON cf_task_feedback
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_outcomes
BEFORE INSERT ON cf_task_outcomes
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_outcomes
BEFORE UPDATE ON cf_task_outcomes
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_context_snapshots
BEFORE INSERT ON cf_task_context_snapshots
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_context_snapshots
BEFORE UPDATE ON cf_task_context_snapshots
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_open_loop_snapshots
BEFORE INSERT ON cf_task_open_loop_snapshots
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_open_loop_snapshots
BEFORE UPDATE ON cf_task_open_loop_snapshots
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_intelligence_jobs
BEFORE INSERT ON cf_task_intelligence_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_intelligence_jobs
BEFORE UPDATE ON cf_task_intelligence_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_llm_receipts
BEFORE INSERT ON cf_task_llm_receipts
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_llm_receipts
BEFORE UPDATE ON cf_task_llm_receipts
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_evaluations
BEFORE INSERT ON cf_task_evaluations
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_evaluations
BEFORE UPDATE ON cf_task_evaluations
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
