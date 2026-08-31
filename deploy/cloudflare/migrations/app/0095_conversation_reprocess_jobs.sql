-- Reprocessing uses the same durable conversation lifecycle as finalization.
-- Keep the lease/result table shared so status polling and account-deletion
-- fencing remain identical, while recording the operation for the processor.
ALTER TABLE cf_conversation_finalization_jobs ADD COLUMN operation TEXT NOT NULL DEFAULT 'finalize'
  CHECK (operation IN ('finalize', 'reprocess'));
ALTER TABLE cf_conversation_finalization_jobs ADD COLUMN language_code TEXT;
ALTER TABLE cf_conversation_finalization_jobs ADD COLUMN app_id TEXT;

CREATE INDEX IF NOT EXISTS cf_conversation_finalization_jobs_operation_idx
  ON cf_conversation_finalization_jobs(operation, status, next_attempt_at, updated_at);
