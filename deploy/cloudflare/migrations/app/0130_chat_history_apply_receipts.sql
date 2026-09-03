-- Receipts for the explicitly reviewed chat-history apply batch.
--
-- The Jobs route stores only the content-bound receipt.  The reviewed plan
-- payload is accepted over an authenticated, bounded request and is never
-- retained as an operator blob.  Canonical session/message rows retain the
-- 0128 source markers and remain the data authority.
CREATE TABLE IF NOT EXISTS cf_chat_history_apply_receipts (
  batch_id TEXT NOT NULL CHECK (length(batch_id) = 64 AND batch_id NOT GLOB '*[^0-9a-f]*'),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  import_id TEXT NOT NULL CHECK (length(import_id) = 64 AND import_id NOT GLOB '*[^0-9a-f]*'),
  entity_kind TEXT NOT NULL CHECK (entity_kind IN ('session', 'message')),
  entity_id TEXT NOT NULL CHECK (length(entity_id) BETWEEN 1 AND 256),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  source_row_sha256 TEXT NOT NULL CHECK (length(source_row_sha256) = 64 AND source_row_sha256 NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK (status = 'applied'),
  applied_at INTEGER NOT NULL CHECK (applied_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  PRIMARY KEY (uid, import_id),
  UNIQUE (batch_id, uid, import_id)
);

CREATE INDEX IF NOT EXISTS cf_chat_history_apply_receipts_batch_idx
  ON cf_chat_history_apply_receipts(batch_id, status, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_chat_history_apply_receipts
BEFORE INSERT ON cf_chat_history_apply_receipts
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
     SELECT 1 FROM cf_chat_history_import_ledger AS l
     WHERE l.uid = NEW.uid AND l.import_id = NEW.import_id
       AND l.entity_kind = NEW.entity_kind AND l.entity_id = NEW.entity_id
       AND l.account_generation = NEW.account_generation
       AND l.source_row_sha256 = NEW.source_row_sha256
       AND l.plan_hash = NEW.plan_hash AND l.status = 'applied'
   )
   OR NOT (
     (NEW.entity_kind = 'session' AND EXISTS (
       SELECT 1 FROM cf_chat_sessions AS s
       WHERE s.uid = NEW.uid AND s.id = NEW.entity_id
         AND s.history_import_id = NEW.import_id
         AND s.history_source_row_sha256 = NEW.source_row_sha256
         AND s.history_account_generation = NEW.account_generation
     ))
     OR (NEW.entity_kind = 'message' AND EXISTS (
       SELECT 1 FROM cf_chat_messages AS m
       WHERE m.uid = NEW.uid AND m.id = NEW.entity_id
         AND m.history_import_id = NEW.import_id
         AND m.history_source_row_sha256 = NEW.source_row_sha256
         AND m.history_account_generation = NEW.account_generation
     ))
   )
BEGIN
  SELECT RAISE(ABORT, 'chat history apply authority changed');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_history_apply_receipts
BEFORE UPDATE ON cf_chat_history_apply_receipts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
