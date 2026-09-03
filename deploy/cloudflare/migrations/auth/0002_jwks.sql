CREATE TABLE IF NOT EXISTS jwks (
  id TEXT PRIMARY KEY NOT NULL,
  publicKey TEXT NOT NULL,
  privateKey TEXT NOT NULL,
  createdAt INTEGER NOT NULL,
  expiresAt INTEGER,
  alg TEXT,
  crv TEXT
);
