CREATE TABLE IF NOT EXISTS cf_llm_usage_daily (
  uid TEXT NOT NULL,
  usage_day TEXT NOT NULL CHECK (length(usage_day) = 10),
  usage_kind TEXT NOT NULL CHECK (usage_kind IN ('feature', 'bucket')),
  feature TEXT NOT NULL CHECK (length(feature) BETWEEN 1 AND 100),
  model TEXT NOT NULL DEFAULT '' CHECK (length(model) <= 200),
  account TEXT NOT NULL DEFAULT 'omi' CHECK (length(account) <= 100),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  cache_read_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
  cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
  total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
  cost_usd REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  call_count INTEGER NOT NULL DEFAULT 0 CHECK (call_count >= 0),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, usage_day, usage_kind, feature, model, account)
);

CREATE INDEX IF NOT EXISTS cf_llm_usage_daily_uid_day_kind_idx
  ON cf_llm_usage_daily(uid, usage_day, usage_kind);

CREATE INDEX IF NOT EXISTS cf_llm_usage_daily_uid_day_cost_idx
  ON cf_llm_usage_daily(uid, usage_day, cost_usd);

-- Settled Cloudflare chat exchanges already have authoritative provider usage
-- in cf_chat_quota_events. Materialize them before installing the trigger so
-- the daily ledger is complete without replaying any provider request.
INSERT INTO cf_llm_usage_daily (
  uid, usage_day, usage_kind, feature, model, account,
  input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
  total_tokens, cost_usd, call_count, updated_at
)
SELECT
  uid,
  date(occurred_at, 'unixepoch'),
  'feature',
  'chat',
  COALESCE(NULLIF(model, ''), 'unknown'),
  'omi',
  SUM(COALESCE(prompt_tokens, 0)),
  SUM(COALESCE(completion_tokens, 0)),
  0,
  0,
  SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)),
  SUM(COALESCE(cost_usd, 0)),
  COUNT(*),
  MAX(settled_at)
FROM cf_chat_quota_events
WHERE source = 'v2_messages' AND settled_at IS NOT NULL
GROUP BY uid, date(occurred_at, 'unixepoch'), COALESCE(NULLIF(model, ''), 'unknown')
ON CONFLICT(uid, usage_day, usage_kind, feature, model, account) DO UPDATE SET
  input_tokens = excluded.input_tokens,
  output_tokens = excluded.output_tokens,
  cache_read_tokens = excluded.cache_read_tokens,
  cache_write_tokens = excluded.cache_write_tokens,
  total_tokens = excluded.total_tokens,
  cost_usd = excluded.cost_usd,
  call_count = excluded.call_count,
  updated_at = excluded.updated_at;

-- The NULL -> settled transition is the exactly-once boundary. A retry of the
-- settlement UPDATE cannot increment this aggregate twice because the trigger
-- predicate is false after the first successful transition.
CREATE TRIGGER IF NOT EXISTS llm_usage_from_chat_settlement
AFTER UPDATE OF settled_at ON cf_chat_quota_events
WHEN OLD.settled_at IS NULL
  AND NEW.settled_at IS NOT NULL
  AND NEW.source = 'v2_messages'
BEGIN
  INSERT INTO cf_llm_usage_daily (
    uid, usage_day, usage_kind, feature, model, account,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    total_tokens, cost_usd, call_count, updated_at
  ) VALUES (
    NEW.uid,
    date(NEW.occurred_at, 'unixepoch'),
    'feature',
    'chat',
    COALESCE(NULLIF(NEW.model, ''), 'unknown'),
    'omi',
    COALESCE(NEW.prompt_tokens, 0),
    COALESCE(NEW.completion_tokens, 0),
    0,
    0,
    COALESCE(NEW.prompt_tokens, 0) + COALESCE(NEW.completion_tokens, 0),
    COALESCE(NEW.cost_usd, 0),
    1,
    NEW.settled_at
  )
  ON CONFLICT(uid, usage_day, usage_kind, feature, model, account) DO UPDATE SET
    input_tokens = input_tokens + excluded.input_tokens,
    output_tokens = output_tokens + excluded.output_tokens,
    cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
    cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
    total_tokens = total_tokens + excluded.total_tokens,
    cost_usd = cost_usd + excluded.cost_usd,
    call_count = call_count + 1,
    updated_at = excluded.updated_at;
END;

CREATE TRIGGER IF NOT EXISTS adf_i_llm_usage_daily
BEFORE INSERT ON cf_llm_usage_daily
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_llm_usage_daily
BEFORE UPDATE ON cf_llm_usage_daily
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
