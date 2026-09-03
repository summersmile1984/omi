-- Reviewed public Persona/App catalog history promotion.
--
-- This is deliberately separate from the app-owner migration job.  It can
-- only promote a public metadata projection after Auth has already produced
-- a hash-only Firebase source projection and an operator has reviewed the
-- content-bound export plan.  Private Persona material, provider state and
-- legacy logo bytes are not represented by this table.

CREATE TABLE IF NOT EXISTS cf_persona_app_history_review_batches (
  review_id TEXT PRIMARY KEY CHECK (length(review_id) = 36),
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  source_ref TEXT NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 256),
  source_projection_revision TEXT NOT NULL CHECK (length(source_projection_revision) = 64 AND source_projection_revision NOT GLOB '*[^0-9a-f]*'),
  source_export_sha256 TEXT NOT NULL CHECK (length(source_export_sha256) = 64 AND source_export_sha256 NOT GLOB '*[^0-9a-f]*'),
  target_account_generation INTEGER NOT NULL CHECK (target_account_generation >= 0),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  entry_count INTEGER NOT NULL CHECK (entry_count > 0 AND entry_count <= 50),
  status TEXT NOT NULL CHECK (status IN ('approved', 'applied', 'revoked')),
  reviewed_at INTEGER NOT NULL CHECK (reviewed_at > 0),
  expires_at INTEGER NOT NULL CHECK (expires_at > reviewed_at),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  UNIQUE (uid, manifest_sha256)
);

CREATE INDEX IF NOT EXISTS cf_persona_app_history_review_batches_uid_idx
  ON cf_persona_app_history_review_batches(uid, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS cf_persona_app_history_review_items (
  review_id TEXT NOT NULL REFERENCES cf_persona_app_history_review_batches(review_id),
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  app_id TEXT NOT NULL CHECK (length(app_id) BETWEEN 1 AND 256),
  source_ref TEXT NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 256),
  source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
  source_row_sha256 TEXT NOT NULL CHECK (length(source_row_sha256) = 64 AND source_row_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  source_projection_revision TEXT NOT NULL CHECK (length(source_projection_revision) = 64 AND source_projection_revision NOT GLOB '*[^0-9a-f]*'),
  source_export_sha256 TEXT NOT NULL CHECK (length(source_export_sha256) = 64 AND source_export_sha256 NOT GLOB '*[^0-9a-f]*'),
  target_account_generation INTEGER NOT NULL CHECK (target_account_generation >= 0),
  public_metadata_json TEXT NOT NULL CHECK (length(public_metadata_json) BETWEEN 2 AND 512000 AND json_valid(public_metadata_json)),
  created_at INTEGER NOT NULL CHECK (created_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  PRIMARY KEY (review_id, uid, app_id),
  UNIQUE (review_id, request_fingerprint)
);

CREATE INDEX IF NOT EXISTS cf_persona_app_history_review_items_lookup_idx
  ON cf_persona_app_history_review_items(uid, app_id, source_row_sha256, target_account_generation);

CREATE TABLE IF NOT EXISTS cf_persona_app_history_applies (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  app_id TEXT NOT NULL CHECK (length(app_id) BETWEEN 1 AND 256),
  review_id TEXT NOT NULL REFERENCES cf_persona_app_history_review_batches(review_id),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_ref TEXT NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 256),
  source_row_sha256 TEXT NOT NULL CHECK (length(source_row_sha256) = 64 AND source_row_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  target_account_generation INTEGER NOT NULL CHECK (target_account_generation >= 0),
  status TEXT NOT NULL CHECK (status = 'applied'),
  applied_at INTEGER NOT NULL CHECK (applied_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  PRIMARY KEY (uid, app_id),
  UNIQUE (uid, request_fingerprint)
);

CREATE INDEX IF NOT EXISTS cf_persona_app_history_applies_review_idx
  ON cf_persona_app_history_applies(review_id, status, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_persona_app_history_review_batches
BEFORE INSERT ON cf_persona_app_history_review_batches
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_persona_app_history_review_batches
BEFORE UPDATE ON cf_persona_app_history_review_batches
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_persona_app_history_review_items
BEFORE INSERT ON cf_persona_app_history_review_items
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_persona_app_history_review_items
BEFORE UPDATE ON cf_persona_app_history_review_items
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

-- The receipt is valid only when the reviewed row, source identity/data
-- attestation, destination cutover and target catalog row still agree.
CREATE TRIGGER IF NOT EXISTS validate_persona_app_history_apply
BEFORE INSERT ON cf_persona_app_history_applies
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
   OR NOT EXISTS (
     SELECT 1 FROM cf_account_cutover AS c
     WHERE c.uid = NEW.uid AND c.state = 'new'
       AND c.checkpoint_phase = 'completed'
       AND c.destination_backend_bound = 1
       AND c.account_generation = NEW.target_account_generation
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_app_owner_migration_sources AS s
     WHERE s.source_uid = NEW.source_ref
       AND s.source_uid LIKE 'fb-anon-%'
       AND s.projection_status = 'imported'
       AND s.target_uid = NEW.uid
       AND s.target_account_generation = NEW.target_account_generation
       AND s.source_projection_revision = (
         SELECT i.source_projection_revision
         FROM cf_persona_app_history_review_items AS i
         WHERE i.review_id = NEW.review_id AND i.uid = NEW.uid AND i.app_id = NEW.app_id
       )
       AND s.data_projection_status = 'verified'
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_persona_app_history_review_items AS i
     WHERE i.review_id = NEW.review_id AND i.uid = NEW.uid AND i.app_id = NEW.app_id
       AND i.source_ref = NEW.source_ref
       AND i.source_row_sha256 = NEW.source_row_sha256
       AND i.request_fingerprint = NEW.request_fingerprint
       AND i.source_export_sha256 = (
         SELECT b.source_export_sha256
         FROM cf_persona_app_history_review_batches AS b
         WHERE b.review_id = NEW.review_id
       )
       AND i.target_account_generation = NEW.target_account_generation
   )
   OR NOT EXISTS (
     SELECT 1 FROM cf_app_catalog AS a
     JOIN cf_persona_app_history_review_items AS i
       ON i.review_id = NEW.review_id AND i.uid = NEW.uid AND i.app_id = NEW.app_id
     WHERE a.id = NEW.app_id
       AND a.owner_uid = NEW.uid
       AND a.owner_account_generation = NEW.target_account_generation
       AND a.data_json = i.public_metadata_json
       AND a.approved = 0
   )
BEGIN
  SELECT RAISE(ABORT, 'persona app history authority changed');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_persona_app_history_applies
BEFORE INSERT ON cf_persona_app_history_applies
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_persona_app_history_applies
BEFORE UPDATE ON cf_persona_app_history_applies
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
