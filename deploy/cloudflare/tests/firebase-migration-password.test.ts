import { describe, expect, it, vi } from "vitest";
import {
  encodeFirebasePasswordHash,
  FirebasePasswordMigrationConfigurationError,
  hashPassword,
  isFirebasePasswordHash,
  parseFirebaseScryptConfig,
  upgradeMigratedFirebasePassword,
  verifyMigratedFirebasePassword,
  verifyPassword,
} from "../workers/auth/firebase-migration-password";

const OFFICIAL_FIREBASE_SAMPLE = Object.freeze({
  config: {
    algorithm: "SCRYPT",
    base64_signer_key:
      "jxspr8Ki0RYycVU8zykbdLGjFQ3McFUH0uiiTvC8pVMXAn210wjLNmdZJzxUECKbm0QsEmYUSDzZvpjeJ9WmXA==",
    base64_salt_separator: "Bw==",
    rounds: 8,
    mem_cost: 14,
  },
  passwordSalt: "42xEC+ixf3L2lw==",
  passwordHash:
    "lSrfV15cpx95/sZS2W9c9Kp6i/LVgQNDNC/qzrCnh1SAyZvqmZqAjTdn3aoItz+VHjoZilo78198JAdRuid5lQ==",
  password: "user1password",
});

function envFor(config: typeof OFFICIAL_FIREBASE_SAMPLE.config) {
  return {
    AUTH_FIREBASE_SCRYPT_SIGNER_KEY: config.base64_signer_key,
    AUTH_FIREBASE_SCRYPT_SALT_SEPARATOR: config.base64_salt_separator,
    AUTH_FIREBASE_SCRYPT_ROUNDS: String(config.rounds),
    AUTH_FIREBASE_SCRYPT_MEM_COST: String(config.mem_cost),
  };
}

function passwordDatabase(
  rows: Array<{
    id: string;
    userId: string;
    providerId: string;
    password: string;
    createdAt?: number;
    updatedAt?: string;
  }>,
  options: { concurrentPassword?: string; failUpdate?: boolean } = {},
) {
  const statement = (query: string, values: unknown[] = []) => ({
    bind: (...nextValues: unknown[]) => statement(query, nextValues),
    all: vi.fn(async () => ({
      success: true,
      results: rows
        .filter(
          (row) => row.userId === values[0] && row.providerId === "credential",
        )
        .sort(
          (left, right) =>
            (left.createdAt || 0) - (right.createdAt || 0) ||
            left.id.localeCompare(right.id),
        )
        .map(({ id, password }) => ({ id, password })),
      meta: {},
    })),
    first: vi.fn(async () => {
      const row = rows.find((candidate) => candidate.id === values[0]);
      return row ? { id: row.id, password: row.password } : null;
    }),
    run: vi.fn(async () => {
      if (options.failUpdate) throw new Error("D1 unavailable");
      const [replacement, updatedAt, id, expected] = values;
      const row = rows.find((candidate) => candidate.id === id);
      if (options.concurrentPassword && row) {
        row.password = options.concurrentPassword;
        return { success: true, meta: { changes: 0 } };
      }
      const changes = row?.password === expected ? 1 : 0;
      if (changes && row) {
        row.password = String(replacement);
        row.updatedAt = String(updatedAt);
      }
      return { success: true, meta: { changes } };
    }),
  });
  return {
    database: {
      prepare: vi.fn((query: string) => statement(query)),
    } as unknown as D1Database,
    rows,
  };
}

describe("Firebase password migration", () => {
  it("verifies the password sample published by Firebase", async () => {
    const config = parseFirebaseScryptConfig(OFFICIAL_FIREBASE_SAMPLE.config);
    const hash = encodeFirebasePasswordHash(OFFICIAL_FIREBASE_SAMPLE, config);

    expect(isFirebasePasswordHash(hash)).toBe(true);
    await expect(
      verifyMigratedFirebasePassword(
        { hash, password: OFFICIAL_FIREBASE_SAMPLE.password },
        envFor(OFFICIAL_FIREBASE_SAMPLE.config),
      ),
    ).resolves.toBe(true);
    await expect(
      verifyMigratedFirebasePassword(
        { hash, password: "definitely-wrong" },
        envFor(OFFICIAL_FIREBASE_SAMPLE.config),
      ),
    ).resolves.toBe(false);
  });

  it("fails closed when migration configuration is absent or mismatched", async () => {
    const config = parseFirebaseScryptConfig(OFFICIAL_FIREBASE_SAMPLE.config);
    const hash = encodeFirebasePasswordHash(OFFICIAL_FIREBASE_SAMPLE, config);

    await expect(
      verifyMigratedFirebasePassword({ hash, password: "secret" }, {}),
    ).rejects.toBeInstanceOf(FirebasePasswordMigrationConfigurationError);
    await expect(
      verifyMigratedFirebasePassword(
        { hash, password: "secret" },
        {
          ...envFor(OFFICIAL_FIREBASE_SAMPLE.config),
          AUTH_FIREBASE_SCRYPT_ROUNDS: "7",
        },
      ),
    ).rejects.toThrow(/fingerprint does not match/);
  });

  it("keeps new Better Auth passwords native while accepting migrated envelopes", async () => {
    const nativeHash = await hashPassword("new-account-password");
    expect(isFirebasePasswordHash(nativeHash)).toBe(false);
    await expect(
      verifyPassword(
        { hash: nativeHash, password: "new-account-password" },
        {},
      ),
    ).resolves.toBe(true);
    await expect(
      verifyPassword({ hash: nativeHash, password: "wrong-password" }, {}),
    ).resolves.toBe(false);
  });

  it("rejects unsafe or incomplete Firebase scrypt parameters", () => {
    expect(() =>
      parseFirebaseScryptConfig({
        ...OFFICIAL_FIREBASE_SAMPLE.config,
        rounds: 9,
      }),
    ).toThrow(/rounds must be an integer/);
    expect(() =>
      parseFirebaseScryptConfig({
        ...OFFICIAL_FIREBASE_SAMPLE.config,
        rounds: 0,
      }),
    ).toThrow(/rounds must be an integer/);
    expect(() =>
      parseFirebaseScryptConfig({
        ...OFFICIAL_FIREBASE_SAMPLE.config,
        mem_cost: 18,
      }),
    ).toThrow(/memory budget/);
    expect(() =>
      encodeFirebasePasswordHash(
        { passwordHash: "AQ==", passwordSalt: "Ag==" },
        parseFirebaseScryptConfig(OFFICIAL_FIREBASE_SAMPLE.config),
      ),
    ).toThrow(/hash length must match/);
  });

  it("upgrades the only migrated credential with a conditional native hash write", async () => {
    const database = passwordDatabase([
      {
        id: "credential-1",
        userId: "firebase-user",
        providerId: "credential",
        password: "firebase-scrypt-v1$fingerprint$salt$hash$",
      },
    ]);
    const nativeHash = vi.fn(async () => "native-better-auth-hash");

    await expect(
      upgradeMigratedFirebasePassword(
        database.database,
        "firebase-user",
        "verified-password",
        nativeHash,
      ),
    ).resolves.toBe("upgraded");
    expect(nativeHash).toHaveBeenCalledWith("verified-password");
    expect(database.rows[0].password).toBe("native-better-auth-hash");
    expect(database.rows[0].updatedAt).toMatch(
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/,
    );
    await expect(
      upgradeMigratedFirebasePassword(
        database.database,
        "firebase-user",
        "verified-password",
        nativeHash,
      ),
    ).resolves.toBe("not_migrated");
    expect(nativeHash).toHaveBeenCalledTimes(1);
  });

  it("treats a concurrent native upgrade as an idempotent success", async () => {
    const database = passwordDatabase(
      [
        {
          id: "credential-1",
          userId: "firebase-user",
          providerId: "credential",
          password: "firebase-scrypt-v1$fingerprint$salt$hash$",
        },
      ],
      { concurrentPassword: "native-from-other-login" },
    );

    await expect(
      upgradeMigratedFirebasePassword(
        database.database,
        "firebase-user",
        "verified-password",
        async () => "native-from-this-login",
      ),
    ).resolves.toBe("already_upgraded");
    expect(database.rows[0].password).toBe("native-from-other-login");
  });

  it("leaves the migrated hash untouched when D1 cannot persist the upgrade", async () => {
    const original = "firebase-scrypt-v1$fingerprint$salt$hash$";
    const database = passwordDatabase(
      [
        {
          id: "credential-1",
          userId: "firebase-user",
          providerId: "credential",
          password: original,
        },
      ],
      { failUpdate: true },
    );

    await expect(
      upgradeMigratedFirebasePassword(
        database.database,
        "firebase-user",
        "verified-password",
        async () => "native-better-auth-hash",
      ),
    ).rejects.toThrow("D1 unavailable");
    expect(database.rows[0].password).toBe(original);
  });

  it("refuses to guess which password to replace when credential rows are duplicated", async () => {
    const database = passwordDatabase([
      {
        id: "credential-1",
        userId: "firebase-user",
        providerId: "credential",
        password: "firebase-scrypt-v1$fingerprint$salt$hash$",
      },
      {
        id: "credential-2",
        userId: "firebase-user",
        providerId: "credential",
        password: "another-hash",
      },
    ]);
    const nativeHash = vi.fn(async () => "native-better-auth-hash");

    await expect(
      upgradeMigratedFirebasePassword(
        database.database,
        "firebase-user",
        "verified-password",
        nativeHash,
      ),
    ).rejects.toThrow(/multiple credential accounts/);
    expect(nativeHash).not.toHaveBeenCalled();
  });
});
