ALTER TABLE cf_jobs ADD COLUMN result_json TEXT;
ALTER TABLE cf_jobs ADD COLUMN idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS cf_jobs_uid_kind_idempotency_idx
  ON cf_jobs(uid, kind, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
