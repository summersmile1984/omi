ALTER TABLE cf_chat_quota_events
  ADD COLUMN cost_usd REAL CHECK (cost_usd IS NULL OR cost_usd >= 0);

ALTER TABLE cf_chat_quota_events
  ADD COLUMN prompt_tokens INTEGER CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0);

ALTER TABLE cf_chat_quota_events
  ADD COLUMN completion_tokens INTEGER CHECK (completion_tokens IS NULL OR completion_tokens >= 0);

ALTER TABLE cf_chat_quota_events
  ADD COLUMN model TEXT;

ALTER TABLE cf_chat_quota_events
  ADD COLUMN settled_at INTEGER;

CREATE INDEX IF NOT EXISTS cf_chat_quota_events_uid_month_cost_idx
  ON cf_chat_quota_events(uid, occurred_at, cost_usd);
