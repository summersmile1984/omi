// LIFECYCLE: permanent
// Run the generated application: beta CD 34419433912 shipped a missing route.
import { checkGateway } from './gateway-ci.mjs';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

const root = resolve(import.meta.dirname, '../..');
const require = createRequire(resolve(root, 'deploy/cloudflare/package.json'));
const { Miniflare, convertV4MiniflareOptions } = require('miniflare');
const { unstable_getMiniflareWorkerOptions } = require('wrangler');
const output = mkdtempSync(resolve(tmpdir(), 'eddy-web-runtime-'));
const image = `eddy-web-ci:${randomUUID()}`;
let container;
function command(args, capture = false) {
  const result = spawnSync(args[0], args.slice(1), {
    cwd: root, encoding: 'utf8', stdio: capture ? 'pipe' : 'inherit', timeout: 15 * 60 * 1000,
  });
  if (result.status !== 0) throw new Error(`${args[0]} failed in Web runtime qualification`);
  return result.stdout?.trim();
}
async function webChecks(fetchApp, artifact) {
  const login = await fetchApp('/login');
  assert.equal(login.status, 200, 'generated Web login');
  assert.match(login.headers.get('content-type') ?? '', /text\/html/);
  assert.match(await login.text(), /Eddy/);
  for (const name of ['favicon.png', 'logo.png']) {
    const response = await fetchApp('/' + name);
    assert.equal(response.status, 200, name);
    assert.deepEqual(Buffer.from(await response.arrayBuffer()), readFileSync(resolve(artifact, 'public', name)));
  }
  const privateRoute = await fetchApp('/api/proxy/v2/messages');
  assert.equal(privateRoute.status, 401, 'generated same-origin authentication admission');
}
try {
  for (const target of ['cloudflare', 'self_hosted']) {
    const directory = resolve(output, target);
    command(['bun', 'deploy/web/build.ts', '--target', target, '--stage', 'beta', '--brand', 'eddy', '--output', directory]);
    const artifact = resolve(directory, 'artifact');
    if (target === 'cloudflare') {
      const { main, workerOptions, externalWorkers } = unstable_getMiniflareWorkerOptions(resolve(artifact, 'wrangler.json'));
      let edgeStatus = 200;
      const runtime = new Miniflare(convertV4MiniflareOptions({
        host: '127.0.0.1', port: 0, cf: false,
        workers: [{ ...workerOptions, modules: [{ type: 'ESModule', path: main }],
          serviceBindings: { EDGE: async request => new URL(request.url).pathname === '/v2/messages'
            ? Response.json({ source: 'private-edge-binding' })
            : new URL(request.url).pathname === '/ready'
            ? Response.json({ status: edgeStatus === 200 ? 'ready' : 'unavailable' }, { status: edgeStatus })
            : Response.json({ error: 'missing synthetic share' }, { status: edgeStatus === 200 ? 404 : edgeStatus }) },
          outboundService: () => { throw new Error('Web CI must not call a live service'); },
        }, ...externalWorkers],
      }));
      try {
        await runtime.ready;
        const request = path => runtime.dispatchFetch('http://web.fixture.invalid' + path);
        await webChecks(request, artifact);
        const proxy = await runtime.dispatchFetch('http://web.fixture.invalid/api/proxy/v2/messages', {
          headers: { Authorization: 'Bearer synthetic-transport-test' },
        });
        assert.equal(proxy.status, 200);
        assert.deepEqual(await proxy.json(), { source: 'private-edge-binding' }, 'authenticated proxy must stay in its selected Worker graph');
        assert.deepEqual(await (await request('/api/worker-ready')).json(), { status: 'ready' });
        const share = '/api/proxy/public/v1/action-items/shared/' + '0'.repeat(32);
        assert.equal((await request(share)).status, 404);
        edgeStatus = 503;
        assert.equal((await request('/api/worker-ready')).status, 503, 'actual generated Worker must reject failed Edge');
        assert.equal((await request(share)).status, 503, 'actual generated share proxy must propagate failed Edge');
      } finally { await runtime.dispose(); }
    } else {
      command(['docker', 'build', '-f', resolve(root, 'deploy/web/Dockerfile'), '-t', image, artifact]);
      container = command(['docker', 'run', '--detach', '--publish', '127.0.0.1::3000',
        '--health-cmd', 'wget -q -O /dev/null http://127.0.0.1:3000/login', '--health-interval', '1s',
        '--health-timeout', '3s', '--health-retries', '30', image], true);
      const address = command(['docker', 'port', container, '3000/tcp'], true);
      const deadline = Date.now() + 60000;
      while (command(['docker', 'inspect', '--format', '{{.State.Health.Status}}', container], true) === 'starting') {
        if (Date.now() > deadline) throw new Error('Web image startup deadline exceeded');
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      assert.equal(command(['docker', 'inspect', '--format', '{{.State.Health.Status}}', container], true), 'healthy');
      await webChecks(path => fetch('http://' + address + path, { redirect: 'error', signal: AbortSignal.timeout(10000) }), artifact);
      await checkGateway(root, directory, image);
      command(['docker', 'rm', '--force', container]);
      container = undefined;
    }
    console.log(`PASS generated ${target} Web: login, exact assets, authentication and runtime boundaries`);
  }
} finally {
  if (container) command(['docker', 'rm', '--force', container]);
  // Remove only this invocation's generated image and directories.
  spawnSync('docker', ['image', 'rm', image], { stdio: 'ignore' });
  rmSync(output, { recursive: true, force: true });
}
