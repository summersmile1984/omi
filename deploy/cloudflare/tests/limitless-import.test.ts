import { describe, expect, it } from "vitest";
import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import { deflateRawSync } from "node:zlib";
import path from "node:path";
import { fileURLToPath } from "node:url";
import jobs from "../workers/jobs/index";
import type { JobMessage } from "../workers/jobs/env";
import { processLimitlessImportMessage } from "../workers/jobs/limitless-import";
import { createSignedAuthContext } from "../workers/shared/auth-context";
import {
  LimitlessImportError,
  parseLimitlessMarkdown,
  validZipPath,
} from "../workers/jobs/limitless-import";

describe("Limitless import parser", () => {
  it("parses the exported filename and transcript quotes into a canonical conversation", () => {
    const markdown = new TextEncoder().encode(
      "# Planning\n\n## Topics\n\n> [1](#startMs=1000&endMs=2500): hello\n> [2](#startMs=2500&endMs=4000): world\n",
    );
    const result = parseLimitlessMarkdown(
      markdown,
      "lifelogs/2026-08-31_07h00m25s_Planning.md",
      "en",
      "conversation-id",
    );
    expect(result?.title).toBe("Planning");
    expect(result?.startedAt).toBe(Date.parse("2026-08-31T07:00:25Z") / 1000);
    expect(result?.finishedAt).toBe(result!.startedAt + 3);
    expect(result?.segments).toHaveLength(2);
    expect(result?.segments[0]).toMatchObject({
      text: "hello",
      start: 0,
      end: 1.5,
      is_user: true,
    });
  });

  it("fails closed on malformed UTF-8 and unsafe ZIP paths", () => {
    expect(() =>
      parseLimitlessMarkdown(
        new Uint8Array([0xc3, 0x28]),
        "lifelogs/2026-08-31_07h00m25s_Bad.md",
        "en",
        "id",
      ),
    ).toThrowError(LimitlessImportError);
    expect(validZipPath("lifelogs/good.md")).toBe(true);
    expect(validZipPath("../lifelogs/escape.md")).toBe(false);
    expect(validZipPath("/lifelogs/absolute.md")).toBe(false);
    expect(validZipPath("lifelogs\\escape.md")).toBe(false);
  });
});

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
        this.database.prepare(sql).get(...(args as never[])) as T | null,
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

async function jobHeaders(
  secret: string,
  method: "POST",
  pathname: string,
  uid = "import-user",
) {
  const signed = await createSignedAuthContext(
    { uid, authority: "better-auth", requestId: "limitless-test" },
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

describe("Limitless import admission", () => {
  it("stages a bounded multipart ZIP once and keeps the job uid scoped", async () => {
    const database = new SqliteD1();
    const queue: JobMessage[] = [];
    const objects = new Map<string, BodyInit>();
    const secret = "limitless-test-secret";
    const env = {
      APP_DB: database,
      INTERNAL_ASSERTION_SECRET: secret,
      ASSETS: {
        put: async (key: string, body: BodyInit) => {
          objects.set(key, body);
        },
        delete: async (key: string) => {
          objects.delete(key);
        },
      },
      JOBS: {
        send: async (message: JobMessage) => {
          queue.push(message);
        },
      },
    };
    const request = async (uid = "import-user") => {
      const body = new FormData();
      body.set(
        "file",
        new File(["not-a-zip"], "export.zip", { type: "application/zip" }),
      );
      body.set("language", "en");
      return jobs.fetch(
        new Request("https://jobs.test/v1/import/limitless", {
          method: "POST",
          headers: await jobHeaders(
            secret,
            "POST",
            "/v1/import/limitless",
            uid,
          ),
          body,
        }),
        env as never,
      );
    };
    const first = await request();
    expect(first.status).toBe(202);
    const firstBody = (await first.json()) as {
      job_id: string;
      status: string;
    };
    expect(firstBody.status).toBe("pending");
    expect(queue).toHaveLength(1);
    expect(objects.size).toBe(1);
    const duplicate = await request();
    expect(duplicate.status).toBe(200);
    expect(((await duplicate.json()) as { job_id: string }).job_id).toBe(
      firstBody.job_id,
    );
    const stranger = await request("other-user");
    expect(stranger.status).toBe(202);
    expect(queue).toHaveLength(2);
    database.database.close();
  });

  it("marks a malformed staged ZIP terminal and removes the staged object", async () => {
    const database = new SqliteD1();
    const deleted: string[] = [];
    database.database
      .prepare(
        "INSERT INTO cf_import_jobs " +
          "(uid, job_id, source_type, source_object_key, source_filename, language_code, request_fingerprint, status, created_at, updated_at) " +
          "VALUES ('import-user', 'job-malformed', 'limitless', 'imports/import-user/job-malformed.zip', 'bad.zip', 'en', ?, 'pending', 1, 1)",
      )
      .run("c".repeat(64));
    const env = {
      APP_DB: database,
      ASSETS: {
        get: async () => ({
          arrayBuffer: async () => new TextEncoder().encode("not a zip").buffer,
        }),
        delete: async (key: string) => {
          deleted.push(key);
        },
      },
    };
    let acknowledged = false;
    await processLimitlessImportMessage(
      {
        body: {
          jobId: "job-malformed",
          uid: "import-user",
          kind: "limitless_import",
          payload: {},
        },
        attempts: 1,
        ack: () => {
          acknowledged = true;
        },
        retry: () => {
          throw new Error("malformed input must not retry");
        },
      } as never,
      env as never,
    );
    expect(acknowledged).toBe(true);
    expect(deleted).toEqual(["imports/import-user/job-malformed.zip"]);
    expect(
      database.database
        .prepare(
          "SELECT status, last_error FROM cf_import_jobs WHERE job_id = 'job-malformed'",
        )
        .get(),
    ).toMatchObject({ status: "failed", last_error: "ZIP size is invalid" });
    database.database.close();
  });

  it("drains a stored/deflated ZIP into cf_conversations and rejects expansion bombs", async () => {
    const database = new SqliteD1();
    const archive = makeZip([
      {
        name: "lifelogs/2026-08-31_07h00m25s_Planning.md",
        data: "# Planning\n> [1](#startMs=0&endMs=1000): hello\n",
        method: 8,
      },
    ]);
    database.database
      .prepare(
        "INSERT INTO cf_import_jobs " +
          "(uid, job_id, source_type, source_object_key, source_filename, language_code, request_fingerprint, status, created_at, updated_at) " +
          "VALUES ('import-user', 'job-valid', 'limitless', 'imports/import-user/job-valid.zip', 'export.zip', 'en', ?, 'pending', 1, 1)",
      )
      .run("d".repeat(64));
    const deleted: string[] = [];
    const env = {
      APP_DB: database,
      ASSETS: {
        get: async () => ({
          arrayBuffer: async () =>
            archive.buffer.slice(
              archive.byteOffset,
              archive.byteOffset + archive.byteLength,
            ),
        }),
        delete: async (key: string) => {
          deleted.push(key);
        },
      },
    };
    let acknowledged = false;
    await processLimitlessImportMessage(
      {
        body: {
          jobId: "job-valid",
          uid: "import-user",
          kind: "limitless_import",
          payload: {},
        },
        attempts: 1,
        ack: () => {
          acknowledged = true;
        },
        retry: () => undefined,
      } as never,
      env as never,
    );
    expect(acknowledged).toBe(true);
    expect(
      database.database
        .prepare(
          "SELECT status, conversations_created FROM cf_import_jobs WHERE job_id = 'job-valid'",
        )
        .get(),
    ).toMatchObject({ status: "completed", conversations_created: 1 });
    expect(
      database.database
        .prepare(
          "SELECT source, language FROM cf_conversations WHERE uid = 'import-user'",
        )
        .get(),
    ).toEqual({ source: "limitless", language: "en" });
    expect(deleted).toEqual(["imports/import-user/job-valid.zip"]);
    database.database.close();

    const bombDb = new SqliteD1();
    const bomb = makeZip([
      {
        name: "lifelogs/2026-08-31_07h00m25s_Bomb.md",
        data: "x",
        method: 0,
        declaredUncompressed: 200_000_000,
      },
    ]);
    bombDb.database
      .prepare(
        "INSERT INTO cf_import_jobs " +
          "(uid, job_id, source_type, source_object_key, source_filename, language_code, request_fingerprint, status, created_at, updated_at) " +
          "VALUES ('import-user', 'job-bomb', 'limitless', 'imports/import-user/job-bomb.zip', 'bomb.zip', 'en', ?, 'pending', 1, 1)",
      )
      .run("e".repeat(64));
    const bombDeleted: string[] = [];
    const bombEnv = {
      APP_DB: bombDb,
      ASSETS: {
        get: async () => ({
          arrayBuffer: async () =>
            bomb.buffer.slice(
              bomb.byteOffset,
              bomb.byteOffset + bomb.byteLength,
            ),
        }),
        delete: async (key: string) => {
          bombDeleted.push(key);
        },
      },
    };
    await processLimitlessImportMessage(
      {
        body: {
          jobId: "job-bomb",
          uid: "import-user",
          kind: "limitless_import",
          payload: {},
        },
        attempts: 1,
        ack: () => undefined,
        retry: () => {
          throw new Error("bomb must not retry");
        },
      } as never,
      bombEnv as never,
    );
    expect(
      bombDb.database
        .prepare(
          "SELECT status, last_error FROM cf_import_jobs WHERE job_id = 'job-bomb'",
        )
        .get(),
    ).toMatchObject({
      status: "failed",
      last_error: "ZIP entry exceeds expansion limits",
    });
    expect(bombDeleted).toEqual(["imports/import-user/job-bomb.zip"]);
    bombDb.database.close();
  });
});

type ZipFixture = {
  name: string;
  data: string;
  method: 0 | 8;
  declaredUncompressed?: number;
};

function crc32(data: Uint8Array): number {
  let crc = 0xffffffff;
  for (const value of data) {
    crc ^= value;
    for (let bit = 0; bit < 8; bit++)
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function makeZip(files: ZipFixture[]): Uint8Array {
  const local: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  for (const file of files) {
    const name = new TextEncoder().encode(file.name);
    const raw = new TextEncoder().encode(file.data);
    const body = file.method === 8 ? new Uint8Array(deflateRawSync(raw)) : raw;
    const declared = file.declaredUncompressed ?? raw.length;
    const localHeader = new Uint8Array(30 + name.length + body.length);
    const lv = new DataView(localHeader.buffer);
    lv.setUint32(0, 0x04034b50, true);
    lv.setUint16(4, 20, true);
    lv.setUint16(8, file.method, true);
    lv.setUint32(14, crc32(raw), true);
    lv.setUint32(18, body.length, true);
    lv.setUint32(22, declared, true);
    lv.setUint16(26, name.length, true);
    localHeader.set(name, 30);
    localHeader.set(body, 30 + name.length);
    local.push(localHeader);
    const header = new Uint8Array(46 + name.length);
    const cv = new DataView(header.buffer);
    cv.setUint32(0, 0x02014b50, true);
    cv.setUint16(4, 20, true);
    cv.setUint16(6, 20, true);
    cv.setUint16(10, file.method, true);
    cv.setUint32(16, crc32(raw), true);
    cv.setUint32(20, body.length, true);
    cv.setUint32(24, declared, true);
    cv.setUint16(28, name.length, true);
    cv.setUint32(42, offset, true);
    header.set(name, 46);
    central.push(header);
    offset += localHeader.length;
  }
  const cdSize = central.reduce((sum, item) => sum + item.length, 0);
  const end = new Uint8Array(22);
  const ev = new DataView(end.buffer);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, files.length, true);
  ev.setUint16(10, files.length, true);
  ev.setUint32(12, cdSize, true);
  ev.setUint32(16, offset, true);
  const result = new Uint8Array(offset + cdSize + end.length);
  let cursor = 0;
  for (const part of [...local, ...central, end]) {
    result.set(part, cursor);
    cursor += part.length;
  }
  return result;
}
