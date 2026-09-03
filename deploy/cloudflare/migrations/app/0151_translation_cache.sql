-- Content-addressed translation cache (port of the legacy Redis
-- RedisTranslationStore policy, default 14-day TTL). Keys are a SHA-256
-- fingerprint over model, source language, and exact content, so the cache
-- carries no user identity and no readable text beyond what the caller
-- already supplied; identical segments across users share one Workers AI
-- spend. Expired rows are pruned by request traffic.
CREATE TABLE IF NOT EXISTS cf_translation_cache (
  fingerprint TEXT NOT NULL CHECK (
    length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  target_language TEXT NOT NULL CHECK (length(target_language) BETWEEN 2 AND 32),
  translated_text TEXT NOT NULL,
  detected_language TEXT NOT NULL DEFAULT '',
  expires_at INTEGER NOT NULL CHECK (expires_at > 0),
  created_at INTEGER NOT NULL,
  PRIMARY KEY (fingerprint, target_language)
);

CREATE INDEX IF NOT EXISTS cf_translation_cache_expiry_idx
  ON cf_translation_cache(expires_at);
