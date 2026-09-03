CREATE TABLE IF NOT EXISTS auth_identity_imports (
  id TEXT PRIMARY KEY NOT NULL CHECK (id = 'firebase'),
  sourceSha256 TEXT NOT NULL,
  configFingerprint TEXT NOT NULL,
  canonicalSha256 TEXT NOT NULL,
  userCount INTEGER NOT NULL CHECK (userCount >= 0),
  accountCount INTEGER NOT NULL CHECK (accountCount >= 0),
  status TEXT NOT NULL CHECK (status IN ('applying', 'completed')),
  startedAt INTEGER NOT NULL,
  completedAt INTEGER
);
