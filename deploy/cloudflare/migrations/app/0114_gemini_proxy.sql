-- Cloudflare Gemini desktop-proxy admission and usage authority.
--
-- This migration covers the direct AI Studio adapter only. Vertex ADC/PT
-- traffic remains disabled until a Cloudflare service-identity contract is
-- reviewed. The request counter is incremented by the receipt trigger, so the
-- conditional INSERT and quota-window update are atomic inside one D1 batch.
CREATE TABLE IF NOT EXISTS cf_gemini_quota_windows (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  window_kind TEXT NOT NULL CHECK (window_kind = 'daily'),
  window_start INTEGER NOT NULL CHECK (window_start >= 0),
  request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
  PRIMARY KEY (uid, window_kind, window_start)
);

CREATE TABLE IF NOT EXISTS cf_gemini_usage_receipts (
  request_id TEXT PRIMARY KEY NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
  action TEXT NOT NULL CHECK (action IN ('generateContent', 'streamGenerateContent', 'embedContent', 'batchEmbedContents')),
  credential_source TEXT NOT NULL CHECK (credential_source IN ('byok', 'server')),
  provider TEXT NOT NULL CHECK (provider = 'ai_studio'),
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

CREATE INDEX IF NOT EXISTS cf_gemini_usage_receipts_uid_created_idx
  ON cf_gemini_usage_receipts(uid, created_at DESC, request_id);

CREATE TRIGGER IF NOT EXISTS adf_i_gemini_quota_windows
BEFORE INSERT ON cf_gemini_quota_windows
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_gemini_quota_windows
BEFORE UPDATE ON cf_gemini_quota_windows
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_gemini_usage_receipts
BEFORE INSERT ON cf_gemini_usage_receipts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_gemini_usage_receipts
BEFORE UPDATE ON cf_gemini_usage_receipts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS gemini_receipt_increments_daily
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
