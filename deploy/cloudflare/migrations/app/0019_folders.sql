CREATE TABLE IF NOT EXISTS cf_folders (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  description TEXT,
  color TEXT NOT NULL DEFAULT '#6B7280',
  icon TEXT NOT NULL DEFAULT 'folder',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  display_order INTEGER NOT NULL DEFAULT 0,
  is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
  is_system INTEGER NOT NULL DEFAULT 0 CHECK (is_system IN (0, 1)),
  category_mapping TEXT,
  conversation_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (uid, id)
);

CREATE INDEX IF NOT EXISTS cf_folders_uid_order_idx
  ON cf_folders(uid, display_order, created_at);
