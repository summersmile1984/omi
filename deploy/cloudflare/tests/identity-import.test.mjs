import { chmod, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { describe, expect, it, vi } from "vitest";
import {
  createCloudflareD1Client,
  FirebaseIdentityMigrationError,
  parseFirebaseImportScryptConfig,
  planFirebaseIdentityImport,
  runIdentityImport,
} from "../scripts/import-firebase-identities.mjs";
import {
  encodeFirebasePasswordHash,
  parseFirebaseScryptConfig,
} from "../workers/auth/firebase-migration-password";

const FIREBASE_SAMPLE = Object.freeze({
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
});

function source() {
  return {
    users: [
      {
        localId: "firebase-uid-1",
        email: "Owner@Example.COM",
        emailVerified: true,
        displayName: "Owner",
        photoUrl: "https://images.example.test/owner.png",
        passwordHash: FIREBASE_SAMPLE.passwordHash,
        salt: FIREBASE_SAMPLE.passwordSalt,
        createdAt: "1700000000000",
        lastSignedInAt: "1700000100000",
        providerUserInfo: [
          { providerId: "google.com", rawId: "google-sub-1" },
          { providerId: "apple.com", rawId: "apple-sub-1" },
        ],
      },
    ],
  };
}

const providerEnv = {
  GOOGLE_CLIENT_ID: "google-id",
  GOOGLE_CLIENT_SECRET: "google-secret",
  APPLE_CLIENT_ID: "apple-id",
  APPLE_CLIENT_SECRET: "apple-secret",
};

function memoryD1(options = {}) {
  const state = {
    ledger: null,
    users: new Map(),
    accounts: new Map(),
    sessions: options.sessions || 0,
  };
  if (options.extraUser) {
    state.users.set(options.extraUser.id, options.extraUser);
  }
  let failAfterUsers = options.failAfterUsers === true;

  const query = vi.fn(async (sql, params = []) => {
    const normalized = sql.replace(/\s+/g, " ").trim();
    if (normalized.startsWith("SELECT name FROM sqlite_schema")) {
      return ["user", "account", "session", "auth_identity_imports"].map(
        (name) => ({ name }),
      );
    }
    if (normalized.includes("FROM auth_identity_imports")) {
      return state.ledger ? [{ ...state.ledger }] : [];
    }
    if (normalized.startsWith("INSERT OR IGNORE INTO auth_identity_imports")) {
      if (!state.ledger) {
        state.ledger = {
          id: params[0],
          sourceSha256: params[1],
          configFingerprint: params[2],
          canonicalSha256: params[3],
          userCount: Number(params[4]),
          accountCount: Number(params[5]),
          status: "applying",
          startedAt: Number(params[6]),
          completedAt: null,
        };
      }
      return [];
    }
    if (normalized.startsWith("UPDATE auth_identity_imports")) {
      if (
        state.ledger?.status === "applying" &&
        state.ledger.id === params[1] &&
        state.ledger.sourceSha256 === params[2] &&
        state.ledger.canonicalSha256 === params[3] &&
        state.ledger.configFingerprint === params[4]
      ) {
        state.ledger.status = "completed";
        state.ledger.completedAt = Number(params[0]);
      }
      return [];
    }
    if (normalized.includes("FROM user ORDER BY")) {
      return [...state.users.values()].sort((left, right) =>
        left.id.localeCompare(right.id),
      );
    }
    if (normalized.includes("FROM account ORDER BY")) {
      return [...state.accounts.values()].sort((left, right) =>
        left.id.localeCompare(right.id),
      );
    }
    if (normalized === "SELECT COUNT(*) AS count FROM session") {
      return [{ count: state.sessions }];
    }
    throw new Error(`unexpected query: ${normalized}`);
  });

  const batch = vi.fn(async (statements) => {
    for (const { sql, params } of statements) {
      const normalized = sql.replace(/\s+/g, " ").trim();
      if (normalized.startsWith("INSERT OR IGNORE INTO user")) {
        const hasImageParameter = !normalized.includes("?, ?, ?, ?, NULL,");
        const row = {
          id: params[0],
          name: params[1],
          email: params[2],
          emailVerified: Number(params[3]),
          image: hasImageParameter ? params[4] : null,
          createdAt: params[hasImageParameter ? 5 : 4],
          updatedAt: params[hasImageParameter ? 6 : 5],
        };
        if (!state.users.has(row.id)) state.users.set(row.id, row);
      } else if (normalized.startsWith("INSERT OR IGNORE INTO account")) {
        const hasPasswordParameter = !normalized.includes(
          "?, ?, ?, ?, ?, NULL,",
        );
        const row = {
          id: params[0],
          issuer: params[1],
          accountId: params[2],
          providerId: params[3],
          userId: params[4],
          password: hasPasswordParameter ? params[5] : null,
          createdAt: params[hasPasswordParameter ? 6 : 5],
          updatedAt: params[hasPasswordParameter ? 7 : 6],
        };
        if (!state.accounts.has(row.id)) state.accounts.set(row.id, row);
      } else {
        throw new Error(`unexpected batch query: ${normalized}`);
      }
    }
    if (
      failAfterUsers &&
      statements.some(({ sql }) => /INTO user\s/.test(sql))
    ) {
      failAfterUsers = false;
      throw new Error("simulated transport interruption after committed users");
    }
    return statements.map(() => ({ success: true }));
  });

  return { client: { query, batch }, state };
}

describe("Firebase identity import", () => {
  it("plans stable password, Google, and Apple identities with Worker-compatible envelopes", () => {
    const importConfig = parseFirebaseImportScryptConfig(
      FIREBASE_SAMPLE.config,
    );
    const first = planFirebaseIdentityImport(source(), importConfig);
    const second = planFirebaseIdentityImport(source(), importConfig);
    const workerEnvelope = encodeFirebasePasswordHash(
      FIREBASE_SAMPLE,
      parseFirebaseScryptConfig(FIREBASE_SAMPLE.config),
    );

    expect(first.canonicalSha256).toBe(second.canonicalSha256);
    expect(first.users[0]).toMatchObject({
      id: "firebase-uid-1",
      email: "owner@example.com",
      updatedAt: "2023-11-14T22:15:00.000Z",
    });
    expect(first.accounts.map((account) => account.providerId).sort()).toEqual([
      "apple",
      "credential",
      "google",
    ]);
    expect(
      Object.fromEntries(
        first.accounts.map((account) => [account.providerId, account.issuer]),
      ),
    ).toEqual({
      apple: "https://appleid.apple.com",
      credential: "local:credential",
      google: "https://accounts.google.com",
    });
    expect(first.requiredSocialProviders).toEqual(["apple", "google"]);
    expect(
      first.accounts.find((account) => account.providerId === "credential")
        .password,
    ).toBe(workerEnvelope);
  });

  it("accepts the official lastSignedInAt field and rejects ambiguous or malformed export metadata", () => {
    const config = parseFirebaseImportScryptConfig(FIREBASE_SAMPLE.config);
    const base = source().users[0];

    expect(() =>
      planFirebaseIdentityImport(
        {
          users: [
            {
              ...base,
              lastLoginAt: "1700000200000",
            },
          ],
        },
        config,
      ),
    ).toThrow(/lastSignedInAt and lastLoginAt disagree/);
    expect(() =>
      planFirebaseIdentityImport(
        { users: [{ ...base, emailVerified: "true" }] },
        config,
      ),
    ).toThrow(/emailVerified must be a boolean/);
    expect(() =>
      planFirebaseIdentityImport(
        {
          users: [
            {
              ...base,
              providerUserInfo: [
                {
                  providerId: "google.com",
                  rawId: "google-sub-1",
                  unreviewedAuthority: "unexpected",
                },
              ],
            },
          ],
        },
        config,
      ),
    ).toThrow(/providerUserInfo\[0\] contains unsupported fields/);
  });

  it("rejects Firebase scrypt settings that cannot fit the Workers verifier budget", () => {
    expect(() =>
      parseFirebaseImportScryptConfig({
        ...FIREBASE_SAMPLE.config,
        mem_cost: 18,
      }),
    ).toThrow(/memory budget/);
  });

  it("applies, verifies, and replays one completed D1 import", async () => {
    const plan = planFirebaseIdentityImport(
      source(),
      parseFirebaseImportScryptConfig(FIREBASE_SAMPLE.config),
    );
    const database = memoryD1();
    const sourceSha256 = "a".repeat(64);

    await expect(
      runIdentityImport(
        "apply",
        plan,
        sourceSha256,
        database.client,
        providerEnv,
      ),
    ).resolves.toMatchObject({
      status: "applied",
      users: 1,
      accounts: 3,
      sessions: 0,
    });
    await expect(
      runIdentityImport(
        "verify",
        plan,
        sourceSha256,
        database.client,
        providerEnv,
      ),
    ).resolves.toMatchObject({ status: "verified" });
    await expect(
      runIdentityImport(
        "apply",
        plan,
        sourceSha256,
        database.client,
        providerEnv,
      ),
    ).resolves.toMatchObject({ status: "already_applied" });
    expect(database.state.ledger.status).toBe("completed");
    expect(database.state.users.size).toBe(1);
    expect(database.state.accounts.size).toBe(3);
  });

  it("resumes the same source after an interrupted committed batch", async () => {
    const plan = planFirebaseIdentityImport(
      source(),
      parseFirebaseImportScryptConfig(FIREBASE_SAMPLE.config),
    );
    const database = memoryD1({ failAfterUsers: true });
    const sourceSha256 = "b".repeat(64);

    await expect(
      runIdentityImport(
        "apply",
        plan,
        sourceSha256,
        database.client,
        providerEnv,
      ),
    ).rejects.toThrow(/transport interruption/);
    expect(database.state.ledger.status).toBe("applying");
    expect(database.state.users.size).toBe(1);
    expect(database.state.accounts.size).toBe(0);

    await expect(
      runIdentityImport(
        "apply",
        plan,
        sourceSha256,
        database.client,
        providerEnv,
      ),
    ).resolves.toMatchObject({ status: "applied" });
    expect(database.state.ledger.status).toBe("completed");
    expect(database.state.accounts.size).toBe(3);
  });

  it("fails closed for target conflicts and missing provider credentials", async () => {
    const plan = planFirebaseIdentityImport(
      source(),
      parseFirebaseImportScryptConfig(FIREBASE_SAMPLE.config),
    );
    const database = memoryD1({
      extraUser: {
        id: "unexpected-user",
        name: "Unexpected",
        email: "unexpected@example.test",
        emailVerified: 1,
        image: null,
        createdAt: "2023-01-01T00:00:00.000Z",
        updatedAt: "2023-01-01T00:00:00.000Z",
      },
    });

    await expect(
      runIdentityImport(
        "apply",
        plan,
        "c".repeat(64),
        database.client,
        providerEnv,
      ),
    ).rejects.toThrow(/conflicting user row/);
    await expect(
      runIdentityImport("verify", plan, "c".repeat(64), database.client, {
        GOOGLE_CLIENT_ID: "partial",
      }),
    ).rejects.toThrow(/GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET/);
  });

  it("uses parameterized Cloudflare D1 API requests and sanitizes failures", async () => {
    const fetchImpl = vi.fn(async (_url, request) => {
      const body = JSON.parse(request.body);
      expect(body).toEqual({
        sql: "SELECT value FROM probe WHERE id = ?",
        params: ["sensitive-value"],
      });
      expect(request.headers.authorization).toBe("Bearer api-token");
      return Response.json({
        success: true,
        result: [{ success: true, results: [{ value: "ok" }] }],
      });
    });
    const client = createCloudflareD1Client({
      accountId: "account-id",
      databaseId: "11111111-1111-1111-1111-111111111111",
      apiToken: "api-token",
      fetchImpl,
    });
    await expect(
      client.query("SELECT value FROM probe WHERE id = ?", ["sensitive-value"]),
    ).resolves.toEqual([{ value: "ok" }]);

    const failed = createCloudflareD1Client({
      accountId: "account-id",
      databaseId: "11111111-1111-1111-1111-111111111111",
      apiToken: "api-token",
      fetchImpl: async () =>
        Response.json(
          {
            success: false,
            errors: [{ message: "secret database detail" }],
          },
          { status: 500 },
        ),
    });
    await expect(failed.query("SELECT ?", ["sensitive-value"])).rejects.toEqual(
      new FirebaseIdentityMigrationError(
        "Cloudflare D1 API query failed (500)",
      ),
    );
  });

  it("rejects readable or symlinked Firebase export files", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "omi-d1-auth-import-"));
    const users = path.join(root, "users.json");
    const config = path.join(root, "hash-config.json");
    try {
      await writeFile(users, JSON.stringify(source()), { mode: 0o600 });
      await writeFile(config, JSON.stringify(FIREBASE_SAMPLE.config), {
        mode: 0o600,
      });
      await chmod(users, 0o644);
      const readable = spawnSync(
        process.execPath,
        [
          path.resolve("scripts/import-firebase-identities.mjs"),
          "validate",
          "--users",
          users,
          "--hash-config",
          config,
        ],
        { cwd: path.resolve("."), encoding: "utf8" },
      );
      expect(readable.status).toBe(2);
      expect(readable.stderr).toMatch(/mode 0600/);

      if (process.platform !== "win32") {
        await chmod(users, 0o600);
        const linked = path.join(root, "users-link.json");
        await symlink(users, linked);
        const symlinked = spawnSync(
          process.execPath,
          [
            path.resolve("scripts/import-firebase-identities.mjs"),
            "validate",
            "--users",
            linked,
            "--hash-config",
            config,
          ],
          { cwd: path.resolve("."), encoding: "utf8" },
        );
        expect(symlinked.status).toBe(2);
        expect(symlinked.stderr).toMatch(/regular file, not a symlink/);
      }
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
});
