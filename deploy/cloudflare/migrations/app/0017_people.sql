CREATE TABLE IF NOT EXISTS cf_people (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 2 AND 40),
  speech_samples_json TEXT NOT NULL DEFAULT '[]',
  speech_sample_transcripts_json TEXT,
  speech_samples_version INTEGER NOT NULL DEFAULT 3,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, id),
  UNIQUE (uid, name)
);

CREATE INDEX IF NOT EXISTS cf_people_uid_created_idx
  ON cf_people(uid, created_at DESC);
