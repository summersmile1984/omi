-- Extend the Gemini usage authority for the explicit Vertex service-account
-- provider.  0114 is already applied in staging, so changing its CHECK in
-- place would not update existing D1 databases.  Rebuild only the receipt
-- table and preserve all existing rows; quota windows and deletion fences are
-- unchanged.
DROP TRIGGER IF EXISTS adf_i_gemini_usage_receipts;
DROP TRIGGER IF EXISTS adf_u_gemini_usage_receipts;
DROP TRIGGER IF EXISTS gemini_receipt_increments_daily;

ALTER TABLE cf_gemini_usage_receipts RENAME TO cf_gemini_usage_receipts_v114;

CREATE TABLE cf_gemini_usage_receipts (
  request_id TEXT PRIMARY KEY NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
  action TEXT NOT NULL CHECK (action IN ('generateContent', 'streamGenerateContent', 'embedContent', 'batchEmbedContents')),
  credential_source TEXT NOT NULL CHECK (credential_source IN ('byok', 'server')),
  provider TEXT NOT NULL CHECK (provider IN ('ai_studio', 'vertex_ai')),
  status TEXT NOT NULL CHECK (status IN ('reserved', 'success', 'rejected', 'failed')),
  prompt_tokens INTEGER CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
  output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
  total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
  cached_input_tokens INTEGER CHECK (cached_input_tokens IS NULL OR cached_input_tokens >= 0),
  reasoning_tokens INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
  traffic_type TEXT CHECK (traffic_type IS NULL OR length(traffic_type) BETWEEN 1 AND 64),
  estimated_cost_micros INTEGER CHECK (estimated_cost_micros IS NULL OR estimated_cost_micros >= 0),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  completed_at INTEGER CHECK (completed_at IS NULL OR completed_at >= created_at),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 512)
);

INSERT INTO cf_gemini_usage_receipts (
  request_id, uid, model, action, credential_source, provider, status,
  prompt_tokens, output_tokens, total_tokens, cached_input_tokens,
  reasoning_tokens, traffic_type, estimated_cost_micros, account_generation,
  created_at, completed_at, last_error
)
SELECT
  request_id, uid, model, action, credential_source, provider, status,
  prompt_tokens, output_tokens, total_tokens, cached_input_tokens,
  reasoning_tokens, traffic_type, estimated_cost_micros, account_generation,
  created_at, completed_at, last_error
FROM cf_gemini_usage_receipts_v114;

DROP TABLE cf_gemini_usage_receipts_v114;

CREATE INDEX IF NOT EXISTS cf_gemini_usage_receipts_uid_created_idx
  ON cf_gemini_usage_receipts(uid, created_at DESC, request_id);

CREATE TRIGGER adf_i_gemini_usage_receipts
BEFORE INSERT ON cf_gemini_usage_receipts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER adf_u_gemini_usage_receipts
BEFORE UPDATE ON cf_gemini_usage_receipts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER gemini_receipt_increments_daily
AFTER INSERT ON cf_gemini_usage_receipts
WHEN NEW.status = 'reserved'
BEGIN
  UPDATE cf_gemini_quota_windows
  SET request_count = request_count + 1,
      updated_at = NEW.created_at
  WHERE uid = NEW.uid
    AND window_kind = 'daily'
    AND window_start = (NEW.created_at / 86400) * 86400;
END;
