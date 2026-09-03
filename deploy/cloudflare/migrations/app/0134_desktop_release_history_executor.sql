-- Reviewed historical desktop release manifest promotion.
--
-- The legacy release endpoint remains the source authority until an operator
-- reviews an exact, content-bound plan.  These rows are an audit ledger for
-- that handoff; the immutable manifest table remains the destination
-- authority and is written through API Core's existing contract.
CREATE TABLE IF NOT EXISTS cf_desktop_release_import_review_batches (
  review_id TEXT PRIMARY KEY CHECK (length(review_id) = 36),
  source_endpoint TEXT NOT NULL CHECK (length(source_endpoint) BETWEEN 1 AND 2048),
  release_id TEXT NOT NULL CHECK (length(release_id) BETWEEN 1 AND 128),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK (status IN ('approved', 'applied', 'revoked')),
  reviewed_at INTEGER NOT NULL CHECK (reviewed_at > 0),
  expires_at INTEGER NOT NULL CHECK (expires_at > reviewed_at),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  UNIQUE (source_endpoint, release_id, manifest_sha256, plan_hash)
);

CREATE INDEX IF NOT EXISTS cf_desktop_release_import_review_batches_status_idx
  ON cf_desktop_release_import_review_batches(status, expires_at, updated_at DESC);

CREATE TABLE IF NOT EXISTS cf_desktop_release_import_review_items (
  review_id TEXT PRIMARY KEY REFERENCES cf_desktop_release_import_review_batches(review_id),
  source_endpoint TEXT NOT NULL CHECK (length(source_endpoint) BETWEEN 1 AND 2048),
  release_id TEXT NOT NULL CHECK (length(release_id) BETWEEN 1 AND 128),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  manifest_json TEXT NOT NULL CHECK (length(manifest_json) BETWEEN 2 AND 128000 AND json_valid(manifest_json)),
  created_at INTEGER NOT NULL CHECK (created_at > 0)
);

CREATE INDEX IF NOT EXISTS cf_desktop_release_import_review_items_release_idx
  ON cf_desktop_release_import_review_items(release_id, manifest_sha256, plan_hash);

CREATE TABLE IF NOT EXISTS cf_desktop_release_import_applies (
  release_id TEXT PRIMARY KEY CHECK (length(release_id) BETWEEN 1 AND 128),
  review_id TEXT NOT NULL REFERENCES cf_desktop_release_import_review_batches(review_id),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK (status = 'applied'),
  applied_at INTEGER NOT NULL CHECK (applied_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0)
);

CREATE INDEX IF NOT EXISTS cf_desktop_release_import_applies_review_idx
  ON cf_desktop_release_import_applies(review_id, status, updated_at DESC);

-- The apply marker is a D1 CAS boundary: it can only be written for the
-- reviewed item that is already present in the immutable destination table.
CREATE TRIGGER IF NOT EXISTS validate_desktop_release_import_apply
BEFORE INSERT ON cf_desktop_release_import_applies
WHEN NOT EXISTS (
  SELECT 1
  FROM cf_desktop_release_import_review_batches AS b
  JOIN cf_desktop_release_import_review_items AS i ON i.review_id = b.review_id
  JOIN cf_desktop_release_manifests AS m ON m.release_id = i.release_id
  WHERE b.review_id = NEW.review_id
    AND b.release_id = NEW.release_id
    AND b.manifest_sha256 = NEW.manifest_sha256
    AND b.plan_hash = NEW.plan_hash
    AND b.status IN ('approved', 'applied')
    AND i.source_endpoint = b.source_endpoint
    AND i.release_id = NEW.release_id
    AND i.manifest_sha256 = NEW.manifest_sha256
    AND i.plan_hash = NEW.plan_hash
    AND m.manifest_sha256 = NEW.manifest_sha256
)
BEGIN
  SELECT RAISE(ABORT, 'desktop release import authority changed');
END;

-- No channel pointer is touched by this ledger.  A historical manifest must
-- be promoted to a channel only by the existing explicit CAS endpoint.
