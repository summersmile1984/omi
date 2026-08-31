-- Firebase custom-token bridge audit authority.  The bridge stores no
-- credential or token plaintext; only a short-lived issuance id/hash is
-- retained so a deletion fence can race-proof the final issuance update.

CREATE TABLE IF NOT EXISTS cf_firebase_bridge_issuances (
  issuanceId TEXT PRIMARY KEY NOT NULL CHECK (length(issuanceId) BETWEEN 1 AND 128),
  firebaseUid TEXT NOT NULL
    REFERENCES cf_firebase_identity_projection(firebaseUid) ON DELETE RESTRICT,
  betterAuthUserId TEXT NOT NULL
    REFERENCES user(id) ON DELETE RESTRICT,
  accountGeneration INTEGER NOT NULL CHECK (accountGeneration >= 1),
  status TEXT NOT NULL CHECK (status IN ('reserved', 'issued', 'failed')),
  issuedAt INTEGER NOT NULL,
  expiresAt INTEGER NOT NULL CHECK (expiresAt > issuedAt),
  tokenHash TEXT UNIQUE,
  lastError TEXT CHECK (lastError IS NULL OR length(lastError) <= 128)
);

CREATE INDEX IF NOT EXISTS cf_firebase_bridge_issuances_user_idx
  ON cf_firebase_bridge_issuances(betterAuthUserId, accountGeneration, issuedAt DESC);
CREATE INDEX IF NOT EXISTS cf_firebase_bridge_issuances_expiry_idx
  ON cf_firebase_bridge_issuances(status, expiresAt);

-- A reserved/issued token must never be created or finalized once account
-- deletion has started.  Failed rows remain writable for reconciliation.
CREATE TRIGGER IF NOT EXISTS fbt_i_firebase_bridge_issuances
BEFORE INSERT ON cf_firebase_bridge_issuances
WHEN NEW.status IN ('reserved', 'issued')
AND EXISTS (
  SELECT 1 FROM cf_auth_deletion_fences
   WHERE uid = NEW.betterAuthUserId AND status <> 'clear'
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS fbt_u_firebase_bridge_issuances
BEFORE UPDATE ON cf_firebase_bridge_issuances
WHEN NEW.status IN ('reserved', 'issued')
AND EXISTS (
  SELECT 1 FROM cf_auth_deletion_fences
   WHERE uid = NEW.betterAuthUserId AND status <> 'clear'
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
