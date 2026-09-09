#!/usr/bin/env bash
# LIFECYCLE: permanent
# The shared build boundary runs identically in local and fork CI lanes.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
if [ "$(bun --version)" != "1.3.14" ]; then
  echo 'Web build checks require Bun 1.3.14 (the upstream Docker runtime).'
  exit 1
fi
(cd web/app && bun install --frozen-lockfile)
npm ci --prefix scripts/brand/raster --ignore-scripts --no-audit --no-fund
web/app/node_modules/.bin/tsc --project deploy/web/tsconfig.json
bun test deploy/web/build.test.ts
