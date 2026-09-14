#!/usr/bin/env bash
# Compatibility wrapper — the local stack now lives in dev/local.sh.
#
# Old invocations keep working:
#   dev/deploy-local.sh              → dev/local.sh up
#   dev/deploy-local.sh --no-backend → dev/local.sh up --no-backend
#   dev/deploy-local.sh --stop       → dev/local.sh down
#
# The one behavioural change: the backend now runs as a supervised background
# process with a pid file and a log (dev/local.sh logs backend) instead of
# blocking this shell with uvicorn in the foreground.

set -euo pipefail
printf 'note: dev/deploy-local.sh is now dev/local.sh (same stack, one lifecycle CLI)\n' >&2
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/local.sh" "$@"
