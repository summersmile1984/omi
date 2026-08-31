-- Immutable v1 desktop release manifests.  The existing release projection is
-- intentionally optimized for public feeds and cannot reconstruct this full
-- contract.  A manifest is inserted once and never updated or deleted.
CREATE TABLE IF NOT EXISTS cf_desktop_release_manifests (
  release_id TEXT PRIMARY KEY CHECK (length(release_id) BETWEEN 1 AND 128),
  manifest_json TEXT NOT NULL CHECK (length(manifest_json) BETWEEN 2 AND 128000 AND json_valid(manifest_json)),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at INTEGER NOT NULL
);

CREATE TRIGGER IF NOT EXISTS cf_desktop_release_manifests_no_update
  BEFORE UPDATE ON cf_desktop_release_manifests
BEGIN
  SELECT RAISE(ABORT, 'desktop release manifests are immutable');
END;

CREATE TRIGGER IF NOT EXISTS cf_desktop_release_manifests_no_delete
  BEFORE DELETE ON cf_desktop_release_manifests
BEGIN
  SELECT RAISE(ABORT, 'desktop release manifests are immutable');
END;
