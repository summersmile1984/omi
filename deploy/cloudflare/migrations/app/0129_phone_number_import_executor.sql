-- Reviewed historical phone-number import receipts and apply markers.
--
-- The reconciliation ledger remains an immutable, operator-generated plan. A
-- separate review batch is required before an executor can promote any row to
-- cf_phone_numbers. Apply markers make the promotion idempotent without
-- changing the original planner's status constraint.

ALTER TABLE cf_phone_number_import_ledger
  ADD COLUMN manifest_sha256 TEXT
  CHECK (manifest_sha256 IS NULL OR (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'));

CREATE INDEX IF NOT EXISTS cf_phone_number_import_ledger_manifest_idx
  ON cf_phone_number_import_ledger(manifest_sha256, uid, import_id);

CREATE TABLE IF NOT EXISTS cf_phone_number_import_review_batches (
  review_id TEXT PRIMARY KEY CHECK (length(review_id) = 36),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  entry_count INTEGER NOT NULL CHECK (entry_count > 0 AND entry_count <= 100),
  status TEXT NOT NULL CHECK (status IN ('approved', 'applied', 'revoked')),
  reviewed_at INTEGER NOT NULL CHECK (reviewed_at > 0),
  expires_at INTEGER NOT NULL CHECK (expires_at > reviewed_at),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0)
);

CREATE TABLE IF NOT EXISTS cf_phone_number_import_review_items (
  review_id TEXT NOT NULL,
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  import_id TEXT NOT NULL CHECK (length(import_id) = 64 AND import_id NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  PRIMARY KEY (review_id, uid, import_id),
  UNIQUE (review_id, import_id)
);

CREATE INDEX IF NOT EXISTS cf_phone_number_import_review_items_lookup_idx
  ON cf_phone_number_import_review_items(uid, import_id, plan_hash);

CREATE TABLE IF NOT EXISTS cf_phone_number_import_applies (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  import_id TEXT NOT NULL CHECK (length(import_id) = 64 AND import_id NOT GLOB '*[^0-9a-f]*'),
  review_id TEXT NOT NULL CHECK (length(review_id) = 36),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  phone_number_id TEXT NOT NULL CHECK (length(phone_number_id) BETWEEN 1 AND 128),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  status TEXT NOT NULL CHECK (status = 'applied'),
  applied_at INTEGER NOT NULL CHECK (applied_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  PRIMARY KEY (uid, import_id)
);

CREATE INDEX IF NOT EXISTS cf_phone_number_import_applies_review_idx
  ON cf_phone_number_import_applies(review_id, status, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_phone_number_import_review_items
BEFORE INSERT ON cf_phone_number_import_review_items
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_phone_number_import_review_items
BEFORE UPDATE ON cf_phone_number_import_review_items
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_phone_number_import_applies
BEFORE INSERT ON cf_phone_number_import_applies
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
   OR NOT EXISTS (
     SELECT 1 FROM cf_phone_numbers AS p
     WHERE p.uid = NEW.uid AND p.id = NEW.phone_number_id
       AND p.account_generation = NEW.account_generation
       AND p.phone_number_hash = (
         SELECT l.phone_number_hash FROM cf_phone_number_import_ledger AS l
         WHERE l.uid = NEW.uid AND l.import_id = NEW.import_id
           AND l.plan_hash = NEW.plan_hash
       )
       AND p.phone_number_ciphertext = (
         SELECT l.phone_number_ciphertext FROM cf_phone_number_import_ledger AS l
         WHERE l.uid = NEW.uid AND l.import_id = NEW.import_id
           AND l.plan_hash = NEW.plan_hash
       )
       AND (p.friendly_name = (
         SELECT l.friendly_name FROM cf_phone_number_import_ledger AS l
         WHERE l.uid = NEW.uid AND l.import_id = NEW.import_id
           AND l.plan_hash = NEW.plan_hash
       ) OR (p.friendly_name IS NULL AND NOT EXISTS (
         SELECT 1 FROM cf_phone_number_import_ledger AS l
         WHERE l.uid = NEW.uid AND l.import_id = NEW.import_id
           AND l.plan_hash = NEW.plan_hash AND l.friendly_name IS NOT NULL
       )))
       AND (p.twilio_sid = (
         SELECT l.twilio_sid FROM cf_phone_number_import_ledger AS l
         WHERE l.uid = NEW.uid AND l.import_id = NEW.import_id
           AND l.plan_hash = NEW.plan_hash
       ) OR (p.twilio_sid IS NULL AND NOT EXISTS (
         SELECT 1 FROM cf_phone_number_import_ledger AS l
         WHERE l.uid = NEW.uid AND l.import_id = NEW.import_id
           AND l.plan_hash = NEW.plan_hash AND l.twilio_sid IS NOT NULL
       )))
       AND p.verified_at = (
         SELECT l.verified_at FROM cf_phone_number_import_ledger AS l
         WHERE l.uid = NEW.uid AND l.import_id = NEW.import_id
           AND l.plan_hash = NEW.plan_hash
       )
       AND p.is_primary = (
         SELECT l.is_primary FROM cf_phone_number_import_ledger AS l
         WHERE l.uid = NEW.uid AND l.import_id = NEW.import_id
           AND l.plan_hash = NEW.plan_hash
       )
       AND p.created_at = (
         SELECT l.created_at FROM cf_phone_number_import_ledger AS l
         WHERE l.uid = NEW.uid AND l.import_id = NEW.import_id
           AND l.plan_hash = NEW.plan_hash
       )
       AND p.updated_at = (
         SELECT l.updated_at FROM cf_phone_number_import_ledger AS l
         WHERE l.uid = NEW.uid AND l.import_id = NEW.import_id
           AND l.plan_hash = NEW.plan_hash
       )
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_account_cutover AS c
     WHERE c.uid = NEW.uid AND c.state = 'new'
       AND c.checkpoint_phase = 'completed'
       AND c.destination_backend_bound = 1
       AND c.account_generation = NEW.account_generation
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_phone_number_import_review_items AS i
     WHERE i.review_id = NEW.review_id AND i.uid = NEW.uid
       AND i.import_id = NEW.import_id AND i.plan_hash = NEW.plan_hash
   )
BEGIN SELECT RAISE(ABORT, 'phone import authority changed'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_phone_number_import_applies
BEFORE UPDATE ON cf_phone_number_import_applies
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
