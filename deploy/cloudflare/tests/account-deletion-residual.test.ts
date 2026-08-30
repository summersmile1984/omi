import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import {
  ACCOUNT_DELETION_CONTROL_D1_SURFACES,
  ACCOUNT_DELETION_CONVERSATION_RECORDING_PREFIX_PATTERNS,
  ACCOUNT_DELETION_D1_SURFACES,
  ACCOUNT_DELETION_D1_PURGE_SURFACES,
  ACCOUNT_DELETION_R2_PREFIX_PATTERNS,
  ACCOUNT_DELETION_SPEECH_PROFILE_PREFIX_PATTERNS,
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
  "uid_hint",
]);

function migrationIdentitySurfaces(): Set<string> {
  const directory = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "../migrations/app",
  );
  const database = new DatabaseSync(":memory:");
  database.exec("PRAGMA foreign_keys = ON");
  for (const filename of readdirSync(directory)
    .filter((value) => value.endsWith(".sql"))
    .sort()) {
    database.exec(readFileSync(path.join(directory, filename), "utf8"));
  }
  try {
    const tables = database
      .prepare(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
      )
      .all() as Array<{ name: string }>;
    const surfaces = new Set<string>();
    for (const { name } of tables) {
      const quoted = name.replaceAll('"', '""');
      const columns = database
        .prepare(`PRAGMA table_info("${quoted}")`)
        .all() as Array<{
        name: string;
      }>;
      for (const { name: column } of columns) {
        if (IDENTITY_COLUMNS.has(column)) surfaces.add(`${name}.${column}`);
      }
    }
    return surfaces;
  } finally {
    database.close();
  }
}

function residualEnvironment(
  options: {
    d1Count?: number;
    r2Prefix?: string;
    conversationR2Prefix?: string;
    speechProfileR2Prefix?: string;
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
  const conversationBucket = {
    list: vi.fn(async ({ prefix }: { prefix: string }) => ({
      objects:
        prefix === options.conversationR2Prefix
          ? [{ key: `${prefix}object`, version: "1", size: 1 }]
          : [],
      truncated: false,
      delimitedPrefixes: [],
    })),
  } as unknown as R2Bucket;
  const speechProfileBucket = {
    list: vi.fn(async ({ prefix }: { prefix: string }) => ({
      objects:
        prefix === options.speechProfileR2Prefix
          ? [{ key: `${prefix}speech_profile.wav`, version: "1", size: 1 }]
          : [],
      truncated: false,
      delimitedPrefixes: [],
    })),
  } as unknown as R2Bucket;
  return {
    env: {
      APP_DB: database,
      ASSETS: bucket,
      CONVERSATION_RECORDINGS: conversationBucket,
      SPEECH_PROFILES: speechProfileBucket,
    },
    statements,
    bucket,
    conversationBucket,
    speechProfileBucket,
  };
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
    const migrationDirectory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/app",
    );
    const migration = readdirSync(migrationDirectory)
      .filter((filename) => filename.endsWith(".sql"))
      .sort()
      .map((filename) =>
        readFileSync(path.join(migrationDirectory, filename), "utf8"),
      )
      .join("\n");
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
    expect(empty.conversationBucket.list).toHaveBeenCalledTimes(
      ACCOUNT_DELETION_CONVERSATION_RECORDING_PREFIX_PATTERNS.length,
    );
    expect(empty.speechProfileBucket.list).toHaveBeenCalledTimes(
      ACCOUNT_DELETION_SPEECH_PROFILE_PREFIX_PATTERNS.length,
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

    const isolatedPrefix = "account-user/";
    const isolated = residualEnvironment({
      conversationR2Prefix: isolatedPrefix,
    });
    const isolatedResult = await readAccountProductResidual(
      isolated.env,
      "account-user",
    );
    expect(isolatedResult.empty).toBe(false);
    expect(isolatedResult.r2[`conversation-recordings:${isolatedPrefix}`]).toBe(
      1,
    );
    expect(isolatedResult.r2[`speech-profiles:${isolatedPrefix}`]).toBe(0);
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
