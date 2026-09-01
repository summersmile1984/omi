import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { runPreservingGeneratedFile } from "../scripts/preserve-generated-file.mjs";

const temporaryDirectories = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function createGeneratedFile() {
  const directory = mkdtempSync(resolve(tmpdir(), "omi-cf-generated-file-"));
  temporaryDirectories.push(directory);
  const filePath = resolve(directory, "next-env.d.ts");
  writeFileSync(filePath, "tracked contents\n");
  return filePath;
}

describe("generated release files", () => {
  it("restores a tracked generated file after a successful command", () => {
    const filePath = createGeneratedFile();

    expect(
      runPreservingGeneratedFile(filePath, () => {
        writeFileSync(filePath, "generated contents\n");
        return "complete";
      }),
    ).toBe("complete");
    expect(readFileSync(filePath, "utf8")).toBe("tracked contents\n");
  });

  it("restores a tracked generated file when the command fails", () => {
    const filePath = createGeneratedFile();

    expect(() =>
      runPreservingGeneratedFile(filePath, () => {
        writeFileSync(filePath, "partial generated contents\n");
        throw new Error("type generation failed");
      }),
    ).toThrow("type generation failed");
    expect(readFileSync(filePath, "utf8")).toBe("tracked contents\n");
  });
});
