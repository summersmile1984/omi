-- Public desktop update metadata. Release artifacts remain immutable objects in
-- the release pipeline; D1 stores only the signed metadata and live pointers.
CREATE TABLE IF NOT EXISTS cf_desktop_releases (
  id TEXT PRIMARY KEY,
  version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 128),
  build_number INTEGER NOT NULL CHECK (build_number >= 0),
  download_url TEXT NOT NULL CHECK (length(download_url) BETWEEN 1 AND 4096),
  manual_download_url TEXT CHECK (manual_download_url IS NULL OR length(manual_download_url) BETWEEN 1 AND 4096),
  ed_signature TEXT NOT NULL CHECK (length(ed_signature) BETWEEN 1 AND 4096),
  published_at TEXT NOT NULL CHECK (length(published_at) BETWEEN 1 AND 128),
  changelog_json TEXT NOT NULL DEFAULT '[]' CHECK (length(changelog_json) <= 32000),
  is_live INTEGER NOT NULL DEFAULT 0 CHECK (is_live IN (0, 1)),
  is_critical INTEGER NOT NULL DEFAULT 0 CHECK (is_critical IN (0, 1)),
  channel TEXT NOT NULL DEFAULT 'staging' CHECK (channel IN ('staging', 'beta', 'stable')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_desktop_releases_live_idx
  ON cf_desktop_releases(is_live, channel, build_number DESC, id);

CREATE TABLE IF NOT EXISTS cf_desktop_update_policy (
  id TEXT PRIMARY KEY,
  active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
  severity TEXT NOT NULL DEFAULT 'none' CHECK (severity IN ('none', 'banner', 'required')),
  maximum_build_number INTEGER CHECK (maximum_build_number IS NULL OR maximum_build_number >= 0),
  latest_build_number INTEGER CHECK (latest_build_number IS NULL OR latest_build_number >= 0),
  title TEXT CHECK (title IS NULL OR length(title) <= 512),
  message TEXT CHECK (message IS NULL OR length(message) <= 8000),
  cta_text TEXT NOT NULL DEFAULT 'Download latest' CHECK (length(cta_text) BETWEEN 1 AND 256),
  download_url TEXT NOT NULL CHECK (length(download_url) BETWEEN 1 AND 4096),
  can_dismiss INTEGER NOT NULL DEFAULT 1 CHECK (can_dismiss IN (0, 1)),
  platforms_json TEXT NOT NULL DEFAULT '[]' CHECK (length(platforms_json) <= 2048),
  updated_at INTEGER NOT NULL
);

INSERT OR IGNORE INTO cf_desktop_update_policy
  (id, active, severity, cta_text, download_url, can_dismiss, platforms_json, updated_at)
VALUES
  ('current', 0, 'none', 'Download latest',
   'https://api.omi.me/v2/desktop/download/latest?channel=stable', 1, '[]', unixepoch());
