-- Windows electron-updater feed directories are platform-specific and must
-- not be inferred from a macOS installer URL. Release backfill/promotion may
-- populate this immutable URL after a Windows asset has been qualified.
ALTER TABLE cf_desktop_releases ADD COLUMN windows_feed_url TEXT
  CHECK (windows_feed_url IS NULL OR length(windows_feed_url) BETWEEN 1 AND 4096);

