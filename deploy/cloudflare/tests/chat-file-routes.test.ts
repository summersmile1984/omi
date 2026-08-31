import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import jobs from "../workers/jobs/index";
import type { JobMessage } from "../workers/jobs/env";
import { createSignedAuthContext } from "../workers/shared/auth-context";

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
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

  prepare(sql: string) {
    const bind = (args: unknown[] = []) => ({
      bind: (...values: unknown[]) => bind(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...(args as never[])) as
          T | undefined) ?? null,
      all: async <T>() => ({
        results: this.database.prepare(sql).all(...(args as never[])) as T[],
      }),
      run: async () => ({
        meta: {
          changes: Number(
            this.database.prepare(sql).run(...(args as never[])).changes,
          ),
        },
      }),
    });
    return bind();
  }
}

async function headers(
  secret: string,
  method: "GET" | "POST" | "DELETE",
  pathname: string,
  uid = "file-user",
) {
  const signed = await createSignedAuthContext(
    { uid, authority: "better-auth", requestId: "chat-file-test" },
    "jobs",
    method,
    pathname,
    secret,
  );
  return {
    "x-omi-auth-context": signed!.encoded,
    "x-omi-internal-signature": signed!.signature,
  };
}

function environment(openaiKey = "provider-key", withImages = false) {
  const database = new SqliteD1();
  const objects = new Map<string, Uint8Array>();
  const deleted: string[] = [];
  const env: Record<string, unknown> = {
    APP_DB: database,
    INTERNAL_ASSERTION_SECRET: "chat-file-secret",
    OPENAI_API_KEY: openaiKey,
    PUBLIC_API_BASE_URL: "https://edge.test",
    CHAT_FILE_THUMBNAIL_SECRET: withImages ? "thumbnail-secret" : undefined,
    CHAT_FILES: {
      put: async (key: string, body: Uint8Array) => {
        objects.set(key, body);
      },
      get: async (key: string) => {
        const body = objects.get(key);
        return body
          ? {
              body: new Response(body.slice().buffer as ArrayBuffer).body!,
              httpMetadata: { contentType: key.endsWith("thumbnail.jpg") ? "image/jpeg" : "application/octet-stream" },
              httpEtag: '"test-etag"',
            }
          : null;
      },
      delete: async (key: string) => {
        objects.delete(key);
        deleted.push(key);
      },
      list: async () => ({
        objects: [...objects.keys()].map((key) => ({ key })),
      }),
    },
    JOBS: { send: async (_message: JobMessage) => undefined },
  };
  if (withImages) {
    env.IMAGES = {
      input: (_stream: ReadableStream<Uint8Array>) => ({
        transform: (_options: unknown) => ({
          output: async (_options: unknown) => ({
            response: () => new Response(new Uint8Array([8, 7, 6])),
            contentType: () => "image/jpeg",
            image: () => new ReadableStream<Uint8Array>(),
          }),
        }),
      }),
    };
  }
  return { database, env, objects, deleted };
}

afterEach(() => vi.unstubAllGlobals());

describe("Cloudflare private chat-file boundary", () => {
  it("uploads through R2 and OpenAI REST, then is idempotent and deletes both authorities", async () => {
    const { database, env, objects, deleted } = environment();
    const provider = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const method =
          init?.method || (input instanceof Request ? input.method : "GET");
        if (method === "POST")
          return Response.json({
            id: "file-provider-1",
            filename: "notes.txt",
          });
        return Response.json({ deleted: true });
      },
    );
    vi.stubGlobal("fetch", provider);
    const upload = async () => {
      const form = new FormData();
      form.set(
        "files",
        new File(["private notes"], "notes.txt", { type: "text/plain" }),
      );
      return jobs.fetch(
        new Request("https://jobs.test/v1/cf/chat-files", {
          method: "POST",
          headers: await headers(
            "chat-file-secret",
            "POST",
            "/v1/cf/chat-files",
          ),
          body: form,
        }),
        env as never,
      );
    };
    const first = await upload();
    expect(first.status).toBe(201);
    const [file] = (await first.json()) as Array<{
      id: string;
      openai_file_id: string;
      thumbnail: string;
    }>;
    expect(file).toMatchObject({
      openai_file_id: "file-provider-1",
      thumbnail: "",
    });
    expect([...objects.keys()]).toEqual([
      expect.stringMatching(/^file-user\/[0-9a-f-]{36}$/),
    ]);
    expect(objects.size).toBe(1);
    expect(provider).toHaveBeenCalledTimes(1);

    const otherAccount = await jobs.fetch(
      new Request(`https://jobs.test/v1/cf/chat-files/${file.id}`, {
        method: "DELETE",
        headers: await headers(
          "chat-file-secret",
          "DELETE",
          `/v1/cf/chat-files/${file.id}`,
          "other-file-user",
        ),
      }),
      env as never,
    );
    expect(otherAccount.status).toBe(404);
    expect(provider).toHaveBeenCalledTimes(1);

    const listed = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/chat-files", {
        headers: await headers("chat-file-secret", "GET", "/v1/cf/chat-files"),
      }),
      env as never,
    );
    expect(listed.status).toBe(200);
    expect(await listed.json()).toEqual([
      expect.objectContaining({ id: file.id }),
    ]);

    const duplicate = await upload();
    expect(duplicate.status).toBe(201);
    expect(await duplicate.json()).toEqual([
      expect.objectContaining({ id: file.id }),
    ]);
    expect(provider).toHaveBeenCalledTimes(1);

    const removed = await jobs.fetch(
      new Request(`https://jobs.test/v1/cf/chat-files/${file.id}`, {
        method: "DELETE",
        headers: await headers(
          "chat-file-secret",
          "DELETE",
          `/v1/cf/chat-files/${file.id}`,
        ),
      }),
      env as never,
    );
    expect(removed.status).toBe(200);
    expect(await removed.json()).toEqual({ status: "ok", id: file.id });
    expect(provider).toHaveBeenCalledTimes(2);
    expect(objects.size).toBe(0);
    expect(deleted).toHaveLength(1);

    const unauthorized = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/chat-files", { method: "GET" }),
      env as never,
    );
    expect(unauthorized.status).toBe(401);
    database.database.close();
  });

  it("fails closed when provider credentials are absent and rejects images without a thumbnail contract", async () => {
    const missing = environment("");
    const form = new FormData();
    form.set("files", new File(["plain"], "notes.txt", { type: "text/plain" }));
    const unavailable = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/chat-files", {
        method: "POST",
        headers: await headers("chat-file-secret", "POST", "/v1/cf/chat-files"),
        body: form,
      }),
      missing.env as never,
    );
    expect(unavailable.status).toBe(503);
    expect(missing.objects.size).toBe(0);

    const images = environment();
    const imageForm = new FormData();
    imageForm.set(
      "files",
      new File(["not-an-image"], "photo.png", { type: "image/png" }),
    );
    const imageResponse = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/chat-files", {
        method: "POST",
        headers: await headers("chat-file-secret", "POST", "/v1/cf/chat-files"),
        body: imageForm,
      }),
      images.env as never,
    );
    expect(imageResponse.status).toBe(503);
    expect(await imageResponse.json()).toMatchObject({
      error: "thumbnail_unavailable",
    });
    expect(images.objects.size).toBe(0);
    missing.database.database.close();
    images.database.database.close();
  });

  it("uses the Images binding for private thumbnails and the vision provider purpose", async () => {
    const { database, env, objects } = environment("provider-key", true);
    const purposes: string[] = [];
    const provider = vi.fn(
      async (_input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method || "GET";
        if (method === "POST") {
          purposes.push(String((init?.body as FormData).get("purpose")));
          return Response.json({ id: "file-image-1", filename: "photo.png" });
        }
        return Response.json({ deleted: true });
      },
    );
    vi.stubGlobal("fetch", provider);
    const form = new FormData();
    form.set(
      "files",
      new File([new Uint8Array([137, 80, 78, 71])], "photo.png", {
        type: "image/png",
      }),
    );
    const upload = await jobs.fetch(
      new Request("https://jobs.test/v1/cf/chat-files", {
        method: "POST",
        headers: await headers("chat-file-secret", "POST", "/v1/cf/chat-files"),
        body: form,
      }),
      env as never,
    );
    expect(upload.status).toBe(201);
    const [file] = (await upload.json()) as Array<{
      id: string;
      thumbnail: string;
      openai_file_id: string;
    }>;
    expect(file).toMatchObject({
      openai_file_id: "file-image-1",
      thumbnail: expect.stringContaining("/v1/cf/chat-files/"),
    });
    expect(purposes).toEqual(["vision"]);
    expect([...objects.values()]).toContainEqual(new Uint8Array([8, 7, 6]));

    const thumb = await jobs.fetch(new Request(file.thumbnail), env as never);
    expect(thumb.status).toBe(200);
    expect(thumb.headers.get("content-type")).toBe("image/jpeg");
    expect([...new Uint8Array(await thumb.arrayBuffer())]).toEqual([8, 7, 6]);

    const removed = await jobs.fetch(
      new Request(`https://jobs.test/v1/cf/chat-files/${file.id}`, {
        method: "DELETE",
        headers: await headers(
          "chat-file-secret",
          "DELETE",
          `/v1/cf/chat-files/${file.id}`,
        ),
      }),
      env as never,
    );
    expect(removed.status).toBe(200);
    expect(objects.size).toBe(0);
    database.database.close();
  });

  it("exposes both legacy aliases through the canonical handler only when explicitly enabled", async () => {
    const { database, env } = environment();
    env.LEGACY_CHAT_FILES_STAGING_ENABLED = "true";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ id: "file-alias-1", filename: "a.txt" })),
    );
    for (const path of ["/v1/files", "/v2/files"]) {
      const form = new FormData();
      form.set("files", new File(["alias"], "a.txt", { type: "text/plain" }));
      const response = await jobs.fetch(
        new Request(`https://jobs.test${path}`, {
          method: "POST",
          headers: await headers("chat-file-secret", "POST", path),
          body: form,
        }),
        env as never,
      );
      expect(response.status, path).toBe(200);
      expect(await response.json()).toEqual([
        expect.objectContaining({ id: expect.any(String), name: "a.txt" }),
      ]);
    }
    database.database.close();
  });
});
