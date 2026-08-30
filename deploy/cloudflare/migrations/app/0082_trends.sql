-- Global, read-only trend projections.  The source index is not user-scoped;
-- keep the category/topic identity explicit so the public reader never needs
-- to hydrate Firestore subcollections at request time.
CREATE TABLE IF NOT EXISTS cf_trend_categories (
  id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 256),
  category TEXT NOT NULL CHECK (category IN ('ceo', 'company', 'software_product', 'hardware_product', 'ai_product')),
  type TEXT NOT NULL CHECK (type IN ('best', 'worst')),
  created_at INTEGER NOT NULL,
  UNIQUE (category, type)
);

CREATE TABLE IF NOT EXISTS cf_trend_topics (
  category_id TEXT NOT NULL,
  id TEXT NOT NULL,
  topic TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 512),
  memories_count INTEGER NOT NULL DEFAULT 0 CHECK (memories_count >= 0),
  PRIMARY KEY (category_id, id),
  UNIQUE (category_id, topic),
  FOREIGN KEY (category_id) REFERENCES cf_trend_categories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_trend_topics_category_count_idx
  ON cf_trend_topics(category_id, memories_count DESC, id ASC);
