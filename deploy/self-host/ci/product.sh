#!/usr/bin/env bash
# LIFECYCLE: permanent
# Same bounded product slice on a local Docker engine and the existing CI lane.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
REPORT="$(mktemp -d "${TMPDIR:-/tmp}/memweft-product-contract.XXXXXX")"
args=(--output "$REPORT/server" --brand-id core-server-fixture --port "${SELF_HOST_CI_PORT:-34800}" --self-test)
if [ -n "${SELF_HOST_CI_RUNTIME_IMAGE:-}" ]; then
  args+=(--runtime-image "$SELF_HOST_CI_RUNTIME_IMAGE")
fi
cd "$ROOT"
"${PYTHON:-python3}" deploy/self-host/ci/test_product.py
exec "${PYTHON:-python3}" deploy/self-host/ci/product.py "${args[@]}"
