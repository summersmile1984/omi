-- Reviewed Windows release artifacts mirrored from the immutable GitHub
-- release into the Cloudflare desktop-updates namespace.
--
-- The source export is deliberately an operator-provided, content-bound plan.
-- These tables are an audit and transfer ledger only: they do not create a
-- Windows channel pointer, mutate desktop release metadata, or read GitHub.
CREATE TABLE IF NOT EXISTS cf_windows_release_artifact_review_batches (
  review_id TEXT PRIMARY KEY CHECK (length(review_id) = 36),
  release_id TEXT NOT NULL CHECK (length(release_id) BETWEEN 1 AND 128),
  source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK (status IN ('approved', 'applied', 'revoked')),
  reviewed_at INTEGER NOT NULL CHECK (reviewed_at > 0),
  expires_at INTEGER NOT NULL CHECK (expires_at > reviewed_at),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  UNIQUE (release_id, source_fingerprint, plan_hash)
);

CREATE INDEX IF NOT EXISTS cf_windows_release_artifact_review_status_idx
  ON cf_windows_release_artifact_review_batches(status, expires_at, updated_at DESC);

CREATE TABLE IF NOT EXISTS cf_windows_release_artifact_review_items (
  review_id TEXT PRIMARY KEY REFERENCES cf_windows_release_artifact_review_batches(review_id),
  release_id TEXT NOT NULL CHECK (length(release_id) BETWEEN 1 AND 128),
  source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  plan_json TEXT NOT NULL CHECK (length(plan_json) BETWEEN 2 AND 262144 AND json_valid(plan_json)),
  created_at INTEGER NOT NULL CHECK (created_at > 0)
);

CREATE INDEX IF NOT EXISTS cf_windows_release_artifact_review_items_release_idx
  ON cf_windows_release_artifact_review_items(release_id, source_fingerprint, plan_hash);

CREATE TABLE IF NOT EXISTS cf_windows_release_artifacts (
  release_id TEXT NOT NULL CHECK (length(release_id) BETWEEN 1 AND 128),
  asset_name TEXT NOT NULL CHECK (asset_name GLOB 'Omi-for-Windows-Setup-[0-9]*.[0-9]*.[0-9]*.exe' OR asset_name GLOB 'Omi-for-Windows-Setup-[0-9]*.[0-9]*.[0-9]*.exe.blockmap' OR asset_name = 'latest.yml'),
  review_id TEXT NOT NULL REFERENCES cf_windows_release_artifact_review_batches(review_id),
  source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  source_url TEXT NOT NULL CHECK (length(source_url) BETWEEN 1 AND 2048),
  object_key TEXT NOT NULL CHECK (length(object_key) BETWEEN 1 AND 512),
  expected_sha256 TEXT NOT NULL CHECK (length(expected_sha256) = 64 AND expected_sha256 NOT GLOB '*[^0-9a-f]*'),
  content_type TEXT NOT NULL CHECK (length(content_type) BETWEEN 1 AND 128),
  size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
  status TEXT NOT NULL CHECK (status IN ('queued', 'copying', 'copied', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_queued_at INTEGER,
  last_error TEXT,
  created_at INTEGER NOT NULL CHECK (created_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  copied_at INTEGER,
  PRIMARY KEY (release_id, asset_name),
  UNIQUE (object_key)
);

CREATE INDEX IF NOT EXISTS cf_windows_release_artifacts_status_idx
  ON cf_windows_release_artifacts(status, updated_at DESC);

CREATE INDEX IF NOT EXISTS cf_windows_release_artifacts_review_idx
  ON cf_windows_release_artifacts(review_id, release_id, asset_name);

-- The mirror is operator/system state and has no customer uid.  A row may not
-- outlive the review batch that authorized it; deleting/revoking a review is
-- intentionally outside the public product surface.
