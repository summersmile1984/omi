-- Durable memory-import evidence for the Cloudflare data plane.
-- Import artifacts are evidence only; extraction/promotion remains a separate
-- workflow and never happens as part of this request.
CREATE TABLE IF NOT EXISTS cf_memory_import_runs (
  uid TEXT NOT NULL,
  run_id TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK (length(source_type) BETWEEN 1 AND 128),
  source_account_hash TEXT CHECK (source_account_hash IS NULL OR length(source_account_hash) <= 512),
  importer_version TEXT NOT NULL CHECK (length(importer_version) BETWEEN 1 AND 128),
  extractor_version TEXT CHECK (extractor_version IS NULL OR length(extractor_version) <= 128),
  status TEXT NOT NULL CHECK (status IN ('received', 'extracting', 'completed', 'failed', 'cancelled')),
  artifact_count INTEGER NOT NULL DEFAULT 0 CHECK (artifact_count >= 0),
  candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
  accepted_count INTEGER NOT NULL DEFAULT 0 CHECK (accepted_count >= 0),
  promoted_count INTEGER NOT NULL DEFAULT 0 CHECK (promoted_count >= 0),
  deduped_count INTEGER NOT NULL DEFAULT 0 CHECK (deduped_count >= 0),
  started_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  completed_at INTEGER,
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  PRIMARY KEY (uid, run_id)
);

CREATE INDEX IF NOT EXISTS cf_memory_import_runs_uid_updated_idx
  ON cf_memory_import_runs(uid, updated_at DESC, run_id DESC);

CREATE TABLE IF NOT EXISTS cf_memory_import_artifacts (
  uid TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK (length(source_type) BETWEEN 1 AND 128),
  external_id TEXT CHECK (external_id IS NULL OR length(external_id) <= 512),
  content_hash TEXT NOT NULL CHECK (length(content_hash) BETWEEN 1 AND 512),
  title TEXT CHECK (title IS NULL OR length(title) <= 2048),
  snippet TEXT CHECK (snippet IS NULL OR length(snippet) <= 8192),
  redacted_body TEXT CHECK (redacted_body IS NULL OR length(redacted_body) <= 50000),
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (length(metadata_json) <= 65536),
  occurred_at INTEGER,
  captured_at INTEGER NOT NULL,
  client_device_id TEXT CHECK (client_device_id IS NULL OR length(client_device_id) <= 256),
  source_state TEXT NOT NULL DEFAULT 'active' CHECK (source_state IN ('active', 'tombstoned', 'purged')),
  redaction_status TEXT NOT NULL CHECK (length(redaction_status) BETWEEN 1 AND 128),
  sensitivity_labels_json TEXT NOT NULL DEFAULT '[]' CHECK (length(sensitivity_labels_json) <= 4096),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, artifact_id),
  FOREIGN KEY (uid, run_id) REFERENCES cf_memory_import_runs(uid, run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_memory_import_artifacts_uid_run_idx
  ON cf_memory_import_artifacts(uid, run_id, created_at DESC, artifact_id);

CREATE INDEX IF NOT EXISTS cf_memory_import_artifacts_uid_hash_idx
  ON cf_memory_import_artifacts(uid, content_hash, artifact_id);

CREATE TRIGGER IF NOT EXISTS adf_i_memory_import_runs
BEFORE INSERT ON cf_memory_import_runs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_import_runs
BEFORE UPDATE ON cf_memory_import_runs
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_import_artifacts
BEFORE INSERT ON cf_memory_import_artifacts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_import_artifacts
BEFORE UPDATE ON cf_memory_import_artifacts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
