#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x node_modules/.bin/vitest ]; then
  echo 'Install the locked Web dependencies: cd web/app && bun install --frozen-lockfile' >&2
  exit 1
fi
bun test fork/tests/auth-session.test.ts
./node_modules/.bin/vitest run --config fork/vitest.config.mts
