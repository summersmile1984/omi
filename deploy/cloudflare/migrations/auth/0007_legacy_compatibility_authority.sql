-- Dormant authority for a future Firebase/native-auth and external-app OAuth
-- compatibility slice.  No route reads these tables until the import ledger,
-- provider secrets, and authenticated replay fixtures have been verified.

CREATE TABLE IF NOT EXISTS cf_auth_deletion_fences (
  uid TEXT PRIMARY KEY NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  generation INTEGER NOT NULL CHECK (generation >= 1),
  status TEXT NOT NULL CHECK (status IN ('clear', 'deleting', 'deleted')),
  startedAt INTEGER NOT NULL,
  completedAt INTEGER
);

CREATE TABLE IF NOT EXISTS cf_firebase_identity_projection (
  firebaseUid TEXT PRIMARY KEY NOT NULL CHECK (length(firebaseUid) BETWEEN 1 AND 256),
  betterAuthUserId TEXT NOT NULL UNIQUE REFERENCES user(id) ON DELETE RESTRICT,
  providersJson TEXT NOT NULL
    CHECK (json_valid(providersJson) AND json_type(providersJson) = 'array'),
  sourceImportId TEXT NOT NULL REFERENCES auth_identity_imports(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK (status IN ('imported', 'revoked', 'conflict')),
  sourceUpdatedAt INTEGER NOT NULL,
  updatedAt INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_firebase_identity_projection_user_idx
  ON cf_firebase_identity_projection(betterAuthUserId);
CREATE INDEX IF NOT EXISTS cf_firebase_identity_projection_import_idx
  ON cf_firebase_identity_projection(sourceImportId);

CREATE TABLE IF NOT EXISTS cf_legacy_auth_transactions (
  id TEXT PRIMARY KEY NOT NULL CHECK (length(id) BETWEEN 1 AND 256),
  kind TEXT NOT NULL CHECK (kind IN ('session', 'code')),
  provider TEXT NOT NULL CHECK (provider IN ('google', 'apple')),
  lookupHash TEXT NOT NULL UNIQUE,
  stateHash TEXT NOT NULL,
  redirectUri TEXT NOT NULL,
  codeChallenge TEXT NOT NULL,
  codeChallengeMethod TEXT NOT NULL CHECK (codeChallengeMethod = 'S256'),
  encryptedPayload TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending', 'consumed', 'failed')),
  expiresAt INTEGER NOT NULL,
  createdAt INTEGER NOT NULL,
  consumedAt INTEGER,
  CHECK (expiresAt > createdAt),
  CHECK (
    (kind = 'session' AND encryptedPayload IS NULL)
    OR (kind = 'code' AND encryptedPayload IS NOT NULL)
  ),
  CHECK (
    (status = 'consumed' AND consumedAt IS NOT NULL)
    OR (status <> 'consumed' AND consumedAt IS NULL)
  )
);

CREATE INDEX IF NOT EXISTS cf_legacy_auth_transactions_expiry_idx
  ON cf_legacy_auth_transactions(status, expiresAt);
CREATE INDEX IF NOT EXISTS cf_legacy_auth_transactions_state_idx
  ON cf_legacy_auth_transactions(stateHash);

CREATE TABLE IF NOT EXISTS cf_external_oauth_transactions (
  id TEXT PRIMARY KEY NOT NULL CHECK (length(id) BETWEEN 1 AND 256),
  appId TEXT NOT NULL CHECK (length(appId) BETWEEN 1 AND 256),
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  stateHash TEXT NOT NULL UNIQUE,
  csrfHash TEXT NOT NULL UNIQUE,
  redirectUrl TEXT NOT NULL,
  appCatalogRevision INTEGER NOT NULL CHECK (appCatalogRevision >= 1),
  appPolicyJson TEXT NOT NULL
    CHECK (json_valid(appPolicyJson) AND json_type(appPolicyJson) = 'object'),
  setupTargetHash TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending', 'consumed', 'failed')),
  expiresAt INTEGER NOT NULL,
  createdAt INTEGER NOT NULL,
  consumedAt INTEGER,
  CHECK (expiresAt > createdAt),
  CHECK (
    (status = 'consumed' AND consumedAt IS NOT NULL)
    OR (status <> 'consumed' AND consumedAt IS NULL)
  )
);

CREATE INDEX IF NOT EXISTS cf_external_oauth_transactions_user_idx
  ON cf_external_oauth_transactions(uid, status, expiresAt);
CREATE INDEX IF NOT EXISTS cf_external_oauth_transactions_app_idx
  ON cf_external_oauth_transactions(appId, status, expiresAt);
