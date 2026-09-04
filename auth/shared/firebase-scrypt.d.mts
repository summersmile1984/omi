export type FirebaseScryptEnvironment = {
  AUTH_FIREBASE_SCRYPT_SIGNER_KEY?: string;
  AUTH_FIREBASE_SCRYPT_SALT_SEPARATOR?: string;
  AUTH_FIREBASE_SCRYPT_ROUNDS?: string;
  AUTH_FIREBASE_SCRYPT_MEM_COST?: string;
};
export type FirebaseScryptConfig = {
  algorithm: "SCRYPT";
  signerKey: Uint8Array;
  saltSeparator: Uint8Array;
  rounds: number;
  memCost: number;
  fingerprint: string;
};
export type PasswordCredentials = { hash: string; password: string };
export type FirebaseScryptPolicy = {
  parseConfig(raw: unknown): FirebaseScryptConfig;
  fromEnv(env: FirebaseScryptEnvironment): FirebaseScryptConfig | null;
  verify(
    credentials: PasswordCredentials,
    env: FirebaseScryptEnvironment,
  ): Promise<boolean>;
};
export class FirebasePasswordMigrationConfigurationError extends Error {}
export function encodeFirebasePasswordHash(
  credentials: { passwordHash: string; passwordSalt: string },
  config: FirebaseScryptConfig,
): string;
export function isFirebasePasswordHash(hash: unknown): hash is string;
export function hashFirebasePassword(
  password: string,
  salt: Uint8Array,
  config: FirebaseScryptConfig,
): Promise<Uint8Array | null>;
export const serverFirebaseScrypt: FirebaseScryptPolicy;
export const workersFirebaseScrypt: FirebaseScryptPolicy;
