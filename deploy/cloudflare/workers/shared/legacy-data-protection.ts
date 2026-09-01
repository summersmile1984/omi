/**
 * Strict reader for the legacy Python data-protection envelope.
 *
 * This module is deliberately not wired into a route or a D1 reader yet. It
 * is the small, testable Web Crypto contract that a future migration executor
 * must use. In particular, malformed/authentication-failed values are errors;
 * returning the input value would turn an opaque ciphertext into plaintext on
 * a subsequent write.
 */

const LEGACY_SECRET_MIN_BYTES = 32;
const LEGACY_NONCE_BYTES = 12;
const LEGACY_TAG_BYTES = 16;
const MAX_CIPHERTEXT_BYTES = 16 * 1024 * 1024;
const LEGACY_INFO = "user-data-encryption";

declare const legacyCiphertextBrand: unique symbol;

/** A value that passed the legacy standard-Base64/envelope shape checks. */
export type LegacyCiphertext = string & {
  readonly [legacyCiphertextBrand]: "LegacyCiphertext";
};

export class LegacyDataProtectionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LegacyDataProtectionError";
  }
}

function bytes(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}

function arrayBuffer(value: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(value.byteLength);
  copy.set(value);
  return copy.buffer;
}

function standardBase64(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function parseStandardBase64(value: unknown): Uint8Array {
  if (typeof value !== "string" || value.length === 0) {
    throw new LegacyDataProtectionError("legacy data-protection value is empty");
  }
  // atob accepts URL-safe characters and non-canonical padding in some
  // runtimes. The Python producer uses standard Base64, so reject those forms
  // before decoding and compare the canonical re-encoding afterwards.
  if (
    value.length % 4 !== 0 ||
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)
  ) {
    throw new LegacyDataProtectionError("legacy data-protection value is not standard Base64");
  }
  let decoded: Uint8Array;
  try {
    const binary = atob(value);
    decoded = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    throw new LegacyDataProtectionError("legacy data-protection value is not decodable");
  }
  if (
    decoded.byteLength < LEGACY_NONCE_BYTES + LEGACY_TAG_BYTES ||
    decoded.byteLength > MAX_CIPHERTEXT_BYTES ||
    standardBase64(decoded) !== value
  ) {
    throw new LegacyDataProtectionError("legacy data-protection envelope is invalid");
  }
  return decoded;
}

/**
 * Validate and brand a non-empty legacy ciphertext. Empty values are the
 * legacy representation of an absent optional field and return null.
 */
export function parseLegacyCiphertext(value: unknown): LegacyCiphertext | null {
  if (value === "") return null;
  parseStandardBase64(value);
  return value as LegacyCiphertext;
}

function validUid(uid: string): boolean {
  return typeof uid === "string" && uid.length > 0 && uid.length <= 256 && !uid.includes("\0");
}

async function deriveKey(
  secret: string,
  uid: string,
  usages: KeyUsage[],
): Promise<CryptoKey> {
  const secretBytes = bytes(secret);
  if (secretBytes.byteLength < LEGACY_SECRET_MIN_BYTES) {
    throw new LegacyDataProtectionError("legacy data-protection secret is not configured");
  }
  if (!validUid(uid)) {
    throw new LegacyDataProtectionError("legacy data-protection uid is invalid");
  }
  const base = await crypto.subtle.importKey(
    "raw",
    arrayBuffer(secretBytes),
    "HKDF",
    false,
    ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    {
      name: "HKDF",
      hash: "SHA-256",
      salt: arrayBuffer(bytes(uid)),
      info: arrayBuffer(bytes(LEGACY_INFO)),
    },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    usages,
  );
}

/**
 * Decrypt a Python ``backend/utils/encryption.py`` value. Authentication and
 * UTF-8 failures are terminal and never fall back to returning the input.
 */
export async function decryptLegacyDataProtection(
  secret: string,
  uid: string,
  value: unknown,
): Promise<string> {
  const ciphertext = parseLegacyCiphertext(value);
  if (ciphertext === null) return "";
  const decoded = parseStandardBase64(ciphertext);
  try {
    const clear = await crypto.subtle.decrypt(
      {
        name: "AES-GCM",
        iv: arrayBuffer(decoded.subarray(0, LEGACY_NONCE_BYTES)),
      },
      await deriveKey(secret, uid, ["decrypt"]),
      arrayBuffer(decoded.subarray(LEGACY_NONCE_BYTES)),
    );
    return new TextDecoder("utf-8", { fatal: true }).decode(clear);
  } catch {
    throw new LegacyDataProtectionError("legacy data-protection authentication failed");
  }
}

/** Encrypt using the exact legacy Python envelope for cross-runtime fixtures. */
export async function encryptLegacyDataProtection(
  secret: string,
  uid: string,
  plaintext: string,
): Promise<string> {
  if (typeof plaintext !== "string") {
    throw new LegacyDataProtectionError("legacy data-protection plaintext is invalid");
  }
  if (plaintext === "") return "";
  const nonce = crypto.getRandomValues(new Uint8Array(LEGACY_NONCE_BYTES));
  const encrypted = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: arrayBuffer(nonce) },
    await deriveKey(secret, uid, ["encrypt"]),
    arrayBuffer(bytes(plaintext)),
  );
  const payload = new Uint8Array(LEGACY_NONCE_BYTES + encrypted.byteLength);
  payload.set(nonce);
  payload.set(new Uint8Array(encrypted), LEGACY_NONCE_BYTES);
  return standardBase64(payload);
}

export const LEGACY_DATA_PROTECTION_CONTRACT = Object.freeze({
  info: LEGACY_INFO,
  nonceBytes: LEGACY_NONCE_BYTES,
  tagBytes: LEGACY_TAG_BYTES,
  secretMinBytes: LEGACY_SECRET_MIN_BYTES,
  maxCiphertextBytes: MAX_CIPHERTEXT_BYTES,
});
