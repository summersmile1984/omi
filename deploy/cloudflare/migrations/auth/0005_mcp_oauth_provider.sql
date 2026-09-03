-- Better Auth OAuth Provider schema generated from @better-auth/mcp 1.7.2.
-- Arrays and JSON values are persisted as JSON text by Better Auth's D1 adapter.

CREATE TABLE IF NOT EXISTS oauthClient (
  id TEXT PRIMARY KEY NOT NULL,
  clientId TEXT NOT NULL UNIQUE,
  clientSecret TEXT,
  clientDiscoveryId TEXT,
  disabled INTEGER,
  skipConsent INTEGER,
  enableEndSession INTEGER,
  subjectType TEXT,
  scopes TEXT,
  clientCredentialsScopes TEXT,
  userId TEXT REFERENCES user(id) ON DELETE CASCADE,
  createdAt INTEGER,
  updatedAt INTEGER,
  name TEXT,
  uri TEXT,
  icon TEXT,
  contacts TEXT,
  tos TEXT,
  policy TEXT,
  softwareId TEXT,
  softwareVersion TEXT,
  softwareStatement TEXT,
  redirectUris TEXT NOT NULL,
  postLogoutRedirectUris TEXT,
  backchannelLogoutUri TEXT,
  backchannelLogoutSessionRequired INTEGER,
  tokenEndpointAuthMethod TEXT,
  applicationType TEXT,
  jwks TEXT,
  jwksUri TEXT,
  grantTypes TEXT,
  responseTypes TEXT,
  requirePKCE INTEGER,
  dpopBoundAccessTokens INTEGER,
  referenceId TEXT,
  metadata TEXT
);

CREATE TABLE IF NOT EXISTS oauthResource (
  id TEXT PRIMARY KEY NOT NULL,
  identifier TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  accessTokenTtl INTEGER,
  refreshTokenTtl INTEGER,
  signingAlgorithm TEXT,
  signingKeyId TEXT,
  allowedScopes TEXT,
  customClaims TEXT,
  dpopBoundAccessTokensRequired INTEGER,
  disabled INTEGER,
  createdAt INTEGER,
  updatedAt INTEGER,
  policyVersion INTEGER,
  metadata TEXT
);

CREATE TABLE IF NOT EXISTS oauthClientResource (
  id TEXT PRIMARY KEY NOT NULL,
  clientId TEXT NOT NULL REFERENCES oauthClient(clientId) ON DELETE CASCADE,
  resourceId TEXT NOT NULL REFERENCES oauthResource(identifier) ON DELETE CASCADE,
  metadata TEXT,
  createdAt INTEGER
);

CREATE TABLE IF NOT EXISTS oauthRefreshToken (
  id TEXT PRIMARY KEY NOT NULL,
  token TEXT NOT NULL UNIQUE,
  clientId TEXT NOT NULL REFERENCES oauthClient(clientId) ON DELETE CASCADE,
  sessionId TEXT REFERENCES session(id) ON DELETE SET NULL,
  userId TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  referenceId TEXT,
  authorizationCodeId TEXT,
  resources TEXT,
  requestedUserInfoClaims TEXT,
  expiresAt INTEGER NOT NULL,
  createdAt INTEGER NOT NULL,
  revoked INTEGER,
  rotatedAt INTEGER,
  rotationReplayResponse TEXT,
  rotationReplayExpiresAt INTEGER,
  authTime INTEGER,
  confirmation TEXT,
  scopes TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauthAccessToken (
  id TEXT PRIMARY KEY NOT NULL,
  token TEXT NOT NULL UNIQUE,
  clientId TEXT NOT NULL REFERENCES oauthClient(clientId) ON DELETE CASCADE,
  sessionId TEXT REFERENCES session(id) ON DELETE SET NULL,
  userId TEXT REFERENCES user(id) ON DELETE CASCADE,
  referenceId TEXT,
  authorizationCodeId TEXT,
  resources TEXT,
  requestedUserInfoClaims TEXT,
  refreshId TEXT REFERENCES oauthRefreshToken(id) ON DELETE CASCADE,
  expiresAt INTEGER NOT NULL,
  createdAt INTEGER NOT NULL,
  revoked INTEGER,
  confirmation TEXT,
  scopes TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauthConsent (
  id TEXT PRIMARY KEY NOT NULL,
  clientId TEXT NOT NULL REFERENCES oauthClient(clientId) ON DELETE CASCADE,
  userId TEXT REFERENCES user(id) ON DELETE CASCADE,
  referenceId TEXT,
  resources TEXT,
  requestedUserInfoClaims TEXT,
  scopes TEXT NOT NULL,
  createdAt INTEGER NOT NULL,
  updatedAt INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauthClientAssertion (
  id TEXT PRIMARY KEY NOT NULL,
  expiresAt INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS oauthClient_userId_idx ON oauthClient(userId);
CREATE INDEX IF NOT EXISTS oauthClientResource_clientId_idx ON oauthClientResource(clientId);
CREATE INDEX IF NOT EXISTS oauthClientResource_resourceId_idx ON oauthClientResource(resourceId);
CREATE UNIQUE INDEX IF NOT EXISTS oauthClientResource_clientId_resourceId_uidx
  ON oauthClientResource(clientId, resourceId);
CREATE INDEX IF NOT EXISTS oauthRefreshToken_clientId_idx ON oauthRefreshToken(clientId);
CREATE INDEX IF NOT EXISTS oauthRefreshToken_sessionId_idx ON oauthRefreshToken(sessionId);
CREATE INDEX IF NOT EXISTS oauthRefreshToken_userId_idx ON oauthRefreshToken(userId);
CREATE INDEX IF NOT EXISTS oauthRefreshToken_authorizationCodeId_idx
  ON oauthRefreshToken(authorizationCodeId);
CREATE INDEX IF NOT EXISTS oauthAccessToken_clientId_idx ON oauthAccessToken(clientId);
CREATE INDEX IF NOT EXISTS oauthAccessToken_sessionId_idx ON oauthAccessToken(sessionId);
CREATE INDEX IF NOT EXISTS oauthAccessToken_userId_idx ON oauthAccessToken(userId);
CREATE INDEX IF NOT EXISTS oauthAccessToken_authorizationCodeId_idx
  ON oauthAccessToken(authorizationCodeId);
CREATE INDEX IF NOT EXISTS oauthAccessToken_refreshId_idx ON oauthAccessToken(refreshId);
CREATE INDEX IF NOT EXISTS oauthConsent_clientId_idx ON oauthConsent(clientId);
CREATE INDEX IF NOT EXISTS oauthConsent_userId_idx ON oauthConsent(userId);
