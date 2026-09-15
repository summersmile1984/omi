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
# Cheap and early: a projection that references a name nothing stages cannot run at all,
# and the suites below only catch it on the paths they happen to exercise.
"${OPENAPI_RUNNER_PYTHON:-python3}" scripts/check_projection_names.py
cd python/api-core
# This suite is the lane's slow half -- 17:45 of the 28-minute CI gate, measured on
# run 34963800926 -- and it is file-isolated: each test file stages its own modules in
# its own temporary directory. `--dist loadfile` keeps a file's tests in one worker, so
# the sharding is safe where a shared cross-file fixture would not be. 1299 tests take
# 2:50 sharded versus 10:34 single-process on the same developer machine.
uvx uv==0.12.3 run pytest -q -n auto --dist loadfile

cd ../api-ai
uvx uv==0.12.3 run pytest -q
