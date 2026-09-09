import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

export async function refreshBundleIntegrity(out: string) {
  const path = resolve(out, 'manifest.json');
  const manifest = JSON.parse(await readFile(path, 'utf8'));
  for (const asset of manifest.assets) {
    const file = Bun.file(resolve(out, asset.file));
    asset.integrity = `sha256-${new Bun.CryptoHasher('sha256')
      .update(await file.bytes())
      .digest('base64')}`;
  }
  await writeFile(path, JSON.stringify(manifest, null, 2) + '\n');
}

export async function compileWorker(
  out: string,
  environment: Record<string, string>,
  workerName: string,
) {
  const runtimeSource = resolve(import.meta.dir, 'worker-runtime.ts');
  const runtime = resolve(out, 'worker-runtime.ts');
  await writeFile(runtime, await readFile(runtimeSource));
  await writeFile(
    resolve(out, 'worker.ts'),
    `import './server.ts';
import { fetchApp } from './worker-runtime.ts';
import { isShareProxyRequest, proxyPublicGet } from '../src/lib/fork/public-proxy.ts';
export default {
  async fetch(request, env) {
    if (request.method === 'GET' || request.method === 'HEAD') {
      const asset = await env.ASSETS.fetch(request);
      if (asset.status !== 404) return asset;
    }
    if (isShareProxyRequest(request)) return proxyPublicGet(request, env.EDGE);
    return fetchApp(request);
  }
};\n`,
  );
  const shared = {
    target: 'browser' as const,
    format: 'esm' as const,
    conditions: ['workerd'],
    minify: true,
    sourcemap: 'none' as const,
    external: [
      'node:*',
      'path',
      'process',
      'url',
      'stream',
      'async_hooks',
      'util',
      'buffer',
    ],
    define: {
      'import.meta.dir': JSON.stringify('/app/.moonshine'),
      'process.env': JSON.stringify(environment),
    },
  };
  const assertBuilt = (result: Awaited<ReturnType<typeof Bun.build>>) => {
    if (!result.success)
      throw new Error(result.logs.map((log) => log.message).join('\n'));
  };
  // The original compiler selects Bun-specific React server code (Bun.hash).
  // Rebuild its generated entry with workerd conditions, retaining every route,
  // layout, data loader and support module from that very same source build.
  assertBuilt(
    await Bun.build({
      ...shared,
      entrypoints: [resolve(out, '.build/server.ts')],
      outdir: resolve(out, 'dist'),
    }),
  );
  await refreshBundleIntegrity(out);
  assertBuilt(
    await Bun.build({
      ...shared,
      entrypoints: [resolve(out, 'worker.ts')],
      outdir: resolve(out, 'cloudflare'),
      plugins: [
        {
          name: 'fork-worker-runtime',
          setup(build) {
            build.onResolve({ filter: /^@tschk\/moonshine-deploy-bun$/ }, () => ({
              path: runtime,
            }));
          },
        },
      ],
    }),
  );
  await writeFile(
    resolve(out, 'wrangler.json'),
    JSON.stringify(
      {
        name: workerName,
        main: 'cloudflare/worker.js',
        compatibility_date: '2026-08-27',
        compatibility_flags: ['nodejs_compat'],
        workers_dev: false,
        assets: { directory: 'public', binding: 'ASSETS', run_worker_first: true },
      },
      null,
      2,
    ) + '\n',
  );
}
