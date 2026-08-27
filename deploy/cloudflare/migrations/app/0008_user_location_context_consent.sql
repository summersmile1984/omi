CREATE TABLE IF NOT EXISTS cf_user_location_context_consent (
  uid TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('granted', 'revoked')),
  purpose TEXT NOT NULL,
  disclosed_providers_json TEXT NOT NULL,
  granted_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
