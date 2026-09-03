import { describe, expect, it } from "vitest";
import {
  assertNoPendingD1Migrations,
  createD1MigrationConfig,
  resolveD1DatabaseId,
  resolveProductionD1Migrations,
  resolveStagingD1Migrations,
  runD1MigrationWithRetry,
} from "../scripts/d1-migrations.mjs";

describe("Cloudflare D1 migration release config", () => {
  it("resolves one exact staging database UUID", () => {
    const output = JSON.stringify([
      {
        name: "omi-cf-app-staging-copy",
        uuid: "11111111-1111-4111-8111-111111111111",
      },
      {
        name: "omi-cf-app-staging",
        uuid: "22222222-2222-4222-8222-222222222222",
      },
    ]);
    expect(resolveD1DatabaseId(output, "omi-cf-app-staging")).toBe(
      "22222222-2222-4222-8222-222222222222",
    );
  });

  it("fails closed on missing, duplicate, or malformed database identities", () => {
    expect(() => resolveD1DatabaseId("not-json", "missing")).toThrow(
      "invalid JSON",
    );
    expect(() => resolveD1DatabaseId("[]", "missing")).toThrow("found 0");
    expect(() =>
      resolveD1DatabaseId(
        JSON.stringify([
          { name: "duplicate", uuid: "11111111-1111-4111-8111-111111111111" },
          { name: "duplicate", uuid: "22222222-2222-4222-8222-222222222222" },
        ]),
        "duplicate",
      ),
    ).toThrow("found 2");
    expect(() =>
      resolveD1DatabaseId(
        JSON.stringify([{ name: "malformed", uuid: "not-a-uuid" }]),
        "malformed",
      ),
    ).toThrow("invalid UUID");
  });

  it("binds the resolved UUID in a temporary Wrangler config", () => {
    expect(
      createD1MigrationConfig({
        databaseName: "omi-cf-app-staging",
        databaseId: "22222222-2222-4222-8222-222222222222",
        binding: "APP_DB",
        migrationsDir: "/workspace/migrations/app",
      }),
    ).toMatchObject({
      d1_databases: [
        {
          binding: "APP_DB",
          database_name: "omi-cf-app-staging",
          database_id: "22222222-2222-4222-8222-222222222222",
          migrations_dir: "/workspace/migrations/app",
        },
      ],
    });
  });

  it("builds both staging migration targets from one exact database list", () => {
    const output = JSON.stringify([
      {
        name: "omi-cf-auth-staging",
        uuid: "11111111-1111-4111-8111-111111111111",
      },
      {
        name: "omi-cf-app-staging",
        uuid: "22222222-2222-4222-8222-222222222222",
      },
    ]);
    expect(
      resolveStagingD1Migrations(output, (path) => `/repo/${path}`),
    ).toEqual([
      {
        databaseName: "omi-cf-auth-staging",
        databaseId: "11111111-1111-4111-8111-111111111111",
        binding: "AUTH_DB",
        migrationsDir: "/repo/migrations/auth",
      },
      {
        databaseName: "omi-cf-app-staging",
        databaseId: "22222222-2222-4222-8222-222222222222",
        binding: "APP_DB",
        migrationsDir: "/repo/migrations/app",
      },
    ]);
  });

  it("builds isolated production migration targets without reusing staging", () => {
    const output = JSON.stringify([
      {
        name: "omi-cf-auth-production",
        uuid: "33333333-3333-4333-8333-333333333333",
      },
      {
        name: "omi-cf-app-production",
        uuid: "44444444-4444-4444-8444-444444444444",
      },
    ]);
    expect(
      resolveProductionD1Migrations(output, (path) => `/repo/${path}`),
    ).toEqual([
      {
        databaseName: "omi-cf-auth-production",
        databaseId: "33333333-3333-4333-8333-333333333333",
        binding: "AUTH_DB",
        migrationsDir: "/repo/migrations/auth",
      },
      {
        databaseName: "omi-cf-app-production",
        databaseId: "44444444-4444-4444-8444-444444444444",
        binding: "APP_DB",
        migrationsDir: "/repo/migrations/app",
      },
    ]);
  });

  it("fails verification whenever Wrangler reports pending migrations", () => {
    expect(() =>
      assertNoPendingD1Migrations(
        "Migrations to be applied:\n0148_example.sql",
        "omi-cf-app-staging",
      ),
    ).toThrow("has unapplied migrations");
    expect(() =>
      assertNoPendingD1Migrations(
        "\u001b[32m✅ No migrations to apply!\u001b[0m",
        "omi-cf-app-staging",
      ),
    ).not.toThrow();
  });

  it("retries transient Cloudflare migration failures before succeeding", () => {
    const attempts = [
      { ok: false, stderr: "D1 DB is overloaded. [code: 7429]" },
      { ok: false, stderr: "internal error [code: 7500]" },
      { ok: true, stdout: "No migrations to apply!" },
    ];
    const delays = [];
    const retries = [];

    const result = runD1MigrationWithRetry(() => attempts.shift(), {
      sleep: (delayMs) => delays.push(delayMs),
      onRetry: (retry) => retries.push(retry),
    });

    expect(result).toMatchObject({ ok: true });
    expect(attempts).toHaveLength(0);
    expect(delays).toEqual([2_000, 4_000]);
    expect(retries.map(({ attempt }) => attempt)).toEqual([1, 2]);
  });

  it("does not retry a non-transient migration failure", () => {
    let attempts = 0;
    const result = runD1MigrationWithRetry(
      () => {
        attempts += 1;
        return { ok: false, stderr: "SQLITE_ERROR: no such table" };
      },
      { sleep: () => expect.unreachable("unexpected retry") },
    );

    expect(result).toMatchObject({ ok: false });
    expect(attempts).toBe(1);
  });
});
