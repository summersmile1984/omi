-- Reviewed desktop release artifacts mirrored from the immutable GitHub
-- release into the Cloudflare R2 desktop-updates namespace.
--
-- The manifest remains the metadata authority.  These rows are a separate,
-- content-addressed transfer ledger: an artifact can only be queued from an
-- already-applied manifest review, and a successful transfer is recorded only
-- after the R2 object's digest metadata has been verified.
CREATE TABLE IF NOT EXISTS cf_desktop_release_artifacts (
  release_id TEXT NOT NULL,
  asset_name TEXT NOT NULL,
  review_id TEXT NOT NULL REFERENCES cf_desktop_release_import_review_batches(review_id),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_url TEXT NOT NULL CHECK (length(source_url) BETWEEN 1 AND 2048),
  object_key TEXT NOT NULL CHECK (length(object_key) BETWEEN 1 AND 512),
  expected_sha256 TEXT NOT NULL CHECK (length(expected_sha256) = 71 AND expected_sha256 GLOB 'sha256:*' AND substr(expected_sha256, 8) NOT GLOB '*[^0-9a-f]*'),
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

CREATE INDEX IF NOT EXISTS cf_desktop_release_artifacts_status_idx
  ON cf_desktop_release_artifacts(status, updated_at DESC);

CREATE INDEX IF NOT EXISTS cf_desktop_release_artifacts_review_idx
  ON cf_desktop_release_artifacts(review_id, release_id, asset_name);

-- An artifact ledger row is never allowed to outlive its review batch.  The
-- review tables are operator/audit control state, not customer data, so no
-- account-deletion fence is necessary here.
