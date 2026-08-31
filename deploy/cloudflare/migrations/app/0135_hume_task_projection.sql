-- Reviewed Hume task authority projection.
--
-- Legacy callback ownership is resolved by the Firestore task where
-- action=requested Hume measurement and request_id=provider job_id.  The
-- callback must not infer an owner from provider data.  This table is the
-- reviewed, destination-bound projection of that relationship and is only
-- writable through the protected Jobs apply seam.
ALTER TABLE cf_hume_webhook_results ADD COLUMN mapped_uid TEXT;
ALTER TABLE cf_hume_webhook_results ADD COLUMN mapped_conversation_id TEXT;
ALTER TABLE cf_hume_webhook_results ADD COLUMN mapped_account_generation INTEGER;

CREATE TABLE IF NOT EXISTS cf_hume_task_projections (
  request_id TEXT PRIMARY KEY NOT NULL CHECK (length(request_id) BETWEEN 1 AND 256),
  task_id TEXT NOT NULL UNIQUE CHECK (length(task_id) BETWEEN 1 AND 128),
  action TEXT NOT NULL CHECK (action = 'hume_mersure_user_expression'),
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  conversation_id TEXT NOT NULL CHECK (length(conversation_id) BETWEEN 1 AND 256),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  source_export_sha256 TEXT NOT NULL CHECK (length(source_export_sha256) = 64 AND source_export_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_row_sha256 TEXT NOT NULL CHECK (length(source_row_sha256) = 64 AND source_row_sha256 NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  review_id TEXT NOT NULL CHECK (length(review_id) = 36),
  created_at INTEGER NOT NULL CHECK (created_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  FOREIGN KEY (uid, conversation_id) REFERENCES cf_conversations(uid, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_hume_task_projections_uid_conversation_idx
  ON cf_hume_task_projections(uid, conversation_id, account_generation);

-- A projection is destination-bound evidence, not a generic provider lookup.
-- The account must already be Cloudflare-owned at the exact generation and
-- the conversation must exist before a reviewed mapping can be admitted.
CREATE TRIGGER IF NOT EXISTS adf_i_hume_task_projections
BEFORE INSERT ON cf_hume_task_projections
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
     SELECT 1 FROM cf_conversations AS c
     WHERE c.uid = NEW.uid AND c.id = NEW.conversation_id
   )
BEGIN
  SELECT RAISE(ABORT, 'hume task projection authority changed');
END;
-- Projection evidence is immutable. Account deletion purges it with the
-- conversation; no late update may retarget a provider job to a new owner or
-- generation.
CREATE TRIGGER IF NOT EXISTS adf_u_hume_task_projections
BEFORE UPDATE ON cf_hume_task_projections
BEGIN
  SELECT RAISE(ABORT, 'hume task projection immutable');
END;

CREATE TRIGGER IF NOT EXISTS validate_hume_result_mapping
BEFORE UPDATE OF mapping_status, mapped_uid, mapped_conversation_id, mapped_account_generation
ON cf_hume_webhook_results
WHEN NEW.mapping_status = 'attested'
 AND (
   NEW.mapped_uid IS NULL OR NEW.mapped_conversation_id IS NULL OR NEW.mapped_account_generation IS NULL
   OR EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.mapped_uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.mapped_uid)
   OR NOT EXISTS (
     SELECT 1 FROM cf_hume_task_projections AS p
     WHERE p.request_id = NEW.job_id
       AND p.uid = NEW.mapped_uid
       AND p.conversation_id = NEW.mapped_conversation_id
       AND p.account_generation = NEW.mapped_account_generation
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_account_cutover AS c
     WHERE c.uid = NEW.mapped_uid AND c.state = 'new'
       AND c.checkpoint_phase = 'completed'
       AND c.destination_backend_bound = 1
       AND c.account_generation = NEW.mapped_account_generation
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_conversations AS c
     WHERE c.uid = NEW.mapped_uid AND c.id = NEW.mapped_conversation_id
   )
 )
BEGIN
  SELECT RAISE(ABORT, 'hume result mapping authority changed');
END;

CREATE TRIGGER IF NOT EXISTS validate_hume_unmapped_result
BEFORE UPDATE OF mapping_status, mapped_uid, mapped_conversation_id, mapped_account_generation
ON cf_hume_webhook_results
WHEN NEW.mapping_status = 'unmapped'
 AND (NEW.mapped_uid IS NOT NULL OR NEW.mapped_conversation_id IS NOT NULL OR NEW.mapped_account_generation IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'hume unmapped result carries identity');
END;
