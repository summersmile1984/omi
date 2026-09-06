-- One immutable normalized turn owns realtime counters and desktop accounting.
-- Existing daily aggregates remain historical totals; no unprovable retroactive
-- quota event or charge is manufactured from them.
CREATE TABLE cf_realtime_usage_events (
  uid TEXT NOT NULL,
  idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 300),
  provider TEXT NOT NULL CHECK (provider IN ('openai', 'gemini', 'workers-ai')),
  model TEXT NOT NULL,
  input_text_tokens INTEGER NOT NULL CHECK (input_text_tokens >= 0),
  input_audio_tokens INTEGER NOT NULL CHECK (input_audio_tokens >= 0),
  input_cached_tokens INTEGER NOT NULL CHECK (input_cached_tokens >= 0),
  output_text_tokens INTEGER NOT NULL CHECK (output_text_tokens >= 0),
  output_audio_tokens INTEGER NOT NULL CHECK (output_audio_tokens >= 0),
  total_tokens INTEGER NOT NULL CHECK (total_tokens >= 0),
  cost_micros INTEGER NOT NULL CHECK (cost_micros >= 0),
  occurred_at INTEGER NOT NULL,
  PRIMARY KEY (uid, idempotency_key),
  CHECK (provider != 'workers-ai' OR (cost_micros = 0 AND model = ''))
);

CREATE TRIGGER IF NOT EXISTS adf_i_realtime_usage_events
BEFORE INSERT ON cf_realtime_usage_events
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_realtime_usage_events
BEFORE UPDATE ON cf_realtime_usage_events
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER realtime_usage_events_immutable
BEFORE UPDATE ON cf_realtime_usage_events
BEGIN SELECT RAISE(ABORT, 'realtime usage event is immutable'); END;

CREATE TRIGGER realtime_usage_event_admission
BEFORE INSERT ON cf_realtime_usage_events
WHEN NEW.provider != 'workers-ai' AND NOT EXISTS (
  SELECT 1 FROM cf_chat_quota_events
  WHERE uid = NEW.uid AND idempotency_key = NEW.idempotency_key
    AND source = 'desktop_realtime_turn'
)
BEGIN SELECT RAISE(ABORT, 'realtime quota reservation required'); END;

CREATE TRIGGER realtime_usage_event_projection
AFTER INSERT ON cf_realtime_usage_events
BEGIN
  INSERT INTO cf_realtime_usage (
    uid, usage_date, input_text_tokens, input_audio_tokens, input_cached_tokens,
    output_text_tokens, output_audio_tokens, total_tokens, cost_micros, call_count, updated_at
  ) VALUES (
    NEW.uid, date(NEW.occurred_at, 'unixepoch'), NEW.input_text_tokens,
    NEW.input_audio_tokens, NEW.input_cached_tokens, NEW.output_text_tokens,
    NEW.output_audio_tokens, NEW.total_tokens, NEW.cost_micros, 1, NEW.occurred_at
  ) ON CONFLICT(uid, usage_date) DO UPDATE SET
    input_text_tokens = input_text_tokens + excluded.input_text_tokens,
    input_audio_tokens = input_audio_tokens + excluded.input_audio_tokens,
    input_cached_tokens = input_cached_tokens + excluded.input_cached_tokens,
    output_text_tokens = output_text_tokens + excluded.output_text_tokens,
    output_audio_tokens = output_audio_tokens + excluded.output_audio_tokens,
    total_tokens = total_tokens + excluded.total_tokens,
    cost_micros = cost_micros + excluded.cost_micros,
    call_count = call_count + 1, updated_at = excluded.updated_at;

  INSERT INTO cf_llm_usage_daily (
    uid, usage_day, usage_kind, feature, model, account, input_tokens,
    output_tokens, cache_read_tokens, cache_write_tokens, total_tokens,
    cost_usd, call_count, updated_at
  ) SELECT
    NEW.uid, date(NEW.occurred_at, 'unixepoch'), 'bucket', 'desktop_chat', '',
    'desktop_chat_realtime', NEW.input_text_tokens + NEW.input_audio_tokens,
    NEW.output_text_tokens + NEW.output_audio_tokens, NEW.input_cached_tokens, 0,
    NEW.total_tokens, NEW.cost_micros / 1000000.0, 1, NEW.occurred_at
  WHERE NEW.provider != 'workers-ai'
  ON CONFLICT(uid, usage_day, usage_kind, feature, model, account) DO UPDATE SET
    input_tokens = input_tokens + excluded.input_tokens,
    output_tokens = output_tokens + excluded.output_tokens,
    cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
    total_tokens = total_tokens + excluded.total_tokens,
    cost_usd = cost_usd + excluded.cost_usd,
    call_count = call_count + 1, updated_at = excluded.updated_at;

  UPDATE cf_chat_quota_events
  SET cost_usd = NEW.cost_micros / 1000000.0,
    prompt_tokens = NEW.input_text_tokens + NEW.input_audio_tokens,
    completion_tokens = NEW.output_text_tokens + NEW.output_audio_tokens,
    model = NEW.model, settled_at = NEW.occurred_at
  WHERE uid = NEW.uid AND idempotency_key = NEW.idempotency_key
    AND source = 'desktop_realtime_turn' AND settled_at IS NULL
    AND NEW.provider != 'workers-ai';
END;
