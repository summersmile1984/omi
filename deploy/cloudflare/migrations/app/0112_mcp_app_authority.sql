-- Foundation only for the legacy "install an external MCP server as an app"
-- flow. No route reads these tables yet. Keep this authority separate from
-- Better Auth's oauthClient/oauthAccessToken tables: those tables authorize a
-- client to call Omi's MCP server and cannot store an app's upstream provider
-- credentials or discovery state.

CREATE TABLE IF NOT EXISTS cf_mcp_app_connections (
  app_id TEXT PRIMARY KEY NOT NULL
    CHECK (length(app_id) BETWEEN 1 AND 256),
  owner_uid TEXT NOT NULL
    CHECK (length(owner_uid) BETWEEN 1 AND 256),
  server_url TEXT NOT NULL
    CHECK (length(server_url) BETWEEN 9 AND 2048),
  resolved_endpoint TEXT
    CHECK (resolved_endpoint IS NULL OR length(resolved_endpoint) BETWEEN 9 AND 2048),
  status TEXT NOT NULL DEFAULT 'unauthorized'
    CHECK (status IN ('unauthorized', 'pending', 'authorized', 'reauthorize', 'failed')),
  oauth_metadata_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(oauth_metadata_json) AND length(oauth_metadata_json) <= 100000),
  -- AES-GCM v1 envelope; plaintext client/access/refresh tokens are forbidden
  -- by the route contract and must never be placed in cf_app_catalog.data_json.
  credential_envelope_enc TEXT
    CHECK (credential_envelope_enc IS NULL OR
      (length(credential_envelope_enc) BETWEEN 20 AND 400000
       AND credential_envelope_enc LIKE 'v1.%')),
  credential_key_version INTEGER NOT NULL DEFAULT 1
    CHECK (credential_key_version BETWEEN 1 AND 32),
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2_000),
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_mcp_app_connections_owner_status_idx
  ON cf_mcp_app_connections(owner_uid, status, updated_at DESC, app_id);

-- state_hash is the SHA-256 of the opaque callback state. The verifier and
-- registration credentials are encrypted envelopes, so a callback can be
-- consumed without putting OAuth secrets in a URL or catalog projection.
CREATE TABLE IF NOT EXISTS cf_mcp_app_oauth_transactions (
  transaction_id TEXT PRIMARY KEY NOT NULL
    CHECK (length(transaction_id) BETWEEN 1 AND 128),
  app_id TEXT NOT NULL
    CHECK (length(app_id) BETWEEN 1 AND 256),
  owner_uid TEXT NOT NULL
    CHECK (length(owner_uid) BETWEEN 1 AND 256),
  state_hash TEXT NOT NULL UNIQUE
    CHECK (length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
  code_verifier_enc TEXT NOT NULL
    CHECK (length(code_verifier_enc) BETWEEN 20 AND 400000
      AND code_verifier_enc LIKE 'v1.%'),
  client_credentials_enc TEXT
    CHECK (client_credentials_enc IS NULL OR
      (length(client_credentials_enc) BETWEEN 20 AND 400000
       AND client_credentials_enc LIKE 'v1.%')),
  authorization_endpoint TEXT NOT NULL
    CHECK (length(authorization_endpoint) BETWEEN 9 AND 2048),
  token_endpoint TEXT NOT NULL
    CHECK (length(token_endpoint) BETWEEN 9 AND 2048),
  registration_endpoint TEXT
    CHECK (registration_endpoint IS NULL OR length(registration_endpoint) BETWEEN 9 AND 2048),
  client_id TEXT NOT NULL CHECK (length(client_id) BETWEEN 1 AND 2048),
  redirect_uri TEXT NOT NULL CHECK (length(redirect_uri) BETWEEN 9 AND 2048),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'exchanged', 'failed', 'expired')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  expires_at INTEGER NOT NULL CHECK (expires_at >= 0),
  consumed_at INTEGER CHECK (consumed_at IS NULL OR consumed_at >= 0),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2_000),
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_mcp_app_oauth_transactions_owner_expiry_idx
  ON cf_mcp_app_oauth_transactions(owner_uid, expires_at, status, transaction_id);

-- Discovery is kept as a separate projection so a failed/partial provider
-- refresh never overwrites the last known good tool list. The route must use
-- owner_uid + revision in its UPDATE predicate and invalidate the public app
-- projection only after the D1 write succeeds.
CREATE TABLE IF NOT EXISTS cf_mcp_app_discoveries (
  app_id TEXT PRIMARY KEY NOT NULL
    CHECK (length(app_id) BETWEEN 1 AND 256),
  owner_uid TEXT NOT NULL
    CHECK (length(owner_uid) BETWEEN 1 AND 256),
  endpoint TEXT NOT NULL CHECK (length(endpoint) BETWEEN 9 AND 2048),
  protocol_version TEXT NOT NULL CHECK (length(protocol_version) BETWEEN 1 AND 64),
  tools_json TEXT NOT NULL
    CHECK (json_valid(tools_json) AND json_type(tools_json) = 'array'
      AND length(tools_json) <= 2_000_000),
  provider_etag TEXT CHECK (provider_etag IS NULL OR length(provider_etag) <= 512),
  provider_session_id_enc TEXT
    CHECK (provider_session_id_enc IS NULL OR
      (length(provider_session_id_enc) BETWEEN 20 AND 400000
       AND provider_session_id_enc LIKE 'v1.%')),
  status TEXT NOT NULL DEFAULT 'ready'
    CHECK (status IN ('ready', 'failed')),
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2_000),
  fetched_at INTEGER NOT NULL CHECK (fetched_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= fetched_at),
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_mcp_app_discoveries_owner_status_idx
  ON cf_mcp_app_discoveries(owner_uid, status, updated_at DESC, app_id);

CREATE TRIGGER IF NOT EXISTS adf_i_mcp_app_connections
BEFORE INSERT ON cf_mcp_app_connections
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.owner_uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.owner_uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_mcp_app_connections
BEFORE UPDATE ON cf_mcp_app_connections
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.owner_uid, NEW.owner_uid)
)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.owner_uid, NEW.owner_uid)
  )
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_mcp_app_oauth_transactions
BEFORE INSERT ON cf_mcp_app_oauth_transactions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.owner_uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.owner_uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_mcp_app_oauth_transactions
BEFORE UPDATE ON cf_mcp_app_oauth_transactions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.owner_uid, NEW.owner_uid)
)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.owner_uid, NEW.owner_uid)
  )
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_mcp_app_discoveries
BEFORE INSERT ON cf_mcp_app_discoveries
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.owner_uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.owner_uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_mcp_app_discoveries
BEFORE UPDATE ON cf_mcp_app_discoveries
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.owner_uid, NEW.owner_uid)
)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.owner_uid, NEW.owner_uid)
  )
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
