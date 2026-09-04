import { defineConfig } from 'vitest/config';
import { resolve } from 'node:path';

export default defineConfig({
  root: resolve(import.meta.dirname, '..'),
  test: {
    environment: 'jsdom',
    include: ['fork/tests/*.test.tsx'],
    setupFiles: ['./vitest.setup.ts'],
  },
  resolve: { alias: { '@': resolve(import.meta.dirname, '../src') } },
});
