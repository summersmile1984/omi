#!/usr/bin/env bash
# Run the Server OS (self-hosted) backend locally from the checkout.
#
# main's self-hosted runtime is normally an image: `deploy/self-host/Dockerfile`
# renders the `self_hosted.<stage>` profile with `scripts/profiles/render.py` and
# bakes in the model stores. This script reproduces that boot from a checkout so
# the product surface can be exercised without an amd64 image build:
#
#   1. render the `self_hosted.local` profile table (core-only: the shape
#      `deploy/self-host/ci/product.py::core_only_profile` uses, so no speech or
#      LLM model stores are required)
#   2. run the Qdrant collection migration the runtime refuses to start without
#   3. start `uvicorn fork.main:app` with an explicit env file and wait for health
#
#   dev/selfhost-local.sh up      start it (data plane must already be up)
#   dev/selfhost-local.sh stop    stop it and restore the committed profile table
#   dev/selfhost-local.sh status  what is running
#
# Prerequisites: `dev/local.sh up` (postgres/redis/minio/qdrant/typesense).
# Env: copy dev/selfhost-local.env.example to dev/selfhost-local.env.

set -euo pipefail

_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$_REPO_ROOT"

BACKEND_DIR="$_REPO_ROOT/backend"
STATE_DIR="${OMI_SELFHOST_STATE_DIR:-$_REPO_ROOT/.local/selfhost}"
LOG="$STATE_DIR/backend.log"
PID_FILE="$STATE_DIR/backend.pid"
TABLE="backend/fork/deployment_profiles.generated.json"
ENV_EXAMPLE="$_REPO_ROOT/dev/selfhost-local.env.example"
ENV_FILE="${OMI_SELFHOST_ENV_FILE:-$_REPO_ROOT/dev/selfhost-local.env}"
PORT="${OMI_SELFHOST_PORT:-8100}"
PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"

log() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

mkdir -p "$STATE_DIR"

if [ -f "$ENV_FILE" ]; then
  _env_source="$ENV_FILE"
else
  _env_source="$ENV_EXAMPLE"
fi
[ -f "$_env_source" ] || die "missing $ENV_EXAMPLE"

# shellcheck disable=SC1090
set -a
. "$_env_source"
set +a

pid_alive() {
  [ -f "$PID_FILE" ] || return 1
  kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

restore_table() {
  if git -C "$_REPO_ROOT" diff --quiet -- "$TABLE" 2>/dev/null; then
    return 0
  fi
  git -C "$_REPO_ROOT" checkout -- "$TABLE" 2>/dev/null &&
    log "    committed profile table restored" ||
    log "    warning: restore $TABLE by hand (git checkout -- $TABLE)"
}

cmd_up() {
  pid_alive && { log "already running (pid $(cat "$PID_FILE"))"; return 0; }

  log "==> [1/3] containers required by the profile"
  for name in omi-postgres omi-redis omi-minio omi-qdrant omi-typesense; do
    docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null | grep -q true ||
      die "$name is not running — start the data plane with 'dev/local.sh up'"
  done
  log "    postgres, redis, minio, qdrant, typesense: up"

  log "==> [2/3] core-only self_hosted.local profile + qdrant migration"
  "$PYTHON_BIN" scripts/profiles/render.py --target self_hosted \
    --manifest brand/omi-upstream/manifest.yaml --stage local --emit-json 2>/dev/null |
    "$PYTHON_BIN" -c '
import copy, json, sys
table = json.load(sys.stdin)
row = copy.deepcopy(table["profiles"]["self_hosted.local"])
# The exact core-only shape deploy/self-host/ci/product.py uses: no speech/LLM
# model stores, so the checkout can admit the profile without them.
row.pop("speech", None)
row.pop("llm", None)
row["capabilities"].update(stt_providers=[], tts_provider="disabled", llm_provider="disabled")
table["profiles"]["self_hosted.local"] = row
json.dump(table, sys.stdout, indent=1)
' >"$TABLE" || die "rendering the profile table failed"
  log "    $TABLE rendered (self_hosted.local, core-only)"

  (cd "$BACKEND_DIR" && "$PYTHON_BIN" -m fork.vector_qdrant migrate) ||
    die "qdrant migration failed"

  log "==> [3/3] backend (uvicorn fork.main:app on :$PORT)"
  (cd "$BACKEND_DIR" && nohup "$PYTHON_BIN" -m uvicorn fork.main:app \
    --host 127.0.0.1 --port "$PORT" >"$LOG" 2>&1 & echo $! >"$PID_FILE")

  for _ in $(seq 1 90); do
    if curl -sf -m 3 "http://127.0.0.1:$PORT/v1/health" >/dev/null 2>&1; then
      log ""
      log "self-hosted backend is up: http://127.0.0.1:$PORT/v1/health (log: $LOG)"
      log "auth: sign up at the auth-server, then mint a JWT via /auth-issue"
      return 0
    fi
    pid_alive || { tail -20 "$LOG" >&2; die "backend exited — see $LOG"; }
    sleep 1
  done
  tail -20 "$LOG" >&2
  die "backend did not become healthy"
}

cmd_stop() {
  if pid_alive; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    sleep 2
    kill -9 "$(cat "$PID_FILE")" 2>/dev/null || true
    log "backend stopped"
  fi
  rm -f "$PID_FILE"
  restore_table
}

cmd_status() {
  if pid_alive; then
    log "backend: running (pid $(cat "$PID_FILE"))"
    curl -s -m 3 "http://127.0.0.1:$PORT/v1/health" -w ' HTTP %{http_code}\n' || true
  else
    log "backend: stopped"
  fi
  if git -C "$_REPO_ROOT" diff --quiet -- "$TABLE" 2>/dev/null; then
    log "profile table: committed version (no local render)"
  else
    log "profile table: locally rendered (dev/selfhost-local.sh stop restores it)"
  fi
}

case "${1:-up}" in
  up) cmd_up ;;
  stop | down) cmd_stop ;;
  status) cmd_status ;;
  *) die "unknown command '$1' (up | stop | status)" ;;
esac
