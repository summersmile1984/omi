-- Optional encrypted metadata for a native-auth session transaction.
--
-- The legacy session contains the caller's redirect URI and opaque state.  A
-- callback must recover those values after consuming the provider-facing
-- state, but neither value belongs in plaintext D1.  Keep this separate from
-- encryptedPayload: code rows carry provider credentials there, while session
-- rows carry only this bounded, secret-derived AES-GCM envelope.
ALTER TABLE cf_legacy_auth_transactions
  ADD COLUMN metadataEnvelopeEnc TEXT
  CHECK (
    metadataEnvelopeEnc IS NULL OR
    (length(metadataEnvelopeEnc) BETWEEN 20 AND 400000
      AND metadataEnvelopeEnc LIKE 'v1.%')
  );

CREATE INDEX IF NOT EXISTS cf_legacy_auth_transactions_metadata_idx
  ON cf_legacy_auth_transactions(kind, status, expiresAt);
