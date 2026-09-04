#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"
for component in auth-server deploy/cloudflare; do
  if [ ! -d "$component/node_modules" ]; then
    echo "Install locked dependencies first: (cd $component && npm ci --ignore-scripts)" >&2
    exit 1
  fi
done
npm --prefix auth-server test
(cd deploy/cloudflare && npm run typecheck && ./node_modules/.bin/vitest run tests/auth.test.ts tests/auth-mcp-oauth.test.ts)
selection="$(mktemp)"
trap 'rm -f "$selection"' EXIT
printf '%s\n' fork/tests/test_auth_contract.py tests/unit/test_auth_shim.py > "$selection"
PYTHON="$root/backend/.venv/bin/python" BACKEND_UNIT_TEST_FILE_LIST="$selection" bash backend/test.sh
