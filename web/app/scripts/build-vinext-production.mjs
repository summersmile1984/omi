#!/usr/bin/env node

import { spawnSync } from 'node:child_process';

const subdomain = (process.env.CLOUDFLARE_WORKERS_SUBDOMAIN || 'summersmile1984').trim().toLowerCase();
if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(subdomain)) {
  console.error('Invalid CLOUDFLARE_WORKERS_SUBDOMAIN');
  process.exit(1);
}

const edgeHost = `omi-cf-edge-production.${subdomain}.workers.dev`;
const productionEnv = {
  ...process.env,
  NEXT_PUBLIC_API_BASE_URL: `https://${edgeHost}`,
  NEXT_PUBLIC_WS_BASE_URL: `wss://${edgeHost}`,
  NEXT_PUBLIC_AUTH_MODE: 'better-auth',
  NEXT_PUBLIC_AUTH_SERVER_URL: `https://omi-cf-auth-production.${subdomain}.workers.dev`,
  VINEXT_BUILD: '1',
};

const result = spawnSync('vinext', ['build'], {
  env: productionEnv,
  stdio: 'inherit',
});

if (result.error) {
  console.error(`Unable to start vinext production build: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status ?? 1);
