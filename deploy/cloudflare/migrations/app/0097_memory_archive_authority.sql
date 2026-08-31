-- D1 authority for explicit Archive memory reads.
--
-- These tables are projections, not a compatibility cache.  The projection
-- writer must provide the account generation from cf_account_cutover and the
-- server-owned capability state; no request path creates or upgrades either
-- value.  Rows are therefore unreadable until an approved cutover projection
-- has populated both control tables.
CREATE TABLE IF NOT EXISTS cf_memory_global_read_gate (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
  source TEXT NOT NULL CHECK (source = 'cloudflare_operator'),
  memory_reads_enabled INTEGER NOT NULL CHECK (memory_reads_enabled IN (0, 1)),
  kill_switch_active INTEGER NOT NULL CHECK (kill_switch_active IN (0, 1)),
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cf_memory_control (
  uid TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
  source TEXT NOT NULL CHECK (source = 'cloudflare_cutover_projection'),
  memory_reads_enabled INTEGER NOT NULL CHECK (memory_reads_enabled IN (0, 1)),
  default_memory_grant INTEGER NOT NULL CHECK (default_memory_grant IN (0, 1)),
  archive_capability INTEGER NOT NULL CHECK (archive_capability IN (0, 1)),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  source_revision TEXT NOT NULL CHECK (length(source_revision) BETWEEN 1 AND 256),
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cf_memory_archive_items (
  uid TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  memory_tier TEXT NOT NULL DEFAULT 'archive' CHECK (memory_tier = 'archive'),
  content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 50000),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'hidden', 'tombstoned')),
  processing_state TEXT NOT NULL CHECK (processing_state IN ('pending', 'processed', 'blocked')),
  source_state TEXT NOT NULL CHECK (source_state IN ('active', 'tombstoned', 'purged')),
  sensitivity_labels_json TEXT NOT NULL DEFAULT '[]'
    CHECK (length(sensitivity_labels_json) <= 4096),
  visibility TEXT NOT NULL CHECK (visibility IN ('private', 'public', 'shared')),
  user_asserted INTEGER NOT NULL DEFAULT 0 CHECK (user_asserted IN (0, 1)),
  captured_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  expires_at INTEGER,
  ledger_commit_id TEXT,
  ledger_sequence INTEGER,
  item_revision INTEGER NOT NULL DEFAULT 1 CHECK (item_revision > 0),
  source_id TEXT,
  evidence_json TEXT NOT NULL DEFAULT '[]' CHECK (length(evidence_json) <= 65536),
  confidence REAL,
  superseded_by TEXT,
  is_locked INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0, 1)),
  account_generation INTEGER NOT NULL DEFAULT 0 CHECK (account_generation >= 0),
  created_at INTEGER NOT NULL,
  deleted_at INTEGER,
  PRIMARY KEY (uid, memory_id)
);

CREATE INDEX IF NOT EXISTS cf_memory_archive_items_uid_updated_idx
  ON cf_memory_archive_items(uid, account_generation, deleted_at, updated_at DESC, memory_id DESC);

CREATE INDEX IF NOT EXISTS cf_memory_archive_items_uid_content_idx
  ON cf_memory_archive_items(uid, memory_tier, status, source_state, is_locked, updated_at DESC, memory_id DESC);

-- Archive rows and capability state are both account-scoped product data.  A
-- deletion intent/tombstone prevents new projections or control updates while
-- the Jobs owner is purging the account.
CREATE TRIGGER IF NOT EXISTS adf_i_memory_control
BEFORE INSERT ON cf_memory_control
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_control
BEFORE UPDATE ON cf_memory_control
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_archive_items
BEFORE INSERT ON cf_memory_archive_items
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_archive_items
BEFORE UPDATE ON cf_memory_archive_items
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
