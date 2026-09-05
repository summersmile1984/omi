#!/usr/bin/env bun
import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { compileWorker, refreshBundleIntegrity } from './compile-worker';
import { injectPublicEnvironment, publicEnvironment } from './public-environment';
import {
  applyBrandMetadata,
  applyMcpOverlay,
  emptyOutput,
  stageSources,
} from './source-stage';
import { applyRealtimeOverlay } from '../../web/app/fork/realtime-overlay';
import { generateWebAssets } from './brand-assets.mjs';

const root = resolve(import.meta.dir, '../..');
const webRoot = resolve(root, 'web/app');

function run(command: string[], cwd: string, environment?: Record<string, string>) {
  const result = Bun.spawnSync(command, {
    cwd,
    env: { ...process.env, ...environment },
    stdout: 'inherit',
    stderr: 'inherit',
  });
  if (result.exitCode !== 0) throw new Error(`${command[0]} exited ${result.exitCode}`);
}

export async function buildWeb(options: {
  target: string;
  stage: string;
  output: string;
  brand?: string;
  manifest?: string;
}) {
  if (
    !['self_hosted', 'cloudflare'].includes(options.target) ||
    !['local', 'beta', 'production'].includes(options.stage)
  ) {
    throw new Error('Select self_hosted or cloudflare and a local/beta/production stage');
  }
  const profileCommand = [
    process.env.PYTHON ?? resolve(root, 'backend/.venv/bin/python'),
    resolve(import.meta.dir, 'profile_input.py'),
    '--target',
    options.target,
    '--stage',
    options.stage,
  ];
  if (options.brand) profileCommand.push('--brand', options.brand);
  if (options.manifest) profileCommand.push('--manifest', resolve(options.manifest));
  const profileResult = Bun.spawnSync(profileCommand, {
    cwd: root,
    stdout: 'pipe',
    stderr: 'pipe',
  });
  if (profileResult.exitCode) throw new Error(profileResult.stderr.toString());
  const input = JSON.parse(profileResult.stdout.toString());
  const environment = publicEnvironment(input);
  const overlays = JSON.parse(
    await readFile(resolve(webRoot, 'fork/overlays.json'), 'utf8'),
  );
  for (const required of [
    'src/lib/firebase.ts',
    'src/components/auth/AuthProvider.tsx',
    'src/app/login/LoginClient.tsx',
    'src/components/auth/LoginPanel.tsx',
  ]) {
    if (!overlays.files?.[required])
      throw new Error(`Missing required Web auth overlay: ${required}`);
  }
  if (!overlays.files?.['src/app/api/proxy/public/[...path]/route.ts'])
    throw new Error('Missing required public share proxy overlay');
  for (const required of [
    'src/app/(public)/chat/[token]/page.tsx',
    'src/app/(public)/tasks/[token]/page.tsx',
  ]) {
    if (!overlays.additions?.[required])
      throw new Error(`Missing required public share route: ${required}`);
  }
  const output = await emptyOutput(options.output, webRoot);
  const source = resolve(output, 'source');
  const applied = await stageSources(webRoot, source, overlays);
  const brandAssets = generateWebAssets(
    input.brand_id,
    input.asset_input,
    resolve(source, 'public'),
  );
  await applyMcpOverlay(source, webRoot);
  await applyRealtimeOverlay(source);
  // Upstream tests remain on the untouched upstream lane. This check compiles
  // every production source file after overlaying the alternate identity owner.
  const stagedTypes = resolve(source, 'tsconfig.fork.json');
  await writeFile(
    stagedTypes,
    JSON.stringify(
      {
        extends: './tsconfig.json',
        compilerOptions: { incremental: false },
        exclude: [
          'node_modules',
          '.moonshine',
          'src/**/__tests__/**',
          'src/**/*.test.ts',
          'src/**/*.test.tsx',
          'scripts/**/*.test.ts',
        ],
      },
      null,
      2,
    ) + '\n',
  );
  run([resolve(webRoot, 'node_modules/.bin/tsc'), '--project', stagedTypes], source);

  // Better Auth + webhook profiles do not configure Firebase service workers.
  // Keep the original compiler/assets scripts; only the source stage changes.
  run([resolve(webRoot, 'node_modules/.bin/moonshine'), 'build'], source, environment);
  run(['bun', 'run', 'build:assets'], source, environment);
  const generated = resolve(source, '.moonshine');
  await applyBrandMetadata(generated, webRoot, input);
  const client = resolve(generated, 'public/client.js');
  await writeFile(
    client,
    injectPublicEnvironment(await readFile(client, 'utf8'), environment),
  );
  for (const file of ['firebase-messaging-sw.js', 'firebase-messaging-sw.js.template']) {
    await rm(resolve(generated, 'public', file), { force: true });
  }
  await refreshBundleIntegrity(generated);
  if (options.target === 'cloudflare') {
    await compileWorker(generated, environment, `${input.brand_id}-web-${options.stage}`);
  } else {
    // Bun's generated server reads the same explicit API base at runtime.
    await writeFile(
      resolve(generated, 'start.ts'),
      `Object.assign(process.env, ${JSON.stringify(
        environment,
      )});\nawait import('./server.ts');\n`,
    );
    const bundled = await Bun.build({
      entrypoints: [resolve(generated, 'start.ts')],
      outdir: generated,
      target: 'bun',
      format: 'esm',
      minify: true,
    });
    if (!bundled.success)
      throw new Error(bundled.logs.map((log) => log.message).join('\n'));
  }
  const manifest = JSON.parse(
    await readFile(resolve(generated, 'manifest.json'), 'utf8'),
  );
  const artifact = resolve(output, 'artifact');
  await mkdir(artifact);
  await cp(resolve(generated, 'public'), resolve(artifact, 'public'), {
    recursive: true,
  });
  if (options.target === 'cloudflare') {
    await cp(resolve(generated, 'cloudflare'), resolve(artifact, 'cloudflare'), {
      recursive: true,
    });
    await cp(resolve(generated, 'wrangler.json'), resolve(artifact, 'wrangler.json'));
  } else {
    await cp(resolve(generated, 'start.js'), resolve(artifact, 'start.js'));
  }
  const git = Bun.spawnSync(['git', 'rev-parse', 'HEAD'], {
    cwd: root,
    stdout: 'pipe',
  });
  const sourceStatus = Bun.spawnSync(['git', 'status', '--porcelain'], {
    cwd: root,
    stdout: 'pipe',
  });
  const result = {
    schema_version: 1,
    source_commit: git.stdout.toString().trim(),
    source_dirty: sourceStatus.stdout.toString().trim().length > 0,
    target: options.target,
    stage: options.stage,
    brand: input.brand_id,
    profile: JSON.parse(environment.NEXT_PUBLIC_OMI_PROFILE_JSON),
    overlays: applied,
    brand_assets: brandAssets,
    transforms: [
      'SettingsPage.mcpServerUrl -> profile MCP origin',
      'HomePage/useGeminiLive -> direct-model capability',
      'public Chat/Tasks share routes -> controlled preview and authenticated acceptance',
      'allowlisted public environment',
      ...(brandAssets.mode === 'manifest'
        ? ['generated server presentation metadata -> manifest product name and tagline']
        : []),
    ],
    routes: manifest.routes.map((route: { path: string }) => route.path),
    artifact,
    entry: options.target === 'cloudflare' ? 'wrangler.json' : 'start.js',
    release_ready: false,
    pending_qualification: [
      'full dual-target browser and previous-schema qualification',
      'complete brand assets and remote custom-domain qualification',
    ],
  };
  await writeFile(
    resolve(output, 'build-manifest.json'),
    JSON.stringify(result, null, 2) + '\n',
  );
  console.log(
    `Built ${manifest.routes.length} routes for ${options.target}.${options.stage}: ${artifact}`,
  );
  return result;
}

if (import.meta.main) {
  const { values } = parseArgs({
    args: Bun.argv.slice(2),
    options: {
      target: { type: 'string' },
      stage: { type: 'string' },
      output: { type: 'string' },
      brand: { type: 'string' },
      manifest: { type: 'string' },
    },
    strict: true,
  });
  if (!values.target || !values.stage || !values.output)
    throw new Error('Required: --target --stage --output [--brand ID | --manifest PATH]');
  await buildWeb({
    target: values.target,
    stage: values.stage,
    output: values.output,
    brand: values.brand,
    manifest: values.manifest,
  });
}
