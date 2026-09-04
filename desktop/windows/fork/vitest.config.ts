import { defineConfig } from 'vitest/config'
export default defineConfig({ test: { include: ['fork/tests/*.test.ts'], environment: 'node' } })
