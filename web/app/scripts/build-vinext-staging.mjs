#!/usr/bin/env node

import { spawnSync } from 'node:child_process';

// Keep a staging build from silently embedding the production API fallback.
// Firebase credentials remain deployment inputs; these two public endpoints are
// intentionally fixed to the isolated Edge Worker for this branch.
const stagingEnv = {
  ...process.env,
  NEXT_PUBLIC_API_BASE_URL: 'https://omi-cf-edge-staging.summersmile1984.workers.dev',
  NEXT_PUBLIC_WS_BASE_URL: 'wss://omi-cf-edge-staging.summersmile1984.workers.dev',
  NEXT_PUBLIC_AUTH_MODE: 'better-auth',
  NEXT_PUBLIC_AUTH_SERVER_URL: 'https://omi-cf-auth-staging.summersmile1984.workers.dev',
  VINEXT_BUILD: '1',
};

const result = spawnSync('vinext', ['build'], {
  env: stagingEnv,
  stdio: 'inherit',
});

if (result.error) {
  console.error(`Unable to start vinext build: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status ?? 1);
