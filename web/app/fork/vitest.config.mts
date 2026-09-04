import { defineConfig } from 'vitest/config';
import { resolve } from 'node:path';
import { rewriteRealtimeStart, rewriteRealtimeControl } from './realtime-overlay';

export default defineConfig({
  plugins: [
    {
      name: 'fork-realtime-capability',
      transform(source, id) {
        if (id === resolve(import.meta.dirname, '../src/hooks/useGeminiLive.ts'))
          return rewriteRealtimeStart(source);
        if (id === resolve(import.meta.dirname, '../src/components/home/HomePage.tsx'))
          return rewriteRealtimeControl(source);
      },
    },
  ],
  root: resolve(import.meta.dirname, '..'),
  test: {
    environment: 'jsdom',
    include: ['fork/tests/*.test.tsx'],
    setupFiles: ['./vitest.setup.ts'],
  },
  resolve: { alias: { '@': resolve(import.meta.dirname, '../src') } },
});
