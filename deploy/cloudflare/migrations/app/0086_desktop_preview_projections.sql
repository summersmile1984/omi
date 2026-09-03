-- Immutable desktop preview metadata and the mutable slug pointer.
-- CI publication/backfill remains a separate release-pipeline concern; public
-- reads fail closed until both rows are projected into this D1 authority.
CREATE TABLE IF NOT EXISTS cf_desktop_preview_manifests (
  slug TEXT NOT NULL CHECK (length(slug) BETWEEN 1 AND 63),
  source_sha TEXT NOT NULL CHECK (length(source_sha) = 40),
  dmg_url TEXT NOT NULL CHECK (length(dmg_url) BETWEEN 1 AND 4096),
  dmg_sha256 TEXT NOT NULL CHECK (length(dmg_sha256) = 64),
  app_name TEXT NOT NULL CHECK (length(app_name) BETWEEN 1 AND 128),
  bundle_id TEXT NOT NULL CHECK (length(bundle_id) BETWEEN 1 AND 96),
  url_scheme TEXT NOT NULL CHECK (length(url_scheme) BETWEEN 1 AND 96),
  built_at TEXT NOT NULL CHECK (length(built_at) BETWEEN 1 AND 128),
  signer TEXT NOT NULL CHECK (length(signer) BETWEEN 1 AND 512),
  notarization TEXT NOT NULL CHECK (length(notarization) BETWEEN 1 AND 32),
  notes TEXT CHECK (notes IS NULL OR length(notes) <= 2000),
  backend_url TEXT CHECK (backend_url IS NULL OR length(backend_url) BETWEEN 1 AND 4096),
  created_at INTEGER NOT NULL,
  PRIMARY KEY (slug, source_sha)
);

CREATE TABLE IF NOT EXISTS cf_desktop_preview_pointers (
  slug TEXT PRIMARY KEY CHECK (length(slug) BETWEEN 1 AND 63),
  source_sha TEXT NOT NULL CHECK (length(source_sha) = 40),
  generation INTEGER NOT NULL CHECK (generation >= 0),
  updated_at INTEGER NOT NULL
);
