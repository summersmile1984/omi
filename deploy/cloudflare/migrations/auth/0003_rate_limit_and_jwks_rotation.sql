CREATE TABLE IF NOT EXISTS rateLimit (
  id TEXT PRIMARY KEY NOT NULL,
  key TEXT NOT NULL UNIQUE,
  count INTEGER NOT NULL,
  lastRequest INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS rateLimit_lastRequest_idx ON rateLimit(lastRequest);
CREATE INDEX IF NOT EXISTS jwks_expiresAt_idx ON jwks(expiresAt);

-- Better Auth's D1 adapter has written Date values as ISO strings in the
-- current staging database, while SQLite also permits integer timestamps in
-- this column. Give the pre-rotation key the same 30-day lifetime as all keys
-- generated after this migration without assuming one storage representation.
UPDATE jwks
SET expiresAt = CASE
  WHEN typeof(createdAt) IN ('integer', 'real') THEN createdAt + 2592000000
  ELSE strftime('%Y-%m-%dT%H:%M:%fZ', createdAt, '+30 days')
END
WHERE expiresAt IS NULL;
