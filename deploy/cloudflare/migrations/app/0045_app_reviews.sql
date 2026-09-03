ALTER TABLE cf_app_catalog ADD COLUMN owner_uid TEXT;

CREATE INDEX IF NOT EXISTS cf_app_catalog_owner_idx
  ON cf_app_catalog(owner_uid, id);

CREATE TABLE IF NOT EXISTS cf_app_reviews (
  app_id TEXT NOT NULL,
  reviewer_uid TEXT NOT NULL,
  score REAL NOT NULL CHECK (score >= 0 AND score <= 5),
  review_text TEXT NOT NULL DEFAULT '',
  username TEXT NOT NULL DEFAULT '',
  response TEXT NOT NULL DEFAULT '',
  rated_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  responded_at INTEGER,
  PRIMARY KEY (app_id, reviewer_uid),
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_app_reviews_app_rated_idx
  ON cf_app_reviews(app_id, rated_at DESC, reviewer_uid);
