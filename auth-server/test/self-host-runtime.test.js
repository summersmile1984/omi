import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { copyFileSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { selectRuntime } from '../../deploy/self-host/auth-runtime.mjs';

const source = new URL('../../deploy/self-host/auth-runtime.mjs', import.meta.url);

test('stage selects security mode before either real entrypoint imports', () => {
  const root = mkdtempSync(join(tmpdir(), 'auth-stage-'));
  try {
    copyFileSync(source, join(root, 'self-host-runtime.mjs'));
    mkdirSync(join(root, 'src'));
    writeFileSync(join(root, 'package.json'), '{"type":"module"}');
    for (const entry of ['index.js', 'migrate.js']) {
      writeFileSync(join(root, 'src', entry), 'console.log(JSON.stringify({mode:process.env.NODE_ENV,args:process.argv.slice(2)}));');
    }
    for (const stage of ['local', 'beta', 'production']) {
      for (const args of [['serve'], ['migrate'], ['migrate', '--check']]) {
        const result = spawnSync(process.execPath, [join(root, 'self-host-runtime.mjs'), ...args], {
          env: { ...process.env, SELF_HOST_STAGE: stage, NODE_ENV: stage === 'local' ? 'production' : 'development' },
          encoding: 'utf8',
        });
        assert.equal(result.status, 0, result.stderr);
        assert.deepEqual(JSON.parse(result.stdout), {
          mode: stage === 'local' ? 'development' : 'production', args: args.slice(1),
        });
      }
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('invalid stage/command fail before application import; default stays production', () => {
  assert.equal(selectRuntime().nodeEnv, 'production');
  for (const [stage, args] of [['prod', ['serve']], ['', ['serve']], ['local', ['wrong']], ['local', ['serve', '--check']]]) {
    const result = spawnSync(process.execPath, [source.pathname, ...args], {
      env: { ...process.env, SELF_HOST_STAGE: stage }, encoding: 'utf8',
    });
    assert.notEqual(result.status, 0);
    assert.doesNotMatch(result.stderr, /ERR_MODULE_NOT_FOUND/);
  }
});
