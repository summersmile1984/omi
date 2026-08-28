import { describe, expect, it } from "vitest";
import {
  createD1MigrationConfig,
  resolveD1DatabaseId,
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
});
