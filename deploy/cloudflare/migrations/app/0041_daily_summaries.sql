CREATE TABLE IF NOT EXISTS cf_daily_summaries (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  date TEXT NOT NULL,
  headline TEXT NOT NULL DEFAULT 'Your Day in Review',
  day_emoji TEXT NOT NULL DEFAULT '📅',
  overview TEXT NOT NULL DEFAULT '',
  stats_json TEXT NOT NULL DEFAULT '{}',
  highlights_json TEXT NOT NULL DEFAULT '[]',
  action_items_json TEXT NOT NULL DEFAULT '[]',
  unresolved_questions_json TEXT NOT NULL DEFAULT '[]',
  decisions_made_json TEXT NOT NULL DEFAULT '[]',
  knowledge_nuggets_json TEXT NOT NULL DEFAULT '[]',
  locations_json TEXT NOT NULL DEFAULT '[]',
  visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'shared')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, id),
  UNIQUE (uid, date)
);

CREATE INDEX IF NOT EXISTS cf_daily_summaries_uid_date_idx
  ON cf_daily_summaries(uid, date DESC, id DESC);
