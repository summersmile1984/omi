import { describe, expect, test } from 'bun:test';
import { createRequire } from 'node:module';
import {
  mkdir,
  mkdtemp,
  readFile,
  realpath,
  rm,
  symlink,
  writeFile,
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { publicEnvironment, injectPublicEnvironment } from './public-environment';
import { confinedPath, emptyOutput, rewriteMcpUrl, stageSources } from './source-stage';

const profile = {
  name: 'cloudflare.local',
  target: 'cloudflare',
  stage: 'local',
  identity_provider: 'better_auth',
  api_base_url: 'http://127.0.0.1:8787/service/',
  auth_base_url: 'http://127.0.0.1:8788',
  web_base_url: 'http://localhost:3000',
  mcp_base_url: 'http://127.0.0.1:9000/mcp',
  share_base_url: 'http://127.0.0.1:8787/share',
  objects_base_url: 'http://127.0.0.1:8787/objects',
  auth_callback_scheme: 'fixture-dev',
  capabilities: { push_provider: 'webhook' },
};

describe('the shared Web build boundary', () => {
  test('projects one validated profile without leaking secrets or dropping API mount paths', () => {
    const values = publicEnvironment({
      product_name: 'Fixture',
      profile: { ...profile, database_password: 'do-not-ship' },
    });
    expect(values.NEXT_PUBLIC_API_BASE_URL).toBe('http://127.0.0.1:8787/service');
    expect(values.NEXT_PUBLIC_WS_BASE_URL).toBe('ws://127.0.0.1:8787/service');
    expect(JSON.stringify(values)).not.toContain('do-not-ship');
    expect(JSON.parse(values.NEXT_PUBLIC_OMI_PROFILE_JSON).mcp_base_url).toBe(
      profile.mcp_base_url,
    );
    expect(
      injectPublicEnvironment(
        'globalThis.process = { env: {} };\nconsole.log("client");',
        values,
      ),
    ).toContain('NEXT_PUBLIC_OMI_PROFILE_JSON');
    expect(() => injectPublicEnvironment('new upstream banner', values)).toThrow(
      'banner changed',
    );
    expect(() =>
      publicEnvironment({
        product_name: 'Fixture',
        profile: { ...profile, auth_base_url: profile.api_base_url },
      }),
    ).toThrow('must be an origin');
    expect(() =>
      publicEnvironment({
        product_name: 'Fixture',
        profile: { ...profile, mcp_base_url: '' },
      }),
    ).toThrow('requires mcp_base_url');
  });

  test('changes the single MCP expression structurally, while refusing upstream drift', () => {
    const ts = createRequire(resolve(import.meta.dir, '../../web/app/package.json'))(
      'typescript',
    );
    const source =
      "'use client';\nexport function Page() { const mcpServerUrl = `${process.env.NEXT_PUBLIC_API_BASE_URL || 'https://api.omi.me'}/v1/mcp/sse`; return mcpServerUrl; }";
    const output = rewriteMcpUrl(source, ts);
    expect(output.startsWith("'use client';\nimport")).toBe(true);
    expect(output).toContain('const mcpServerUrl = profileMcpServerUrl()');
    expect(output).not.toContain('https://api.omi.me');
    expect(() =>
      rewriteMcpUrl(source.replace('mcpServerUrl', 'mcpEndpoint'), ts),
    ).toThrow('expected exactly one');
    expect(() =>
      rewriteMcpUrl(source.replace('https://api.omi.me', 'https://new.example'), ts),
    ).toThrow('source contract changed');
  });

  test('stages exact source replacements with no source writes, env copying or output aliasing', async () => {
    const temp = await mkdtemp(resolve(tmpdir(), 'web-stage-contract-'));
    const web = resolve(temp, 'web');
    const stage = resolve(temp, 'stage');
    try {
      await mkdir(resolve(web, 'src'), { recursive: true });
      await mkdir(resolve(web, 'fork'));
      await mkdir(resolve(web, 'node_modules'));
      await writeFile(resolve(web, 'src/firebase.ts'), 'old Firebase source');
      await writeFile(resolve(web, 'fork/firebase.ts'), 'new Better Auth facade');
      await writeFile(resolve(web, 'fork/share.ts'), 'new public share route');
      await writeFile(resolve(web, '.env.local'), 'PRIVATE=do-not-copy');
      const rows = await stageSources(web, stage, {
        schema_version: 1,
        files: { 'src/firebase.ts': 'fork/firebase.ts' },
        additions: { 'src/app/share.ts': 'fork/share.ts' },
      });
      expect(rows).toHaveLength(2);
      expect(rows.map((row) => row.mode)).toEqual(['replace', 'add']);
      expect(await readFile(resolve(stage, 'src/firebase.ts'), 'utf8')).toBe(
        'new Better Auth facade',
      );
      expect(await readFile(resolve(web, 'src/firebase.ts'), 'utf8')).toBe(
        'old Firebase source',
      );
      expect(await readFile(resolve(stage, 'src/app/share.ts'), 'utf8')).toBe(
        'new public share route',
      );
      await expect(
        stageSources(web, resolve(temp, 'collision-stage'), {
          schema_version: 1,
          files: {},
          additions: { 'src/firebase.ts': 'fork/share.ts' },
        }),
      ).rejects.toThrow('replace an existing source');
      expect(await Bun.file(resolve(stage, '.env.local')).exists()).toBe(false);
      expect(() => confinedPath(web, '../escape.ts')).toThrow('inside its source root');
      await symlink(web, resolve(temp, 'alias'));
      await expect(emptyOutput(resolve(temp, 'alias/build'), web)).rejects.toThrow(
        'outside the upstream',
      );
      const alias = resolve(temp, 'outside-alias');
      await mkdir(resolve(temp, 'outside'));
      await symlink(resolve(temp, 'outside'), alias);
      expect(await emptyOutput(resolve(alias, 'build'), web)).toBe(
        await realpath(resolve(temp, 'outside/build')),
      );
    } finally {
      await rm(temp, { recursive: true, force: true });
    }
  });
});
