import { readdirSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { DatabaseSync } from 'node:sqlite';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const require = createRequire(import.meta.url);
const { unstable_splitSqlQuery } = require('wrangler');

describe('D1 migration transport', () => {
  // Static tripwire, not a remote-D1 emulator. The 2026-09-07 hosted retry
  // migration failed with unparenthesized SELECT CASE; a minimal live D1
  // comparison rejected that shape and accepted SELECT (CASE ... END).
  // SQLite and Wrangler's local splitter both accepted the failed shape.
  // Evidence: dev/unified-main/implementation-2026-09-05/memory-consolidation-retry-2026-09-07.md.
  it('keeps CASE expressions parenthesized for the remote D1 migration parser (static)', () => {
    const directory = fileURLToPath(new URL('../migrations/app/', import.meta.url));
    for (const filename of readdirSync(directory).filter((name) => name.endsWith('.sql'))) {
      const statements = unstable_splitSqlQuery(readFileSync(directory + filename, 'utf8'));
      // Nested SELECT CASE inside a CTE is already parenthesized and is valid.
      expect(statements.join('\n'), filename).not.toMatch(/(?:BEGIN|;)\s*SELECT\s+CASE\b/i);
    }
  });
  it.each(['auth', 'app'])('executes %s migrations after the installed deploy CLI splits statements', (authority) => {
    const directory = fileURLToPath(new URL('../migrations/' + authority + '/', import.meta.url));
    const database = new DatabaseSync(':memory:');
    try {
      for (const filename of readdirSync(directory).filter((name) => name.endsWith('.sql')).sort()) {
        const parts = unstable_splitSqlQuery(readFileSync(directory + filename, 'utf8'));
        for (const part of parts) {
          try {
            database.exec(part);
          } catch (cause) {
            throw new Error(`${authority}/${filename}: Wrangler produced an unexecutable statement`, { cause });
          }
        }
      }
    } finally {
      database.close();
    }
  });
});
