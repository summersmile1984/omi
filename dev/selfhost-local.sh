#!/usr/bin/env bash
# App-only entry for the same instance/configuration that dev/local.sh owns.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -n "${OMI_SELFHOST_ENV_FILE:-}${OMI_SELFHOST_STATE_DIR:-}${OMI_SELFHOST_PORT:-}" ]; then
  printf '%s\n' 'error: use OMI_LOCAL_ENV_FILE / OMI_LOCAL_STATE_DIR / OMI_LOCAL_BACKEND_PORT; separate selfhost environment is no longer supported' >&2
  exit 1
fi
command="${1:-up}"; shift || true
case "$command" in
  up) exec bash "$ROOT/dev/local.sh" backend-up "$@" ;;
  stop|down) exec bash "$ROOT/dev/local.sh" backend-stop "$@" ;;
  status) exec bash "$ROOT/dev/local.sh" backend-status "$@" ;;
  restart) exec bash "$ROOT/dev/local.sh" backend-restart "$@" ;;
  *) printf 'error: unknown command %s (up | stop | status | restart)\n' "$command" >&2; exit 1 ;;
esac
