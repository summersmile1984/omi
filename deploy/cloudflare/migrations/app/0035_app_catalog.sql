CREATE TABLE IF NOT EXISTS cf_app_catalog (
  id TEXT PRIMARY KEY NOT NULL,
  approved INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0, 1)),
  status TEXT NOT NULL DEFAULT 'approved',
  disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
  is_popular INTEGER NOT NULL DEFAULT 0 CHECK (is_popular IN (0, 1)),
  installs INTEGER NOT NULL DEFAULT 0 CHECK (installs >= 0),
  rating_avg REAL,
  rating_count INTEGER NOT NULL DEFAULT 0 CHECK (rating_count >= 0),
  data_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_app_catalog_public_idx
  ON cf_app_catalog (approved, disabled, is_popular, installs DESC, id);
