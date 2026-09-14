// LIFECYCLE: permanent
// Exercise the deployed nginx renderer with controlled network peers. The local
// release rehearsal exposed cached Docker addresses after backend recreation.
import { request as httpRequest } from 'node:http';
import { Readable } from 'node:stream';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

export async function checkGateway(root, directory, image) {
  const require = createRequire(resolve(root, 'deploy/cloudflare/package.json'));
  const WebSocket = require('ws');
  const network = 'eddy-gateway-ci-' + randomUUID();
  const containers = new Set();
  function command(args, input) {
    const result = spawnSync(args[0], args.slice(1), { cwd: root, input, encoding: 'utf8', timeout: 60000 });
    assert.equal(result.status, 0, `${args[0]} gateway fixture failed: ${result.stderr}`);
    return result.stdout.trim();
  }
  const fixture = resolve(directory, 'gateway-fixture.js');
  writeFileSync(fixture, `
let pending;
for (const port of [8080, 3000, 9000]) Bun.serve({ port,
  fetch(request, server) {
    const url = new URL(request.url);
    if (request.headers.get('upgrade') === 'websocket') {
      if (server.upgrade(request)) return;
      return new Response(null, {status:400});
    }
    if (url.pathname === '/stream') return new Response(new ReadableStream({start(controller) {
      controller.enqueue(new TextEncoder().encode('first'));
      pending = () => { controller.enqueue(new TextEncoder().encode('second')); controller.close(); };
    }}), {headers:{'content-type':'text/event-stream'}});
    if (url.pathname === '/continue') { pending(); return new Response('done'); }
    return Response.json({ generation: process.env.GENERATION, host: request.headers.get('host'),
      proto: request.headers.get('x-forwarded-proto'), path: url.pathname + url.search });
  }, websocket: { open(ws) { ws.send('connected'); }, message(ws, message) { ws.send(message); } }
});
`);
  const run = (...args) => { const id = command(['docker', 'run', '--detach', ...args]); containers.add(id); return id; };
  const remove = id => { command(['docker', 'rm', '--force', id]); containers.delete(id); };
  const peer = generation => run('--network', network, '--network-alias', 'backend', '--network-alias', 'auth-server',
    '--network-alias', 'web', '--network-alias', 'minio', '-v', `${fixture}:/fixture.js:ro`,
    '-e', `GENERATION=${generation}`, image, 'bun', '/fixture.js');
  try {
    command(['docker', 'network', 'create', network]);
    const first = peer('first');
    const config = command([resolve(root, 'backend/.venv/bin/python'), '-c',
      "import sys,json; sys.path.insert(0,'scripts/fork'); from server_gateway import gateway_config,NGINX_IMAGE; p={k+'_base_url':'https://'+k+'.fixture.invalid' for k in ('api','auth','web','objects')}; print(json.dumps({'nginx':gateway_config(p,NGINX_IMAGE,'sha256:'+'a'*64,18080)[0],'image':NGINX_IMAGE}))"]);
    const rendered = JSON.parse(config);
    const path = resolve(directory, 'nginx.conf');
    writeFileSync(path, rendered.nginx);
    const gateway = run('--network', network, '-p', '127.0.0.1::8080', '-v', `${path}:/etc/nginx/nginx.conf:ro`, rendered.image);
    const address = command(['docker', 'port', gateway, '8080/tcp']);
    const request = (path, host = 'api', init = {}) => new Promise((accept, reject) => {
      const request = httpRequest('http://' + address + path, {
        ...init, headers: { Host: `${host}.fixture.invalid`, ...init.headers }, signal: AbortSignal.timeout(10000),
      }, response => accept(new Response(Readable.toWeb(response), {status: response.statusCode, headers: response.headers})));
      request.on('error', reject);
      request.end(init.body);
    });
    const deadline = Date.now() + 30000;
    for (;;) {
      try { if ((await request('/ready')).status === 200) break; } catch {}
      assert.ok(Date.now() < deadline, 'gateway startup deadline');
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    for (const host of ['api', 'auth', 'web', 'objects']) {
      const response = await request('/public?value=1', host);
      assert.deepEqual(await response.json(), { generation: 'first', host: host + '.fixture.invalid', proto: 'https', path: '/public?value=1' });
      assert.equal((await request('/internal/private', host)).status, 404);
    }
    assert.equal((await request('/ready', 'unknown')).status, 404);
    const stream = (await request('/stream')).body.getReader();
    assert.equal(Buffer.from((await stream.read()).value).toString(), 'first');
    await request('/continue');
    assert.equal(Buffer.from((await stream.read()).value).toString(), 'second');
    await stream.cancel();
    await new Promise((accept, reject) => {
      const socket = new WebSocket('ws://' + address + '/listen', { headers: { Host: 'api.fixture.invalid' } });
      const timeout = setTimeout(() => { socket.terminate(); reject(new Error('gateway WebSocket deadline')); }, 10000);
      socket.once('error', reject);
      socket.once('message', value => { clearTimeout(timeout); socket.terminate(); value.toString() === 'connected' ? accept() : reject(new Error('gateway frame changed')); });
    });
    const oldIp = command(['docker', 'inspect', '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', first]);
    remove(first);
    // Occupy the released address, forcing backend DNS to point at another IP.
    run('--network', network, '--ip', oldIp, image, 'sleep', '120');
    const replacement = peer('replacement');
    assert.notEqual(command(['docker', 'inspect', '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', replacement]), oldIp);
    const recoveryDeadline = Date.now() + 20000;
    for (;;) {
      try {
        const response = await request('/ready');
        if (response.status === 200 && (await response.json()).generation === 'replacement') break;
      } catch {}
      assert.ok(Date.now() < recoveryDeadline, 'gateway must resolve recreated backend without a restart');
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    console.log('PASS deployed nginx: public hosts, internal denial, incremental stream, WebSocket and recreated backend DNS');
  } catch (error) {
    for (const id of containers) process.stderr.write(command(['docker','logs',id]) + '\n');
    throw error;
  } finally {
    for (const id of containers) spawnSync('docker', ['rm', '--force', id], { stdio: 'ignore' });
    command(['docker', 'network', 'rm', network]);
  }
}
