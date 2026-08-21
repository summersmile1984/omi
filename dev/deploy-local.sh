#!/usr/bin/env bash
# One-shot self-hosted local deployment for the cloud-neutral Omi fork.
#
# Brings up every shim on this machine (no cloud, no GPU) and starts the
# backend pointed at them. Enables the full 4C8G deployment profile locally:
#
#   containers   : PostgreSQL (firestore_pg shim), Redis (queue+cache),
#                  MinIO (object storage), Firebase emulators (dev auth)
#   local service: auth-server (Better Auth, port 3000) + Redis queue worker
#   backend      : uvicorn on :8100 with every shim env set
#
# Usage:
#   dev/deploy-local.sh            # infra + auth-server + backend (fg)
#   dev/deploy-local.sh --no-backend   # infra + auth-server only
#   dev/deploy-local.sh --stop     # teardown backend + auth-server + containers
#
# Env overrides: BACKEND_PORT, MOSS_API_KEY, AUTH_PROVIDER, TRANSLATION_PROVIDER.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
DEV_DIR="$REPO_ROOT/dev"
COMPOSE_FILE="$DEV_DIR/docker-compose.dev.yml"
PORT="${BACKEND_PORT:-8100}"
LOG="${BACKEND_LOG:-/tmp/backend-dev.log}"
AUTH_PORT="${AUTH_PORT:-3000}"

# Dev-only secret (32-byte base64). Never use in prod.
ENCRYPTION_SECRET="${ENCRYPTION_SECRET:-YdtqMcLQx1E9r65YS2lRB9eycbcr76RWmizKq8UecTA=}"
export ENCRYPTION_SECRET

_stop_backend() {
  pkill -f "uvicorn main:app --host 127.0.0.1 --port $PORT" 2>/dev/null || true
  pkill -f "node src/index.js" 2>/dev/null || true
  pkill -f "cloud_tasks_redis.*--worker" 2>/dev/null || true
  pkill -f "cloud_tasks_redis.*--all" 2>/dev/null || true
}

if [[ "${1:-}" == "--stop" ]]; then
  _stop_backend
  docker compose -f "$COMPOSE_FILE" down 2>/dev/null || true
  echo "stack stopped"
  exit 0
fi

echo "==> [1/4] containers (postgres / redis / minio / emulators)"
# Reuse an already-bound emulator instead of racing compose.
if curl -s -o /dev/null --max-time 2 http://localhost:8080/; then
  echo "    emulator already on 8080; starting infra only"
  docker compose -f "$COMPOSE_FILE" up -d postgres redis minio
else
  docker compose -f "$COMPOSE_FILE" up -d
fi

echo "==> [2/4] waiting for postgres + minio"
for _ in $(seq 1 30); do
  docker exec omi-postgres pg_isready -U omi -h 127.0.0.1 >/dev/null 2>&1 && break
  sleep 1
done
for _ in $(seq 1 30); do
  curl -s --max-time 2 http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1 && break
  sleep 1
done
docker exec omi-postgres pg_isready -U omi -h 127.0.0.1 >/dev/null 2>&1 || { echo "postgres not ready"; exit 1; }

echo "==> [3/4] auth-server (Better Auth) + Redis queue worker"
# Ensure the Better Auth PG schema exists.
if ! docker exec omi-postgres psql -U omi -d omi -tAc "SELECT 1 FROM information_schema.tables WHERE table_name='user'" | grep -q 1; then
  echo "    generating + applying Better Auth schema"
  (cd "$REPO_ROOT/auth-server" && npx auth@latest generate --adapter kysely --dialect postgresql --output /tmp/auth-schema.sql -y >/dev/null 2>&1 || true)
  docker exec -i omi-postgres psql -U omi -d omi < /tmp/auth-schema.sql >/dev/null 2>&1 || true
fi

if [[ "${1:-}" == "--no-backend" ]]; then
  _stop_backend  # clear stale, but we skip starting backend
  echo "infra up: postgres 5434 / redis 6379 / minio 9000 / emulators 8080-9199"
  echo "auth-server NOT started (use --no-backend + manual start to control it)"
  exit 0
fi

# Start auth-server (if not already running)
if ! curl -s --max-time 2 http://127.0.0.1:$AUTH_PORT/health >/dev/null 2>&1; then
  # First deploy: ensure the JWKS table is empty so Better Auth re-generates
  # the signing key with the current BETTER_AUTH_SECRET (a leftover key
  # encrypted with an old secret breaks /auth-issue).
  if docker exec omi-postgres psql -U omi -d omi -tAc "SELECT count(*) FROM jwks" 2>/dev/null | grep -q '^0$'; then
    : # fresh or already empty
  else
    echo "    resetting Better Auth JWKS (secret may have changed)"
    docker exec omi-postgres psql -U omi -d omi -c "TRUNCATE jwks" >/dev/null 2>&1 || true
  fi
  (cd "$REPO_ROOT/auth-server" && PORT=$AUTH_PORT \
    DATABASE_URL="postgresql://omi:omi-dev-password@localhost:5434/omi" \
    BETTER_AUTH_SECRET="${BETTER_AUTH_SECRET:-dev-only-better-auth-secret-change-me-32bytes-min}" \
    BETTER_AUTH_URL="http://127.0.0.1:$AUTH_PORT" \
    nohup node src/index.js > /tmp/auth-server.log 2>&1 &)
  echo "    auth-server started on :$AUTH_PORT"
fi

# Start Redis queue worker for all queues
if ! pgrep -f "cloud_tasks_redis.*--all" >/dev/null 2>&1; then
  (cd "$BACKEND_DIR" && QUEUE_BACKEND=redis REDIS_DB_HOST=127.0.0.1 \
    SYNC_TASKS_HANDLER_URL="http://127.0.0.1:$PORT/v2/sync-jobs/run" \
    AUDIO_MERGE_HANDLER_URL="http://127.0.0.1:$PORT/v2/audio-merge-jobs/run" \
    ACCOUNT_DELETION_HANDLER_URL="http://127.0.0.1:$PORT/v1/users/account-deletion-wipes/run" \
    LISTEN_FINALIZATION_HANDLER_URL="http://127.0.0.1:$PORT/v1/conversation-finalization-jobs/run" \
    nohup .venv/bin/python -m utils.cloud_tasks_redis --all > /tmp/queue-worker.log 2>&1 &)
  echo "    queue worker started (all queues)"
fi

echo "==> [4/4] backend on :$PORT (log: $LOG)"
cd "$BACKEND_DIR"
export FIRESTORE_PG_DSN="postgresql+psycopg://omi:omi-dev-password@localhost:5434/omi"
export FIRESTORE_EMULATOR_HOST=localhost:8080
export FIREBASE_AUTH_EMULATOR_HOST=localhost:9099
export STORAGE_EMULATOR_HOST=localhost:9199
export FIREBASE_PROJECT_ID=demo-omi-local
export OMI_ENV_STAGE=offline
export RATE_LIMIT_SHADOW_MODE=true
# Better Auth (auth-server) — JWT issuance + JWKS verification
export AUTH_PROVIDER="${AUTH_PROVIDER:-better_auth}"
export AUTH_JWKS_URL="http://127.0.0.1:${AUTH_PORT}/api/auth/jwks"
# Cloud-neutral shims
export STORAGE_BACKEND=minio
export MINIO_ENDPOINT="http://127.0.0.1:9000"
# MinIO buckets (storage.py reads BUCKET_*; storage_minio auto-creates them)
export BUCKET_SPEECH_PROFILES="${BUCKET_SPEECH_PROFILES:-omi-speech-profiles}"
export BUCKET_POSTPROCESSING="${BUCKET_POSTPROCESSING:-omi-postprocessing}"
export BUCKET_MEMORIES_RECORDINGS="${BUCKET_MEMORIES_RECORDINGS:-omi-memories-recordings}"
export BUCKET_PRIVATE_CLOUD_SYNC="${BUCKET_PRIVATE_CLOUD_SYNC:-omi-private-cloud-sync}"
export BUCKET_TEMPORAL_SYNC_LOCAL="${BUCKET_TEMPORAL_SYNC_LOCAL:-omi-temporal-sync-local}"
export BUCKET_PLUGINS_LOGOS="${BUCKET_PLUGINS_LOGOS:-omi-plugins-logos}"
export BUCKET_APP_THUMBNAILS="${BUCKET_APP_THUMBNAILS:-omi-app-thumbnails}"
export BUCKET_CHAT_FILES="${BUCKET_CHAT_FILES:-omi-chat-files}"
export BUCKET_DESKTOP_UPDATES="${BUCKET_DESKTOP_UPDATES:-omi-desktop-updates}"
export QUEUE_BACKEND=redis
export REDIS_DB_HOST=127.0.0.1
export REDIS_DB_PORT=6379
# Live STT provider. Default: local CPU SenseVoice (off-cloud). To use
# MiMo-V2.5-ASR instead, set STT_SERVICE_MODELS=mimo + MIMO_API_KEY +
# an explicit operator-owned MIMO_API_BASE (or MIMO_TOKENPLAN_BASE).
export SENSEVOICE_MODEL_DIR="${SENSEVOICE_MODEL_DIR:-/tmp/sherpa/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17}"
export STT_SERVICE_MODELS="${STT_SERVICE_MODELS:-sensevoice}"
# Pre-recorded batch STT provider (opt-in override, else upstream policy).
#   STT_PRERECORDED_MODEL=moss + MOSS_API_KEY  → OpenMOSS (ASR)
#   STT_PRERECORDED_MODEL=parakeet|modulate-velma-2 → upstream GPU/hosted
if [[ -n "${STT_PRERECORDED_MODEL:-}" ]]; then
  export STT_PRERECORDED_MODEL
fi
if [[ -n "${MIMO_API_KEY:-}" ]]; then
  export MIMO_API_KEY
fi
if [[ -n "${MIMO_API_BASE:-}" ]]; then
  export MIMO_API_BASE
fi
if [[ -n "${MIMO_TOKENPLAN_BASE:-}" ]]; then
  export MIMO_TOKENPLAN_BASE
fi
if [[ -n "${MOSS_API_KEY:-}" ]]; then
  export MOSS_API_KEY
fi
# TTS provider: default ElevenLabs (needs ELEVENLABS_API_KEY). Set
# TTS_PROVIDER=mimo to use MiMo-V2.5-TTS (needs MIMO_API_KEY and an explicit
# MIMO_API_BASE or MIMO_TOKENPLAN_BASE).
if [[ -n "${TTS_PROVIDER:-}" ]]; then
  export TTS_PROVIDER
fi
if [[ -n "${MIMO_USE_TOKENPLAN:-}" ]]; then
  export MIMO_USE_TOKENPLAN
fi
if [[ -n "${MIMO_TTS_VOICE:-}" ]]; then
  export MIMO_TTS_VOICE
fi
# Translation provider (default gemini; set TRANSLATION_PROVIDER=mimo|deepseek)
if [[ -n "${TRANSLATION_PROVIDER:-}" ]]; then
  export TRANSLATION_PROVIDER
fi

exec .venv/bin/uvicorn main:app --host 127.0.0.1 --port "$PORT"
