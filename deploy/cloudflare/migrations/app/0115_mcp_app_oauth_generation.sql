-- Bind an authorized connection to the exact OAuth transaction that created it.
-- This prevents a slow callback from an older authorization attempt from
-- overwriting a newer pending connection after the old transaction was used.
ALTER TABLE cf_mcp_app_connections
  ADD COLUMN oauth_transaction_id TEXT;

CREATE INDEX IF NOT EXISTS cf_mcp_app_connections_oauth_transaction_idx
  ON cf_mcp_app_connections(oauth_transaction_id);
