#!/usr/bin/env bash
# LIFECYCLE: permanent
# Same isolated actual application target and public cases in local and CI lanes.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
REPORT="$(mktemp -d "${TMPDIR:-/tmp}/memweft-cloudflare-product.XXXXXX")"
printf 'Cloudflare product evidence: %s\n' "$REPORT/target"
cd "$ROOT"
exec node deploy/cloudflare/contracts/local-target.mjs \
  --output "$REPORT/target" --brand-id core-cf-fixture --run-core --run-recording --run-chat --run-share
