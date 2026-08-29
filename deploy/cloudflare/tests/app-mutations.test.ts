import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobsEnv } from "../workers/jobs/env";
import jobs from "../workers/jobs/index";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../workers/shared/auth-context";

type BoundStatement = {
  sql: string;
  args: unknown[];
  execute(): TestD1Result<unknown>;
};
type TestD1Result<T> = {
  success: true;
  results: T[];
  meta: { changes: number };
};
type TestD1PreparedStatement = BoundStatement & {
  bind(...values: unknown[]): TestD1PreparedStatement;
  first<T>(): Promise<T | null>;
  all<T>(): Promise<TestD1Result<T>>;
  run(): Promise<TestD1Result<unknown>>;
};

function sqliteValue(value: unknown) {
  return typeof value === "boolean" ? Number(value) : (value as never);
}

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    this.database.exec("PRAGMA foreign_keys = ON");
    const directory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/app",
    );
    for (const filename of readdirSync(directory)
      .filter((value) => value.endsWith(".sql"))
      .sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string): TestD1PreparedStatement {
    const build = (args: unknown[] = []): TestD1PreparedStatement => ({
      sql,
      args,
      bind: (...values: unknown[]) => build(values),
      first: async <T>() => {
        const row = this.database.prepare(sql).get(...args.map(sqliteValue)) as
          T | undefined;
        return row ?? null;
      },
      all: async <T>() => ({
        success: true,
        results: this.database
          .prepare(sql)
          .all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0 },
      }),
      run: async () => build(args).execute(),
      execute: () => {
        const statement = this.database.prepare(sql);
        if (/^SELECT\b/i.test(sql.trimStart())) {
          return {
            success: true,
            results: statement.all(...args.map(sqliteValue)),
            meta: { changes: 0 },
          };
        }
        const result = statement.run(...args.map(sqliteValue));
        return {
          success: true,
          results: [],
          meta: { changes: Number(result.changes) },
        };
      },
    });
    return build();
  }

  async batch(statements: BoundStatement[]) {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const results = statements.map((statement) => statement.execute());
      this.database.exec("COMMIT");
      return results;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  row<T>(sql: string, ...args: unknown[]): T | null {
    return (
      (this.database.prepare(sql).get(...args.map(sqliteValue)) as
        T | undefined) ?? null
    );
  }

  close() {
    this.database.close();
  }
}

class MemoryR2 {
  failDeletes = false;
  readonly objects = new Map<
    string,
    { bytes: Uint8Array; contentType: string; metadata: Record<string, string> }
  >();

  async put(
    key: string,
    value: ArrayBuffer | ArrayBufferView | Blob | string,
    options?: R2PutOptions,
  ) {
    const bytes =
      value instanceof Blob
        ? new Uint8Array(await value.arrayBuffer())
        : typeof value === "string"
          ? new TextEncoder().encode(value)
          : ArrayBuffer.isView(value)
            ? new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
            : new Uint8Array(value);
    this.objects.set(key, {
      bytes: Uint8Array.from(bytes),
      contentType:
        options?.httpMetadata instanceof Headers
          ? options.httpMetadata.get("content-type") ||
            "application/octet-stream"
          : options?.httpMetadata?.contentType || "application/octet-stream",
      metadata: options?.customMetadata || {},
    });
    return {} as R2Object;
  }

  async get(key: string) {
    const object = this.objects.get(key);
    if (!object) return null;
    return {
      body: new Blob([Uint8Array.from(object.bytes).buffer]).stream(),
      httpEtag: '"test-etag"',
      writeHttpMetadata(headers: Headers) {
        headers.set("content-type", object.contentType);
      },
    } as R2ObjectBody;
  }

  async delete(key: string | string[]) {
    if (this.failDeletes) throw new Error("R2 unavailable");
    for (const value of Array.isArray(key) ? key : [key])
      this.objects.delete(value);
  }
}

function environment(options: { stripeSecret?: string } = {}) {
  const database = new SqliteD1();
  const assets = new MemoryR2();
  const env = {
    AUTH: {
      fetch: vi.fn(async (request: Request) => {
        if (new URL(request.url).pathname === "/internal/profile") {
          return Response.json({
            uid: "creator-user",
            name: "Cloudflare Creator",
            email: "creator@example.com",
          });
        }
        return new Response(null, { status: 404 });
      }),
    } as unknown as Fetcher,
    APP_DB: database as unknown as D1Database,
    ASSETS: assets as unknown as R2Bucket,
    AI: { run: vi.fn() },
    JOBS: { send: vi.fn() } as unknown as Queue,
    SYNC_FRESH: { send: vi.fn() } as unknown as Queue,
    SYNC_BACKFILL: { send: vi.fn() } as unknown as Queue,
    INTERNAL_ASSERTION_SECRET: "app-mutation-assertion-secret",
    PUBLIC_API_BASE_URL: "https://edge.test",
    STRIPE_SECRET_KEY: options.stripeSecret,
  } satisfies JobsEnv;
  return { database, assets, env };
}

async function authHeaders(
  method: "POST" | "PATCH",
  path: string,
  uid = "creator-user",
) {
  const signed = await createSignedAuthContext(
    {
      uid,
      authority: "better-auth",
      displayName: "Cloudflare Creator",
      requestId: `mutation-${method.toLowerCase()}`,
    },
    "jobs",
    method,
    path,
    "app-mutation-assertion-secret",
  );
  if (!signed) throw new Error("missing signed context");
  return {
    [AUTH_CONTEXT_HEADER]: signed.encoded,
    [AUTH_SIGNATURE_HEADER]: signed.signature,
  };
}

const png = Uint8Array.from([
  137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82,
]);

function multipart(data: Record<string, unknown>, includeLogo = true) {
  const form = new FormData();
  form.set("app_data", JSON.stringify(data));
  if (includeLogo) {
    form.set("file", new File([png], "logo.png", { type: "image/png" }));
  }
  return form;
}

function freeApp(overrides: Record<string, unknown> = {}) {
  return {
    name: "Worker Notes",
    description: "Cloudflare-owned app",
    category: "productivity-and-organization",
    capabilities: ["chat"],
    chat_prompt: "Answer from the user's context.",
    private: true,
    is_paid: false,
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Cloudflare app mutations", () => {
  it("creates a pending owner app with a versioned R2 logo and serves that exact logo", async () => {
    const state = environment();
    try {
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps", {
          method: "POST",
          headers: await authHeaders("POST", "/v1/apps"),
          body: multipart(freeApp()),
        }),
        state.env,
      );
      expect(response.status).toBe(200);
      const body = (await response.json()) as { app_id: string };
      expect(body.app_id).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/);
      const row = state.database.row<{
        approved: number;
        status: string;
        owner_uid: string;
        data_json: string;
      }>(
        "SELECT approved, status, owner_uid, data_json FROM cf_app_catalog WHERE id = ?",
        body.app_id,
      );
      expect(row).toMatchObject({
        approved: 0,
        status: "under-review",
        owner_uid: "creator-user",
      });
      const payload = JSON.parse(row!.data_json) as Record<string, unknown>;
      expect(payload).toMatchObject({
        id: body.app_id,
        uid: "creator-user",
        author: "Cloudflare Creator",
        email: "creator@example.com",
        private: true,
        is_paid: false,
      });
      expect(payload.image).toMatch(
        new RegExp(
          `^https://edge\\.test/v1/apps/${body.app_id}/logo/[0-9a-f-]{36}$`,
        ),
      );
      expect(state.assets.objects.size).toBe(1);

      const logo = await jobs.fetch(
        new Request(String(payload.image)),
        state.env,
      );
      expect(logo.status).toBe(200);
      expect(logo.headers.get("content-type")).toBe("image/png");
      expect(new Uint8Array(await logo.arrayBuffer())).toEqual(png);
    } finally {
      state.database.close();
    }
  });

  it("updates only the owner record and preserves the logo when no file is sent", async () => {
    const state = environment();
    try {
      const created = await jobs.fetch(
        new Request("https://jobs.test/v1/apps", {
          method: "POST",
          headers: await authHeaders("POST", "/v1/apps"),
          body: multipart(freeApp()),
        }),
        state.env,
      );
      const appId = ((await created.json()) as { app_id: string }).app_id;
      const original = JSON.parse(
        state.database.row<{ data_json: string }>(
          "SELECT data_json FROM cf_app_catalog WHERE id = ?",
          appId,
        )!.data_json,
      ) as Record<string, unknown>;
      const forbidden = await jobs.fetch(
        new Request(`https://jobs.test/v1/apps/${appId}`, {
          method: "PATCH",
          headers: await authHeaders(
            "PATCH",
            `/v1/apps/${appId}`,
            "other-user",
          ),
          body: multipart({ id: appId, name: "Stolen" }, false),
        }),
        state.env,
      );
      expect(forbidden.status).toBe(403);
      const updated = await jobs.fetch(
        new Request(`https://jobs.test/v1/apps/${appId}`, {
          method: "PATCH",
          headers: await authHeaders("PATCH", `/v1/apps/${appId}`),
          body: multipart(
            { id: appId, name: "Worker Notes 2", private: false },
            false,
          ),
        }),
        state.env,
      );
      expect(updated.status).toBe(200);
      const payload = JSON.parse(
        state.database.row<{ data_json: string }>(
          "SELECT data_json FROM cf_app_catalog WHERE id = ?",
          appId,
        )!.data_json,
      ) as Record<string, unknown>;
      expect(payload.name).toBe("Worker Notes 2");
      expect(payload.private).toBe(false);
      expect(payload.image).toBe(original.image);
      expect(state.assets.objects.size).toBe(1);
    } finally {
      state.database.close();
    }
  });

  it("cleans a replacement logo when the catalog authority disappears before the update batch", async () => {
    const state = environment();
    try {
      const created = await jobs.fetch(
        new Request("https://jobs.test/v1/apps", {
          method: "POST",
          headers: await authHeaders("POST", "/v1/apps"),
          body: multipart(freeApp()),
        }),
        state.env,
      );
      const appId = ((await created.json()) as { app_id: string }).app_id;
      state.assets.failDeletes = true;
      vi.spyOn(state.database, "batch").mockResolvedValueOnce([
        { success: true, results: [], meta: { changes: 0 } },
      ]);
      const response = await jobs.fetch(
        new Request(`https://jobs.test/v1/apps/${appId}`, {
          method: "PATCH",
          headers: await authHeaders("PATCH", `/v1/apps/${appId}`),
          body: multipart({ id: appId, name: "Lost update" }),
        }),
        state.env,
      );
      expect(response.status).toBe(503);
      expect(state.assets.objects.size).toBe(2);
      expect(
        state.database.row<{ reason: string; uid: string }>(
          "SELECT reason, uid FROM cf_asset_cleanup_tasks LIMIT 1",
        ),
      ).toEqual({ reason: "uncommitted-upload", uid: "creator-user" });
      const payload = JSON.parse(
        state.database.row<{ data_json: string }>(
          "SELECT data_json FROM cf_app_catalog WHERE id = ?",
          appId,
        )!.data_json,
      ) as Record<string, unknown>;
      expect(payload.name).toBe("Worker Notes");
    } finally {
      state.database.close();
    }
  });

  it("rejects invalid image bytes without leaving D1 or R2 state", async () => {
    const state = environment();
    try {
      const form = new FormData();
      form.set("app_data", JSON.stringify(freeApp()));
      form.set(
        "file",
        new File([new TextEncoder().encode("<svg></svg>")], "logo.svg", {
          type: "image/svg+xml",
        }),
      );
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps", {
          method: "POST",
          headers: await authHeaders("POST", "/v1/apps"),
          body: form,
        }),
        state.env,
      );
      expect(response.status).toBe(422);
      expect(state.assets.objects.size).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_app_catalog",
        )?.count,
      ).toBe(0);
    } finally {
      state.database.close();
    }
  });

  it("hydrates bounded chat tools from an HTTPS manifest during create", async () => {
    const state = environment();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: RequestInfo | URL) => {
        expect(String(request)).toBe("https://tools.example.test/omi.json");
        return Response.json({
          tools: [
            {
              name: "Create note",
              description: "Create a note in the external service",
              endpoint: "/tools/create-note",
              method: "POST",
              parameters: {
                properties: { title: { type: "string" } },
                required: ["title"],
              },
            },
          ],
          chat_messages: { enabled: true, target: "main", notify: true },
        });
      }),
    );
    try {
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps", {
          method: "POST",
          headers: await authHeaders("POST", "/v1/apps"),
          body: multipart(
            freeApp({
              capabilities: ["external_integration"],
              external_integration: {
                actions: [{ action: "create_conversation" }],
                app_home_url: "https://tools.example.test",
                chat_tools_manifest_url: "https://tools.example.test/omi.json",
              },
            }),
          ),
        }),
        state.env,
      );
      expect(response.status).toBe(200);
      const appId = ((await response.json()) as { app_id: string }).app_id;
      const payload = JSON.parse(
        state.database.row<{ data_json: string }>(
          "SELECT data_json FROM cf_app_catalog WHERE id = ?",
          appId,
        )!.data_json,
      ) as Record<string, unknown>;
      expect(payload.chat_tools).toEqual([
        {
          name: "Create note",
          description: "Create a note in the external service",
          endpoint: "https://tools.example.test/tools/create-note",
          method: "POST",
          auth_required: true,
          parameters: {
            properties: { title: { type: "string" } },
            required: ["title"],
          },
        },
      ]);
      expect(payload.external_integration).toMatchObject({
        chat_messages_enabled: true,
        chat_messages_target: "main",
        chat_messages_notify: true,
      });
    } finally {
      state.database.close();
    }
  });

  it("rejects a hydrated catalog document over the byte limit before provisioning payment resources", async () => {
    const state = environment({ stripeSecret: "sk_test_app_mutations" });
    state.database.database
      .prepare(
        `INSERT INTO cf_creator_payment_profiles
           (uid, stripe_account_id, stripe_charges_enabled,
            stripe_payouts_enabled, stripe_details_submitted,
            stripe_onboarding_complete, updated_at)
         VALUES ('creator-user', 'acct_paidCreator123', 1, 1, 1, 1, 1)`,
      )
      .run();
    const fetched: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: RequestInfo | URL) => {
        const url = String(request);
        fetched.push(url);
        if (url !== "https://tools.example.test/large.json") {
          throw new Error("Stripe must not be called for an oversized app");
        }
        return Response.json({
          tools: [
            {
              name: "Large schema",
              description: "Bounded remote schema",
              endpoint: "/tools/large",
              parameters: {
                properties: { padding: { description: "x".repeat(245_000) } },
              },
            },
          ],
        });
      }),
    );
    const longList = Array.from(
      { length: 100 },
      (_, index) => `${String(index).padStart(3, "0")}-${"x".repeat(250)}`,
    );
    try {
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps", {
          method: "POST",
          headers: await authHeaders("POST", "/v1/apps"),
          body: multipart(
            freeApp({
              description: "d".repeat(20_000),
              memory_prompt: "m".repeat(100_000),
              chat_prompt: "c".repeat(100_000),
              connected_accounts: longList,
              thumbnails: longList,
              proactive_notification_scopes: longList,
              capabilities: ["external_integration"],
              external_integration: {
                actions: [{ action: "create_conversation" }],
                app_home_url: "https://tools.example.test",
                chat_tools_manifest_url:
                  "https://tools.example.test/large.json",
              },
              is_paid: true,
              price: 9,
              payment_plan: "monthly_recurring",
            }),
          ),
        }),
        state.env,
      );
      expect(response.status).toBe(413);
      expect(fetched).toEqual(["https://tools.example.test/large.json"]);
      expect(state.assets.objects.size).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_app_catalog",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_app_payment_links",
        )?.count,
      ).toBe(0);
    } finally {
      state.database.close();
    }
  });

  it("fails a paid create before catalog commit when Stripe is unavailable and removes the staged logo", async () => {
    const state = environment();
    try {
      state.database.database
        .prepare(
          `INSERT INTO cf_creator_payment_profiles
             (uid, stripe_account_id, stripe_charges_enabled,
              stripe_payouts_enabled, stripe_details_submitted,
              stripe_onboarding_complete, updated_at)
           VALUES ('creator-user', 'acct_paidCreator123', 1, 1, 1, 1, 1)`,
        )
        .run();
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps", {
          method: "POST",
          headers: await authHeaders("POST", "/v1/apps"),
          body: multipart(
            freeApp({
              is_paid: true,
              price: 9,
              payment_plan: "monthly_recurring",
            }),
          ),
        }),
        state.env,
      );
      expect(response.status).toBe(503);
      expect(state.assets.objects.size).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_app_catalog",
        )?.count,
      ).toBe(0);
    } finally {
      state.database.close();
    }
  });

  it("atomically persists the Stripe Product, Price, and Payment Link mapping for a paid app", async () => {
    const state = environment({ stripeSecret: "sk_test_app_mutations" });
    state.database.database
      .prepare(
        `INSERT INTO cf_creator_payment_profiles
           (uid, stripe_account_id, stripe_charges_enabled,
            stripe_payouts_enabled, stripe_details_submitted,
            stripe_onboarding_complete, updated_at)
         VALUES ('creator-user', 'acct_paidCreator123', 1, 1, 1, 1, 1)`,
      )
      .run();
    const stripeRequests: Request[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
        const stripeRequest = new Request(request, init);
        stripeRequests.push(stripeRequest);
        const path = new URL(stripeRequest.url).pathname;
        const form = new URLSearchParams(await stripeRequest.text());
        const appId = form.get("metadata[app_id]") || "";
        if (path === "/v1/products") {
          return Response.json({
            id: "prod_paidApp123",
            metadata: { app_id: appId, owner_uid: "creator-user" },
          });
        }
        if (path === "/v1/prices") {
          return Response.json({
            id: "price_paidApp123",
            product: "prod_paidApp123",
            unit_amount: 900,
            currency: "usd",
            recurring: { interval: "month" },
            metadata: { app_id: appId, owner_uid: "creator-user" },
          });
        }
        if (path === "/v1/payment_links") {
          return Response.json({
            id: "plink_paidApp123",
            active: true,
            url: "https://buy.stripe.com/test-paid-app",
            transfer_data: { destination: "acct_paidCreator123" },
            metadata: { app_id: appId, owner_uid: "creator-user" },
          });
        }
        return new Response(null, { status: 404 });
      }),
    );
    try {
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps", {
          method: "POST",
          headers: await authHeaders("POST", "/v1/apps"),
          body: multipart(
            freeApp({
              is_paid: true,
              price: 9,
              payment_plan: "monthly_recurring",
            }),
          ),
        }),
        state.env,
      );
      expect(response.status).toBe(200);
      const appId = ((await response.json()) as { app_id: string }).app_id;
      const mapping = state.database.row<{
        owner_uid: string;
        stripe_product_id: string;
        stripe_price_id: string;
        stripe_payment_link_id: string;
        unit_amount: number;
        active: number;
      }>(
        `SELECT owner_uid, stripe_product_id, stripe_price_id,
                stripe_payment_link_id, unit_amount, active
         FROM cf_app_payment_links WHERE app_id = ?`,
        appId,
      );
      expect(mapping).toEqual({
        owner_uid: "creator-user",
        stripe_product_id: "prod_paidApp123",
        stripe_price_id: "price_paidApp123",
        stripe_payment_link_id: "plink_paidApp123",
        unit_amount: 900,
        active: 1,
      });
      const payload = JSON.parse(
        state.database.row<{ data_json: string }>(
          "SELECT data_json FROM cf_app_catalog WHERE id = ?",
          appId,
        )!.data_json,
      ) as Record<string, unknown>;
      expect(payload).toMatchObject({
        is_paid: true,
        price: 9,
        payment_link: "https://buy.stripe.com/test-paid-app",
      });
      expect(payload).not.toHaveProperty("payment_product_id");
      expect(payload).not.toHaveProperty("payment_price_id");
      expect(payload).not.toHaveProperty("payment_link_id");
      expect(
        stripeRequests.map((request) => new URL(request.url).pathname),
      ).toEqual(["/v1/products", "/v1/prices", "/v1/payment_links"]);
    } finally {
      state.database.close();
    }
  });

  it("deactivates an unpublished Payment Link and removes the logo when the final D1 batch fails", async () => {
    const state = environment({ stripeSecret: "sk_test_app_mutations" });
    state.database.database
      .prepare(
        `INSERT INTO cf_creator_payment_profiles
           (uid, stripe_account_id, stripe_charges_enabled,
            stripe_payouts_enabled, stripe_details_submitted,
            stripe_onboarding_complete, updated_at)
         VALUES ('creator-user', 'acct_paidCreator123', 1, 1, 1, 1, 1)`,
      )
      .run();
    vi.spyOn(state.database, "batch").mockRejectedValueOnce(
      new Error("D1 unavailable"),
    );
    let appId = "";
    const stripeCalls: Array<{ path: string; form: URLSearchParams }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
        const stripeRequest = new Request(request, init);
        const path = new URL(stripeRequest.url).pathname;
        const form = new URLSearchParams(await stripeRequest.text());
        stripeCalls.push({ path, form });
        appId = form.get("metadata[app_id]") || appId;
        if (path === "/v1/products") {
          return Response.json({
            id: "prod_paidApp123",
            metadata: { app_id: appId, owner_uid: "creator-user" },
          });
        }
        if (path === "/v1/prices") {
          return Response.json({
            id: "price_paidApp123",
            product: "prod_paidApp123",
            unit_amount: 900,
            currency: "usd",
            recurring: { interval: "month" },
            metadata: { app_id: appId, owner_uid: "creator-user" },
          });
        }
        if (path === "/v1/payment_links") {
          return Response.json({
            id: "plink_paidApp123",
            active: true,
            url: "https://buy.stripe.com/test-paid-app",
            transfer_data: { destination: "acct_paidCreator123" },
            metadata: { app_id: appId, owner_uid: "creator-user" },
          });
        }
        if (path === "/v1/payment_links/plink_paidApp123") {
          return Response.json({
            id: "plink_paidApp123",
            active: false,
            transfer_data: { destination: "acct_paidCreator123" },
            metadata: { app_id: appId, owner_uid: "creator-user" },
          });
        }
        return new Response(null, { status: 404 });
      }),
    );
    try {
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/apps", {
          method: "POST",
          headers: await authHeaders("POST", "/v1/apps"),
          body: multipart(
            freeApp({
              is_paid: true,
              price: 9,
              payment_plan: "monthly_recurring",
            }),
          ),
        }),
        state.env,
      );
      expect(response.status).toBe(503);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_app_catalog",
        )?.count,
      ).toBe(0);
      expect(
        state.database.row<{ count: number }>(
          "SELECT COUNT(*) AS count FROM cf_app_payment_links",
        )?.count,
      ).toBe(0);
      expect(state.assets.objects.size).toBe(0);
      const compensation = stripeCalls.at(-1);
      expect(compensation?.path).toBe("/v1/payment_links/plink_paidApp123");
      expect(compensation?.form.get("active")).toBe("false");
    } finally {
      state.database.close();
    }
  });
});
