CREATE TABLE IF NOT EXISTS cf_user_geolocation (
  uid TEXT PRIMARY KEY NOT NULL,
  google_place_id TEXT,
  latitude REAL NOT NULL,
  longitude REAL NOT NULL,
  address TEXT,
  location_type TEXT,
  updated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_user_geolocation_expiry_idx
  ON cf_user_geolocation(expires_at);
