import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { describe, expect, it } from "vitest";

const cloudflareDirectory = path.resolve(import.meta.dirname, "..");

const reconcileScripts = [
  "chat-file-reconcile.mjs",
  "chat-history-reconcile.mjs",
  "memory-archive-reconcile.mjs",
  "persona-app-history-reconcile.mjs",
  "phone-history-reconcile.mjs",
  "wrapped-history-reconcile.mjs",
  "audio-reconcile.mjs",
];

describe("historical reconcile input boundaries", () => {
  it("rejects invalid UTF-8 before replacement characters can enter a plan", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "omi-history-input-"));
    const input = path.join(directory, "invalid-utf8.json");
    try {
      // This is syntactically valid JSON only after Buffer#toString("utf8")
      // silently replaces the invalid byte with U+FFFD.
      await writeFile(input, Buffer.from([0x22, 0xff, 0x22]));
      for (const script of reconcileScripts) {
        const result = spawnSync(
          process.execPath,
          [path.join("scripts", script), "--input", input],
          { cwd: cloudflareDirectory, encoding: "utf8" },
        );
        expect(result.status, script).not.toBe(0);
        expect(`${result.stdout}${result.stderr}`, script).toContain(
          "input is not valid JSON",
        );
      }
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
});
