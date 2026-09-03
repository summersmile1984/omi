-- Reviewed historical Wrapped result promotion.
--
-- The planner remains an offline Firestore-export tool.  These tables retain
-- the exact reviewed scalar snapshot and make promotion idempotent.  They do
-- not make Firestore an input available to Workers and they never enqueue a
-- new provider generation.
CREATE TABLE IF NOT EXISTS cf_wrapped_history_review_batches (
  review_id TEXT PRIMARY KEY CHECK (length(review_id) = 36),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  entry_count INTEGER NOT NULL CHECK (entry_count > 0 AND entry_count <= 40),
  status TEXT NOT NULL CHECK (status IN ('approved', 'applied', 'revoked')),
  reviewed_at INTEGER NOT NULL CHECK (reviewed_at > 0),
  expires_at INTEGER NOT NULL CHECK (expires_at > reviewed_at),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0)
);

CREATE TABLE IF NOT EXISTS cf_wrapped_history_review_items (
  review_id TEXT NOT NULL,
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
  job_id TEXT NOT NULL CHECK (length(job_id) BETWEEN 1 AND 128),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  result_json TEXT NOT NULL,
  result_sha256 TEXT NOT NULL CHECK (length(result_sha256) = 64 AND result_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at INTEGER NOT NULL CHECK (created_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  source_row_sha256 TEXT NOT NULL CHECK (length(source_row_sha256) = 64 AND source_row_sha256 NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  PRIMARY KEY (review_id, uid, year),
  UNIQUE (review_id, uid, year)
);

CREATE INDEX IF NOT EXISTS cf_wrapped_history_review_items_lookup_idx
  ON cf_wrapped_history_review_items(uid, year, source_row_sha256, plan_hash);

CREATE TABLE IF NOT EXISTS cf_wrapped_history_applies (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
  review_id TEXT NOT NULL CHECK (length(review_id) = 36),
  job_id TEXT NOT NULL CHECK (length(job_id) BETWEEN 1 AND 128),
  source_row_sha256 TEXT NOT NULL CHECK (length(source_row_sha256) = 64 AND source_row_sha256 NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  status TEXT NOT NULL CHECK (status = 'applied'),
  applied_at INTEGER NOT NULL CHECK (applied_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  PRIMARY KEY (uid, year)
);

CREATE INDEX IF NOT EXISTS cf_wrapped_history_applies_review_idx
  ON cf_wrapped_history_applies(review_id, status, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_wrapped_history_review_items
BEFORE INSERT ON cf_wrapped_history_review_items
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_wrapped_history_review_items
BEFORE UPDATE ON cf_wrapped_history_review_items
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_wrapped_history_applies
BEFORE INSERT ON cf_wrapped_history_applies
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
     SELECT 1 FROM cf_wrapped_jobs AS w
     WHERE w.uid = NEW.uid AND w.year = NEW.year
       AND w.job_id = NEW.job_id
       AND w.source_fingerprint = (
         SELECT i.source_fingerprint FROM cf_wrapped_history_review_items AS i
         WHERE i.review_id = NEW.review_id AND i.uid = NEW.uid AND i.year = NEW.year
       )
       AND w.account_generation = NEW.account_generation
       AND w.status = 'completed'
       AND w.result_json = (
         SELECT i.result_json FROM cf_wrapped_history_review_items AS i
         WHERE i.review_id = NEW.review_id AND i.uid = NEW.uid AND i.year = NEW.year
       )
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_wrapped_history_review_items AS i
     WHERE i.review_id = NEW.review_id AND i.uid = NEW.uid AND i.year = NEW.year
       AND i.job_id = NEW.job_id
       AND i.source_row_sha256 = NEW.source_row_sha256
       AND i.plan_hash = NEW.plan_hash
   )
BEGIN
  SELECT RAISE(ABORT, 'wrapped history authority changed');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_wrapped_history_applies
BEFORE UPDATE ON cf_wrapped_history_applies
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
