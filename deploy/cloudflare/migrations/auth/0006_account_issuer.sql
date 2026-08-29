-- Better Auth 1.7 keys accounts by the verified issuer plus provider account
-- identifier. Rebuild the original table so the new identity boundary is
-- enforced for both migrated and newly created accounts.

CREATE TABLE account_v2 (
  id TEXT PRIMARY KEY NOT NULL,
  issuer TEXT NOT NULL,
  accountId TEXT NOT NULL,
  providerId TEXT NOT NULL,
  userId TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  accessToken TEXT,
  refreshToken TEXT,
  idToken TEXT,
  accessTokenExpiresAt INTEGER,
  refreshTokenExpiresAt INTEGER,
  scope TEXT,
  password TEXT,
  createdAt INTEGER NOT NULL,
  updatedAt INTEGER NOT NULL
);

INSERT INTO account_v2 (
  id,
  issuer,
  accountId,
  providerId,
  userId,
  accessToken,
  refreshToken,
  idToken,
  accessTokenExpiresAt,
  refreshTokenExpiresAt,
  scope,
  password,
  createdAt,
  updatedAt
)
SELECT
  id,
  CASE providerId
    WHEN 'credential' THEN 'local:credential'
    WHEN 'google' THEN 'https://accounts.google.com'
    WHEN 'apple' THEN 'https://appleid.apple.com'
    ELSE 'local:oauth:' || providerId
  END,
  accountId,
  providerId,
  userId,
  accessToken,
  refreshToken,
  idToken,
  accessTokenExpiresAt,
  refreshTokenExpiresAt,
  scope,
  password,
  createdAt,
  updatedAt
FROM account;

DROP TABLE account;
ALTER TABLE account_v2 RENAME TO account;

CREATE INDEX account_userId_idx ON account(userId);
CREATE UNIQUE INDEX account_issuer_accountId_uidx ON account(issuer, accountId);
