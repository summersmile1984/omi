import {
  hashPassword as hashBetterAuthPassword,
  verifyPassword as verifyBetterAuthPassword,
} from "better-auth/crypto";
import {
  isFirebasePasswordHash,
  serverFirebaseScrypt,
} from "../../auth/shared/firebase-scrypt.mjs";

export async function hashPassword(password) {
  return hashBetterAuthPassword(password);
}

export async function verifyPassword({ hash, password }) {
  if (isFirebasePasswordHash(hash)) {
    return serverFirebaseScrypt.verify({ hash, password }, process.env);
  }
  return verifyBetterAuthPassword({ hash, password });
}
