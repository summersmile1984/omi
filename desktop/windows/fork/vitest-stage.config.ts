import { defineConfig } from 'vitest/config'
export default defineConfig({
  esbuild: { jsx: 'automatic' },
  test: { include: ['fork/tests/staged/**/*.test.{ts,tsx}'], environment: 'node' }
})
