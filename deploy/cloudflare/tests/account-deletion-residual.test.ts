import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import {
  ACCOUNT_DELETION_CONTROL_D1_SURFACES,
  ACCOUNT_DELETION_D1_SURFACES,
  ACCOUNT_DELETION_D1_PURGE_SURFACES,
  ACCOUNT_DELETION_R2_PREFIX_PATTERNS,
  readAccountProductResidual,
} from "../workers/jobs/account-deletion-residual";
import jobs from "../workers/jobs/index";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../workers/shared/auth-context";

const IDENTITY_COLUMNS = new Set([
  "uid",
  "owner_uid",
  "recipient_uid",
  "reviewer_uid",
  "sender_uid",
]);

function migrationIdentitySurfaces(): Set<string> {
  const directory = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "../migrations/app",
  );
  const columns = new Map<string, Set<string>>();
  const addColumn = (table: string, column: string) => {
    if (!columns.has(table)) columns.set(table, new Set());
    columns.get(table)?.add(column);
  };
  for (const filename of readdirSync(directory)
    .filter((value) => value.endsWith(".sql"))
    .sort()) {
    const sql = readFileSync(path.join(directory, filename), "utf8");
    const createPattern =
      /CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)[\s\S]*?\);/gi;
    for (const match of sql.matchAll(createPattern)) {
      const table = match[1];
      for (const line of match[0].split("\n").slice(1)) {
        const column = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s+/.exec(line)?.[1];
        if (column && IDENTITY_COLUMNS.has(column)) addColumn(table, column);
      }
    }
    const alterPattern =
      /ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)/gi;
    for (const match of sql.matchAll(alterPattern)) {
      if (IDENTITY_COLUMNS.has(match[2])) addColumn(match[1], match[2]);
    }
  }
  return new Set(
    [...columns.entries()].flatMap(([table, tableColumns]) =>
      [...tableColumns].map((column) => `${table}.${column}`),
    ),
  );
}

function residualEnvironment(
  options: {
    d1Count?: number;
    r2Prefix?: string;
    malformed?: boolean;
  } = {},
) {
  const statements: Array<{ sql: string; uid?: string }> = [];
  const database = {
    prepare: vi.fn((sql: string) => {
      const statement: { sql: string; uid?: string } = { sql };
      statements.push(statement);
      return {
        bind: vi.fn((uid: string) => {
          statement.uid = uid;
          return statement;
        }),
      };
    }),
    batch: vi.fn(async (batch: Array<{ sql: string }>) =>
      options.malformed
        ? []
        : batch.map((statement, index) => ({
            success: true,
            results: [
              {
                count: index === 0 && options.d1Count ? options.d1Count : 0,
              },
            ],
            meta: {},
          })),
    ),
  } as unknown as D1Database;
  const bucket = {
    list: vi.fn(async ({ prefix }: { prefix: string }) => ({
      objects:
        prefix === options.r2Prefix
          ? [{ key: `${prefix}object`, version: "1", size: 1 }]
          : [],
      truncated: false,
      delimitedPrefixes: [],
    })),
  } as unknown as R2Bucket;
  return { env: { APP_DB: database, ASSETS: bucket }, statements, bucket };
}

async function residualHeaders(uid: string, path: string) {
  const signed = await createSignedAuthContext(
    {
      uid,
      authority: "better-auth",
      requestId: "account-deletion-residual-request",
    },
    "jobs",
    "GET",
    path,
    "account-deletion-residual-secret",
  );
  if (!signed) throw new Error("test residual assertion unavailable");
  return {
    [AUTH_CONTEXT_HEADER]: signed.encoded,
    [AUTH_SIGNATURE_HEADER]: signed.signature,
  };
}

describe("Cloudflare account-deletion residual", () => {
  it("covers every identity-bearing App-D1 migration column explicitly", () => {
    const planned = new Set(
      [
        ...ACCOUNT_DELETION_D1_SURFACES,
        ...ACCOUNT_DELETION_CONTROL_D1_SURFACES,
      ].map(({ table, column }) => `${table}.${column}`),
    );
    expect(planned).toEqual(migrationIdentitySurfaces());
    expect(
      new Set(
        ACCOUNT_DELETION_D1_PURGE_SURFACES.map(
          ({ table, column }) => `${table}.${column}`,
        ),
      ),
    ).toEqual(
      new Set(
        ACCOUNT_DELETION_D1_SURFACES.map(
          ({ table, column }) => `${table}.${column}`,
        ),
      ),
    );
  });

  it("installs INSERT and UPDATE mutation fences for every authoritative identity table", () => {
    const migration = readFileSync(
      path.resolve(
        path.dirname(fileURLToPath(import.meta.url)),
        "../migrations/app/0052_account_deletion.sql",
      ),
      "utf8",
    );
    const triggerBlocks = new Map(
      [
        ...migration.matchAll(
          /CREATE TRIGGER IF NOT EXISTS (adf_[A-Za-z0-9_]+)([\s\S]*?)END;/g,
        ),
      ].map((match) => [match[1], match[2]]),
    );
    for (const { table, column } of ACCOUNT_DELETION_D1_SURFACES) {
      if (table === "cf_conversations_fts") continue;
      const suffix = table.replace(/^cf_/, "");
      const insert = triggerBlocks.get(`adf_i_${suffix}`) || "";
      const update = triggerBlocks.get(`adf_u_${suffix}`) || "";
      expect(insert).toContain(`BEFORE INSERT ON ${table}`);
      expect(insert).toContain("cf_account_deletion_intents");
      expect(insert).toContain("cf_account_deletion_tombstones");
      expect(update).toContain(`BEFORE UPDATE ON ${table}`);
      expect(update).toContain(`OLD.${column}`);
      expect(update).toContain(`NEW.${column}`);
      expect(update).toContain("cf_account_deletion_intents");
      expect(update).toContain("cf_account_deletion_tombstones");
    }
    for (const name of [
      "adf_i_task_share_items",
      "adf_u_task_share_items",
      "adf_i_chat_share_messages",
      "adf_u_chat_share_messages",
    ]) {
      expect(triggerBlocks.get(name)).toContain("cf_account_deletion_intents");
      expect(triggerBlocks.get(name)).toContain(
        "cf_account_deletion_tombstones",
      );
    }
  });

  it("reports an empty account only after every D1 and R2 surface is empty", async () => {
    const empty = residualEnvironment();
    await expect(
      readAccountProductResidual(empty.env, "account-user"),
    ).resolves.toMatchObject({
      uid: "account-user",
      empty: true,
    });
    expect(empty.statements).toHaveLength(ACCOUNT_DELETION_D1_SURFACES.length);
    expect(empty.statements.every(({ uid }) => uid === "account-user")).toBe(
      true,
    );
    expect(empty.bucket.list).toHaveBeenCalledTimes(
      ACCOUNT_DELETION_R2_PREFIX_PATTERNS.length,
    );

    const populatedPrefix = "cf-sync/account-user/";
    const populated = residualEnvironment({
      d1Count: 2,
      r2Prefix: populatedPrefix,
    });
    const result = await readAccountProductResidual(
      populated.env,
      "account-user",
    );
    expect(result.empty).toBe(false);
    expect(result.d1["cf_account_cutover.uid"]).toBe(2);
    expect(result.r2[populatedPrefix]).toBe(1);
  });

  it("fails closed for invalid identities and incomplete D1 batches", async () => {
    const malformed = residualEnvironment({ malformed: true });
    await expect(
      readAccountProductResidual(malformed.env, "uid/escape"),
    ).rejects.toThrow(/invalid account deletion uid/);
    await expect(
      readAccountProductResidual(malformed.env, "valid-uid"),
    ).rejects.toThrow(/batch is incomplete/);
  });

  it("exposes residuals only through a uid-bound signed Jobs request", async () => {
    const state = residualEnvironment();
    const env = {
      ...state.env,
      INTERNAL_ASSERTION_SECRET: "account-deletion-residual-secret",
    } as never;
    const path = "/internal/users/account-user/residual";

    const unsigned = await jobs.fetch(
      new Request(`https://jobs.test${path}`),
      env,
    );
    expect(unsigned.status).toBe(401);

    const crossUser = await jobs.fetch(
      new Request(`https://jobs.test${path}`, {
        headers: await residualHeaders("other-user", path),
      }),
      env,
    );
    expect(crossUser.status).toBe(403);

    const response = await jobs.fetch(
      new Request(`https://jobs.test${path}`, {
        headers: await residualHeaders("account-user", path),
      }),
      env,
    );
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({
      uid: "account-user",
      empty: true,
    });
  });

  it("returns 503 instead of a partial residual when a dependency fails", async () => {
    const state = residualEnvironment({ malformed: true });
    const path = "/internal/users/account-user/residual";
    const response = await jobs.fetch(
      new Request(`https://jobs.test${path}`, {
        headers: await residualHeaders("account-user", path),
      }),
      {
        ...state.env,
        INTERNAL_ASSERTION_SECRET: "account-deletion-residual-secret",
      } as never,
    );
    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toEqual({
      error: "product_residual_unavailable",
    });
  });
});
