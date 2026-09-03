-- Server-owned macOS channel pointers.  The immutable release manifest remains
-- the only source of artifact metadata; this table stores only the mutable
-- distribution pointer and its compare-and-swap generation.
CREATE TABLE IF NOT EXISTS cf_desktop_channel_pointers (
  platform TEXT NOT NULL CHECK (platform = 'macos'),
  channel TEXT NOT NULL CHECK (channel IN ('beta', 'stable')),
  release_id TEXT NOT NULL CHECK (length(release_id) BETWEEN 1 AND 128),
  version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
  build_number INTEGER NOT NULL CHECK (build_number > 0),
  generation INTEGER NOT NULL CHECK (generation >= 1),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (platform, channel)
);

CREATE INDEX IF NOT EXISTS cf_desktop_channel_pointers_release_idx
  ON cf_desktop_channel_pointers(release_id);
