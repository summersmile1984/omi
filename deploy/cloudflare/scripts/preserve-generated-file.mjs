import { existsSync, readFileSync, writeFileSync } from "node:fs";

export function runPreservingGeneratedFile(filePath, operation) {
  const original = readFileSync(filePath);
  try {
    return operation();
  } finally {
    const current = existsSync(filePath) ? readFileSync(filePath) : null;
    if (!current?.equals(original)) writeFileSync(filePath, original);
  }
}
