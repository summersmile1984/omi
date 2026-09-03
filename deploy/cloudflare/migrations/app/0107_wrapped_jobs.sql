-- Durable Cloudflare authority for yearly Wrapped status, generation jobs and
-- provider results.  The result is intentionally kept in D1: clients poll the
-- same row that owns admission and deletion fencing.
CREATE TABLE IF NOT EXISTS cf_wrapped_jobs (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
  job_id TEXT NOT NULL CHECK (length(job_id) BETWEEN 1 AND 128),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_token TEXT,
  lease_until INTEGER,
  next_attempt_at INTEGER NOT NULL,
  result_json TEXT,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, year),
  UNIQUE (uid, job_id),
  UNIQUE (uid, request_fingerprint)
);

CREATE INDEX IF NOT EXISTS cf_wrapped_jobs_ready_idx
  ON cf_wrapped_jobs(status, next_attempt_at, lease_until, updated_at);

CREATE INDEX IF NOT EXISTS cf_wrapped_jobs_uid_idx
  ON cf_wrapped_jobs(uid, year);

-- Keep Wrapped generation behind the same account deletion fence as every
-- other D1 product surface.  DELETE remains available to the deletion owner.
CREATE TRIGGER IF NOT EXISTS adf_i_wrapped_jobs
BEFORE INSERT ON cf_wrapped_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_wrapped_jobs
BEFORE UPDATE ON cf_wrapped_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
