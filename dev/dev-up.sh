#!/usr/bin/env bash
# One-shot cloud-neutral dev stack: three Firebase emulators + PostgreSQL +
# backend on the firestore_pg shim.
#
# Usage:
#   dev/dev-up.sh            # start emulators+PG (docker), then run backend in fg
#   dev/dev-up.sh --no-backend   # infra only
#   dev/dev-up.sh --stop     # stop the backend and bring the stack down
#
# The backend runs with FIRESTORE_PG_DSN set, so database/*.py resolves to the
# shim and writes land in Postgres. Requires: docker, a backend/.venv (3.11),
# and ENCRYPTION_SECRET in the environment (dev value documented below).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
DEV_DIR="$REPO_ROOT/dev"
COMPOSE_FILE="$DEV_DIR/docker-compose.dev.yml"
PORT="${BACKEND_PORT:-8100}"
LOG="${BACKEND_LOG:-/tmp/backend-dev.log}"

# Dev-only secret (32-byte base64). Never use in prod.
ENCRYPTION_SECRET="${ENCRYPTION_SECRET:-YdtqMcLQx1E9r65YS2lRB9eycbcr76RWmizKq8UecTA=}"
export ENCRYPTION_SECRET

if [[ "${1:-}" == "--stop" ]]; then
  pkill -f "uvicorn main:app --host 127.0.0.1 --port $PORT" 2>/dev/null || true
  docker compose -f "$COMPOSE_FILE" down 2>/dev/null || true
  echo "stack stopped"
  exit 0
fi

echo "==> starting emulators + postgres"
# An emulator is already bound to 8080/9099/9199 (e.g. a manually started
# container or an earlier compose run) — reuse it instead of racing compose.
if curl -s -o /dev/null --max-time 2 http://localhost:8080/; then
  echo "    firestore emulator already listening on 8080; reusing it"
  docker compose -f "$COMPOSE_FILE" up -d postgres redis
else
  docker compose -f "$COMPOSE_FILE" up -d
fi

echo "==> waiting for postgres on 127.0.0.1:5434"
for _ in $(seq 1 30); do
  if docker exec omi-postgres pg_isready -U omi -h 127.0.0.1 >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec omi-postgres pg_isready -U omi -h 127.0.0.1 >/dev/null 2>&1 || {
  echo "postgres did not become ready"; exit 1
}

if [[ "${1:-}" == "--no-backend" ]]; then
  echo "infra up: emulators 8080/9099/9199, postgres 127.0.0.1:5434"
  exit 0
fi

echo "==> starting backend on :$PORT (log: $LOG)"
cd "$BACKEND_DIR"
export FIRESTORE_PG_DSN="postgresql+psycopg://omi:omi-dev-password@localhost:5434/omi"
export FIRESTORE_EMULATOR_HOST=localhost:8080
export FIREBASE_AUTH_EMULATOR_HOST=localhost:9099
export STORAGE_EMULATOR_HOST=localhost:9199
export FIREBASE_PROJECT_ID=demo-omi-local
export OMI_ENV_STAGE=offline

exec .venv/bin/uvicorn main:app --host 127.0.0.1 --port "$PORT"
