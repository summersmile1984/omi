import { defineConfig } from 'vitest/config';
import { resolve, relative } from 'node:path';
import { readFileSync } from 'node:fs';
import { rewriteRealtimeStart, rewriteRealtimeControl } from './realtime-overlay';
import { rewritePresentation } from '../../../deploy/web/presentation';
import { presentationFixture } from './tests/presentation-fixture';

export default defineConfig({
  plugins: [
    {
      name: 'fork-realtime-capability',
      enforce: 'pre',
      transform(source, id) {
        if (id === resolve(import.meta.dirname, '../src/hooks/useGeminiLive.ts'))
          return rewriteRealtimeStart(source);
        if (id === resolve(import.meta.dirname, '../src/components/home/HomePage.tsx'))
          source = rewriteRealtimeControl(source);
        for (const name of ['OmiOrb', 'OmiPulseMark']) {
          if (id === resolve(import.meta.dirname, `../src/components/ui/${name}.tsx`))
            return readFileSync(
              resolve(import.meta.dirname, `overlays/${name}.tsx`),
              'utf8',
            );
        }
        const path = relative(resolve(import.meta.dirname, '..'), id);
        return rewritePresentation(source, path, presentationFixture);
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
