-- D1 projection for the memory non-active-route audit surface.
--
-- This table is report-only in the first Cloudflare phase.  It is not a
-- compatibility cache for Firestore: rows are readable only for an account
-- with a completed, destination-bound cutover and the admin route never
-- backfills or invents an outcome.
CREATE TABLE IF NOT EXISTS cf_memory_non_active_routes (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  outcome_id TEXT NOT NULL CHECK (length(outcome_id) BETWEEN 1 AND 256),
  route TEXT NOT NULL CHECK (route IN (
    'review', 'archive', 'context_only', 'reject', 'hidden', 'skip'
  )),
  idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 512),
  source_ids_json TEXT NOT NULL CHECK (
    length(source_ids_json) BETWEEN 3 AND 65536
    AND json_valid(source_ids_json)
    AND json_type(source_ids_json) = 'array'
  ),
  reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 2048),
  run_id TEXT NOT NULL CHECK (length(run_id) BETWEEN 1 AND 256),
  patch_id TEXT,
  audit_metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (
    length(audit_metadata_json) BETWEEN 2 AND 65536
    AND json_valid(audit_metadata_json)
    AND json_type(audit_metadata_json) = 'object'
  ),
  created_at INTEGER NOT NULL,
  default_long_term_visible INTEGER NOT NULL DEFAULT 0 CHECK (default_long_term_visible IN (0, 1)),
  payload_fingerprint TEXT NOT NULL CHECK (length(payload_fingerprint) = 64),
  account_generation INTEGER NOT NULL DEFAULT 0 CHECK (account_generation >= 0),
  PRIMARY KEY (uid, outcome_id),
  UNIQUE (uid, idempotency_key)
);

CREATE INDEX IF NOT EXISTS cf_memory_non_active_routes_uid_run_idx
  ON cf_memory_non_active_routes(uid, account_generation, run_id, created_at DESC, outcome_id DESC);

CREATE INDEX IF NOT EXISTS cf_memory_non_active_routes_uid_route_idx
  ON cf_memory_non_active_routes(uid, account_generation, route, created_at DESC, outcome_id DESC);

-- The Jobs deletion owner deletes residual rows, but a late projection must
-- not recreate them after an account's deletion intent or tombstone exists.
CREATE TRIGGER IF NOT EXISTS adf_i_memory_non_active_routes
BEFORE INSERT ON cf_memory_non_active_routes
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_non_active_routes
BEFORE UPDATE ON cf_memory_non_active_routes
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
