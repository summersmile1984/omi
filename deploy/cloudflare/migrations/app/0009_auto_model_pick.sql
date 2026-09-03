CREATE TABLE IF NOT EXISTS cf_auto_model_pick (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  provider TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  updated_at REAL NOT NULL
);
