-- Cloudflare-owned, identity-neutral Hume callback result authority.
--
-- The legacy callback finds uid/conversation through a Firestore task whose
-- request_id equals the provider job id.  That task projection is not present
-- in App D1, so this table deliberately records no guessed identity.  A
-- future attested task projection may add a separate mapping step; until then
-- the normalized provider result is useful and auditable without creating
-- false conversation/task/notification side effects.
CREATE TABLE IF NOT EXISTS cf_hume_webhook_results (
  event_id TEXT PRIMARY KEY NOT NULL,
  job_id TEXT NOT NULL UNIQUE CHECK (length(job_id) BETWEEN 1 AND 256),
  callback_status TEXT NOT NULL CHECK (callback_status IN ('COMPLETED', 'FAILED')),
  mapping_status TEXT NOT NULL DEFAULT 'unmapped'
    CHECK (mapping_status IN ('unmapped', 'attested')),
  processing_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (processing_status IN ('pending', 'completed', 'failed')),
  prediction_count INTEGER NOT NULL DEFAULT 0 CHECK (prediction_count >= 0),
  predictions_json TEXT NOT NULL DEFAULT '[]'
    CHECK (length(predictions_json) <= 524288 AND json_valid(predictions_json)),
  result_json TEXT CHECK (result_json IS NULL OR (length(result_json) <= 524288 AND json_valid(result_json))),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 256),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  processed_at INTEGER,
  FOREIGN KEY (event_id) REFERENCES cf_hume_webhook_events(event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_hume_webhook_results_status_idx
  ON cf_hume_webhook_results(processing_status, updated_at);
