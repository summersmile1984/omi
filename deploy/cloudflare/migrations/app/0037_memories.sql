CREATE TABLE IF NOT EXISTS cf_memories (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 50000),
  category TEXT NOT NULL DEFAULT 'interesting' CHECK (category IN (
    'interesting', 'system', 'manual', 'workflow'
  )),
  visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('public', 'private')),
  tags_json TEXT NOT NULL DEFAULT '[]',
  headline TEXT,
  predicate TEXT,
  arguments_json TEXT NOT NULL DEFAULT '{}',
  subject_entity_id TEXT,
  subject_attribution TEXT NOT NULL DEFAULT 'unknown' CHECK (subject_attribution IN (
    'user', 'third_party', 'unknown', 'legacy_assumed'
  )),
  object_entity_ids_json TEXT NOT NULL DEFAULT '[]',
  qualifiers_json TEXT NOT NULL DEFAULT '{}',
  capture_confidence REAL,
  veracity REAL,
  uncertainty_reasons_json TEXT NOT NULL DEFAULT '[]',
  durability TEXT,
  conversation_id TEXT,
  reviewed INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0, 1)),
  user_review INTEGER CHECK (user_review IS NULL OR user_review IN (0, 1)),
  manually_added INTEGER NOT NULL DEFAULT 0 CHECK (manually_added IN (0, 1)),
  edited INTEGER NOT NULL DEFAULT 0 CHECK (edited IN (0, 1)),
  scoring TEXT,
  app_id TEXT,
  data_protection_level TEXT,
  is_locked INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0, 1)),
  is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
  is_dismissed INTEGER NOT NULL DEFAULT 0 CHECK (is_dismissed IN (0, 1)),
  kg_extracted INTEGER NOT NULL DEFAULT 0 CHECK (kg_extracted IN (0, 1)),
  is_baseline INTEGER NOT NULL DEFAULT 0 CHECK (is_baseline IN (0, 1)),
  memory_tier TEXT NOT NULL CHECK (memory_tier IN ('short_term', 'long_term', 'archive')),
  valid_at INTEGER NOT NULL,
  invalid_at INTEGER,
  superseded_by TEXT,
  primary_capture_device TEXT,
  capture_device_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  deleted_at INTEGER,
  PRIMARY KEY (uid, id)
);

CREATE INDEX IF NOT EXISTS cf_memories_uid_active_updated_idx
  ON cf_memories(uid, deleted_at, invalid_at, updated_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS cf_memories_uid_category_updated_idx
  ON cf_memories(uid, category, updated_at DESC, id DESC);
