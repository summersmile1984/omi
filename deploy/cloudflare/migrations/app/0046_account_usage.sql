CREATE TABLE IF NOT EXISTS cf_usage_sources (
  uid TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('conversation', 'memory')),
  source_id TEXT NOT NULL,
  occurred_at INTEGER NOT NULL,
  transcription_seconds INTEGER NOT NULL DEFAULT 0 CHECK (transcription_seconds >= 0),
  words_transcribed INTEGER NOT NULL DEFAULT 0 CHECK (words_transcribed >= 0),
  insights_gained INTEGER NOT NULL DEFAULT 0 CHECK (insights_gained >= 0),
  memories_created INTEGER NOT NULL DEFAULT 0 CHECK (memories_created >= 0),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, source_kind, source_id)
);

CREATE INDEX IF NOT EXISTS cf_usage_sources_uid_occurred_idx
  ON cf_usage_sources(uid, occurred_at);

CREATE TABLE IF NOT EXISTS cf_user_subscriptions (
  uid TEXT PRIMARY KEY,
  plan TEXT NOT NULL DEFAULT 'basic' CHECK (plan IN (
    'basic', 'unlimited', 'plus', 'unlimited_v2', 'operator', 'architect'
  )),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
  current_period_start INTEGER,
  current_period_end INTEGER,
  stripe_subscription_id TEXT,
  current_price_id TEXT,
  features_json TEXT NOT NULL DEFAULT '[]',
  cancel_at_period_end INTEGER NOT NULL DEFAULT 0 CHECK (cancel_at_period_end IN (0, 1)),
  show_subscription_ui INTEGER NOT NULL DEFAULT 0 CHECK (show_subscription_ui IN (0, 1)),
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cf_subscription_prices (
  id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL CHECK (plan_id IN (
    'unlimited', 'plus', 'unlimited_v2', 'operator', 'architect'
  )),
  title TEXT NOT NULL,
  description TEXT,
  subtitle TEXT,
  eyebrow TEXT,
  price_string TEXT NOT NULL,
  interval TEXT NOT NULL CHECK (interval IN ('month', 'year')),
  unit_amount INTEGER NOT NULL CHECK (unit_amount >= 0),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_subscription_prices_plan_active_idx
  ON cf_subscription_prices(plan_id, active, interval);

-- Preserve usage already projected before this migration. New writes keep the
-- rows current through the same atomic D1 batches as their source records.
INSERT OR IGNORE INTO cf_usage_sources (
  uid, source_kind, source_id, occurred_at, transcription_seconds,
  words_transcribed, insights_gained, memories_created, updated_at
)
SELECT
  uid,
  'memory',
  id,
  created_at,
  0,
  0,
  0,
  1,
  updated_at
FROM cf_memories;

INSERT OR IGNORE INTO cf_usage_sources (
  uid, source_kind, source_id, occurred_at, transcription_seconds,
  words_transcribed, insights_gained, memories_created, updated_at
)
SELECT
  c.uid,
  'conversation',
  c.id,
  COALESCE(c.finished_at, c.created_at),
  CASE
    WHEN c.started_at IS NULL OR c.finished_at IS NULL OR c.finished_at < c.started_at THEN 0
    ELSE MIN(c.finished_at - c.started_at, 604800)
  END,
  COALESCE((
    SELECT SUM(
      CASE
        WHEN TRIM(COALESCE(json_extract(segment.value, '$.text'), '')) = '' THEN 0
        ELSE LENGTH(TRIM(json_extract(segment.value, '$.text')))
          - LENGTH(REPLACE(TRIM(json_extract(segment.value, '$.text')), ' ', '')) + 1
      END
    )
    FROM json_each(c.transcript_segments_json) AS segment
  ), 0),
  COALESCE(json_array_length(json_extract(c.structured_json, '$.action_items')), 0)
    + COALESCE(json_array_length(json_extract(c.structured_json, '$.events')), 0)
    + COALESCE(json_array_length(c.apps_results_json), 0),
  0,
  COALESCE(c.updated_at, c.created_at)
FROM cf_conversations AS c
WHERE c.discarded = 0;
