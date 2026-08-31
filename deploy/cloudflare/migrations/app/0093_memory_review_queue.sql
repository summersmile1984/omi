-- D1 authority for canonical memory conflicts.  The candidate and source
-- snapshot are stored together so a review can be projected without reading
-- the legacy Firestore queue.  Source revisions use the canonical memory's
-- updated_at value; the content hash makes the projection fail closed when
-- a row changes between enqueue and review.
CREATE TABLE IF NOT EXISTS cf_memory_review_queue (
  uid TEXT NOT NULL,
  review_id TEXT NOT NULL,
  fact_id TEXT NOT NULL,
  candidate_json TEXT NOT NULL,
  conflict_with_json TEXT NOT NULL DEFAULT '[]',
  referenced_memory_ids_json TEXT NOT NULL DEFAULT '[]',
  veracity REAL,
  impact REAL NOT NULL DEFAULT 0.5,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
    'pending', 'pending_review', 'accepted', 'rejected', 'dropped',
    'tombstoned'
  )),
  authority TEXT NOT NULL DEFAULT 'canonical_memory' CHECK (authority = 'canonical_memory'),
  previous_status TEXT,
  source_commit_id TEXT NOT NULL,
  source_item_revision INTEGER NOT NULL CHECK (source_item_revision > 0),
  source_content_hash TEXT NOT NULL,
  source_short_term_id TEXT,
  permitted_uses_json TEXT NOT NULL DEFAULT '["answers_with_disclaimer"]',
  reason TEXT,
  decision TEXT,
  resolution_commit_id TEXT,
  correction_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  resolved_at INTEGER,
  PRIMARY KEY (uid, review_id)
);

CREATE INDEX IF NOT EXISTS cf_memory_review_queue_status_idx
  ON cf_memory_review_queue(uid, status, impact DESC, created_at DESC, review_id DESC);

CREATE INDEX IF NOT EXISTS cf_memory_review_queue_fact_idx
  ON cf_memory_review_queue(uid, fact_id, status);

-- Keep the queue inside the same account-deletion fence as cf_memories.  The
-- Jobs deletion owner still purges the row explicitly via its residual list.
CREATE TRIGGER IF NOT EXISTS adf_i_memory_review_queue
BEFORE INSERT ON cf_memory_review_queue
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_review_queue
BEFORE UPDATE ON cf_memory_review_queue
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
