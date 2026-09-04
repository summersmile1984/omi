#!/usr/bin/env bash
# LIFECYCLE: permanent
# One local/CI route qualification lane; no Cloudflare credentials or remote D1.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
# Reuse make setup-backend's pinned interpreter in CI, whose shell Python may
# differ from backend/.python-version. The hermetic runner owns its own venv.
if [ -x "$ROOT/backend/.venv/bin/python" ]; then
  export OPENAPI_RUNNER_PYTHON="${OPENAPI_RUNNER_PYTHON:-$ROOT/backend/.venv/bin/python}"
fi
cd "$ROOT/deploy/cloudflare"
node -e 'if (Number(process.versions.node.split(".")[0]) < 22) { throw new Error("Cloudflare checks require Node >=22"); }'
if [ ! -d node_modules ]; then
  npm ci --no-audit --no-fund
fi
npm run test:route-inventory
npm run validate:backend-routes
npm run typecheck
npm test
cd python/api-core
uvx uv==0.12.3 run pytest -q
