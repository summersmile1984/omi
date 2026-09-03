CREATE TABLE IF NOT EXISTS cf_calendar_meetings (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  calendar_event_id TEXT NOT NULL,
  calendar_source TEXT NOT NULL,
  title TEXT NOT NULL,
  participants_json TEXT NOT NULL DEFAULT '[]',
  platform TEXT,
  meeting_link TEXT,
  start_time INTEGER NOT NULL,
  end_time INTEGER NOT NULL,
  duration_minutes INTEGER NOT NULL,
  notes TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, id),
  UNIQUE (uid, calendar_source, calendar_event_id)
);

CREATE INDEX IF NOT EXISTS cf_calendar_meetings_uid_start_idx
  ON cf_calendar_meetings(uid, start_time DESC, id DESC);
