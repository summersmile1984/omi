-- Operator-reviewed Archive projection import.
--
-- A review is account-scoped so account deletion can purge every control row
-- without a global/orphaned batch.  The payload is a de-identified, source-
-- bound snapshot; Workers never read Firestore or infer Archive capability.

CREATE TABLE IF NOT EXISTS cf_memory_archive_review_batches (
  review_id TEXT PRIMARY KEY CHECK (length(review_id) = 36),
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  entry_count INTEGER NOT NULL CHECK (entry_count > 0 AND entry_count <= 50),
  status TEXT NOT NULL CHECK (status IN ('approved', 'applied', 'revoked')),
  reviewed_at INTEGER NOT NULL CHECK (reviewed_at > 0),
  expires_at INTEGER NOT NULL CHECK (expires_at > reviewed_at),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  UNIQUE (uid, manifest_sha256)
);

CREATE INDEX IF NOT EXISTS cf_memory_archive_review_batches_uid_idx
  ON cf_memory_archive_review_batches(uid, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS cf_memory_archive_review_items (
  review_id TEXT NOT NULL,
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  memory_id TEXT NOT NULL CHECK (length(memory_id) BETWEEN 1 AND 256),
  import_id TEXT NOT NULL CHECK (length(import_id) = 64 AND import_id NOT GLOB '*[^0-9a-f]*'),
  source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
  source_row_sha256 TEXT NOT NULL CHECK (length(source_row_sha256) = 64 AND source_row_sha256 NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  row_json TEXT NOT NULL CHECK (length(row_json) BETWEEN 2 AND 100000),
  created_at INTEGER NOT NULL CHECK (created_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  PRIMARY KEY (review_id, uid, memory_id),
  UNIQUE (review_id, import_id)
);

CREATE INDEX IF NOT EXISTS cf_memory_archive_review_items_lookup_idx
  ON cf_memory_archive_review_items(uid, memory_id, source_row_sha256, plan_hash);

CREATE TABLE IF NOT EXISTS cf_memory_archive_applies (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  memory_id TEXT NOT NULL CHECK (length(memory_id) BETWEEN 1 AND 256),
  review_id TEXT NOT NULL CHECK (length(review_id) = 36),
  import_id TEXT NOT NULL CHECK (length(import_id) = 64 AND import_id NOT GLOB '*[^0-9a-f]*'),
  source_row_sha256 TEXT NOT NULL CHECK (length(source_row_sha256) = 64 AND source_row_sha256 NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  status TEXT NOT NULL CHECK (status = 'applied'),
  applied_at INTEGER NOT NULL CHECK (applied_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  PRIMARY KEY (uid, memory_id)
);

CREATE INDEX IF NOT EXISTS cf_memory_archive_applies_review_idx
  ON cf_memory_archive_applies(review_id, status, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_memory_archive_review_batches
BEFORE INSERT ON cf_memory_archive_review_batches
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_archive_review_batches
BEFORE UPDATE ON cf_memory_archive_review_batches
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_archive_review_items
BEFORE INSERT ON cf_memory_archive_review_items
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_archive_review_items
BEFORE UPDATE ON cf_memory_archive_review_items
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_archive_applies
BEFORE INSERT ON cf_memory_archive_applies
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
     SELECT 1 FROM cf_memory_control AS c
     WHERE c.uid = NEW.uid AND c.source = 'cloudflare_cutover_projection'
       AND c.memory_reads_enabled = 1 AND c.default_memory_grant = 1
       AND c.archive_capability = 1 AND c.account_generation = NEW.account_generation
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_memory_archive_review_items AS i
     WHERE i.review_id = NEW.review_id AND i.uid = NEW.uid
       AND i.memory_id = NEW.memory_id AND i.import_id = NEW.import_id
       AND i.source_row_sha256 = NEW.source_row_sha256 AND i.plan_hash = NEW.plan_hash
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_memory_archive_items AS i
     WHERE i.uid = NEW.uid AND i.memory_id = NEW.memory_id
       AND i.memory_tier = 'archive' AND i.status = 'active'
       AND i.processing_state = 'processed' AND i.source_state = 'active'
       AND i.account_generation = NEW.account_generation
       AND i.item_revision = (SELECT json_extract(r.row_json, '$.item_revision')
                              FROM cf_memory_archive_review_items AS r
                              WHERE r.review_id = NEW.review_id AND r.uid = NEW.uid AND r.memory_id = NEW.memory_id)
   )
BEGIN
  SELECT RAISE(ABORT, 'memory archive authority changed');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_archive_applies
BEFORE UPDATE ON cf_memory_archive_applies
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
