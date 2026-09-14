#!/usr/bin/env bash
# Omi — local development stack (stage 1 of the pipeline: compile + run).
#
# One entry point for the cloud-neutral local stack: the data plane runs in
# containers, while auth-server, the queue worker and the backend run as
# supervised local processes. No cloud account, no cloud credentials, no GPU.
#
#   dev/local.sh up            bring up the local data plane and wait for health
#   dev/local.sh status        what is running, on which ports, healthy or not
#   dev/local.sh verify        end-to-end proof (PG / Redis / MinIO / auth / API)
#   dev/local.sh restart       restart the application processes (keeps data)
#   dev/local.sh logs [svc]    tail stack logs (backend, auth-server, queue-worker)
#   dev/local.sh ports         resolved port allocation + collision report
#   dev/local.sh env           print `export ...` lines for the current shell
#   dev/local.sh down          stop processes and containers (keeps volumes)
#   dev/local.sh reset         down + delete volumes (destructive, re-creatable)
#
# Configuration lives in dev/local.env (copy dev/local.env.example). Precedence:
#   ambient environment  >  dev/local.env  >  dev/local.env.example
#
# Ports are configurable on purpose: a machine may already run another project's
# Postgres or MinIO, and local dev must never require stopping it.

set -euo pipefail

_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$_REPO_ROOT"

DEV_DIR="$_REPO_ROOT/dev"
COMPOSE_FILE="$DEV_DIR/docker-compose.dev.yml"
# OMI_LOCAL_ENV_FILE is a testing/CI seam: it lets a caller point the stack at an
# alternative config file without writing into the checkout.
ENV_FILE="${OMI_LOCAL_ENV_FILE:-$DEV_DIR/local.env}"
ENV_EXAMPLE="$DEV_DIR/local.env.example"

STATE_DIR="${OMI_LOCAL_STATE_DIR:-$_REPO_ROOT/.local/local-dev}"
LOG_DIR="$STATE_DIR/logs"
PID_DIR="$STATE_DIR/pids"
EVIDENCE_DIR="$STATE_DIR/evidence"
STATE_ENV="$STATE_DIR/state.env"
CHILD_ENV_FILE="$STATE_DIR/child.env"

BACKEND_DIR="$_REPO_ROOT/backend"
AUTH_DIR="$_REPO_ROOT/auth-server"

# ---------------------------------------------------------------- configuration
# Layering, strongest first:
#   ambient environment  >  dev/local.env  >  dev/local.env.example
# The example supplies every default, dev/local.env overrides what this machine
# needs, and anything already exported in the caller's shell wins over both.
_CONFIG_KEYS="OMI_LOCAL_POSTGRES_PORT OMI_LOCAL_REDIS_PORT OMI_LOCAL_MINIO_API_PORT \
OMI_LOCAL_MINIO_CONSOLE_PORT OMI_LOCAL_FIRESTORE_PORT OMI_LOCAL_FIREBASE_AUTH_PORT \
OMI_LOCAL_FIREBASE_STORAGE_PORT OMI_LOCAL_AUTH_PORT OMI_LOCAL_BACKEND_PORT \
OMI_LOCAL_BETTER_AUTH_SECRET OMI_LOCAL_AUTH_DEV_ISSUER_SECRET \
OMI_LOCAL_QUEUE_WORKER_SECRET OMI_LOCAL_ENCRYPTION_SECRET OMI_LOCAL_REDIS_PASSWORD"

[ -f "$ENV_EXAMPLE" ] || {
  echo "missing $ENV_EXAMPLE — it carries the local stack defaults" >&2
  exit 1
}

mkdir -p "$STATE_DIR"
_ambient_file="$STATE_DIR/ambient.env"
: >"$_ambient_file"
for _key in $_CONFIG_KEYS; do
  eval "_value=\${$_key:-}"
  if [ -n "$_value" ]; then
    printf "%s='%s'\n" "$_key" "$(printf '%s' "$_value" | sed "s/'/'\\\\''/g")" >>"$_ambient_file"
  fi
done

# shellcheck disable=SC1090
. "$ENV_EXAMPLE"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  _config_source="$ENV_FILE"
else
  _config_source="$ENV_EXAMPLE"
fi
if [ -s "$_ambient_file" ]; then
  # shellcheck disable=SC1090
  . "$_ambient_file"
fi
rm -f "$_ambient_file"

for _key in $_CONFIG_KEYS; do
  eval "_value=\${$_key:-}"
  [ -n "$_value" ] || {
    echo "configuration error: $_key is empty (check $_config_source)" >&2
    exit 1
  }
done

# Names handed to docker compose (container-side ports never change).
export DEV_POSTGRES_PORT="$OMI_LOCAL_POSTGRES_PORT"
export DEV_REDIS_PORT="$OMI_LOCAL_REDIS_PORT"
export DEV_MINIO_API_PORT="$OMI_LOCAL_MINIO_API_PORT"
export DEV_MINIO_CONSOLE_PORT="$OMI_LOCAL_MINIO_CONSOLE_PORT"
export DEV_REDIS_PASSWORD="$OMI_LOCAL_REDIS_PASSWORD"
export FIRESTORE_EMULATOR_PORT="$OMI_LOCAL_FIRESTORE_PORT"
export FIREBASE_AUTH_EMULATOR_PORT="$OMI_LOCAL_FIREBASE_AUTH_PORT"
export FIREBASE_STORAGE_EMULATOR_PORT="$OMI_LOCAL_FIREBASE_STORAGE_PORT"

POSTGRES_CONTAINER="${DEV_POSTGRES_CONTAINER_NAME:-omi-postgres}"
REDIS_CONTAINER="${DEV_REDIS_CONTAINER_NAME:-omi-redis}"
MINIO_CONTAINER="${DEV_MINIO_CONTAINER_NAME:-omi-minio}"
EMULATOR_CONTAINER="${DEV_FIREBASE_CONTAINER_NAME:-omi-emulators}"

POSTGRES_USER="${DEV_POSTGRES_USER:-omi}"
POSTGRES_PASSWORD="${DEV_POSTGRES_PASSWORD:-omi-dev-password}"
POSTGRES_DB="${DEV_POSTGRES_DB:-omi}"
MINIO_ROOT_USER="${DEV_MINIO_ROOT_USER:-minioadmin}"
MINIO_ROOT_PASSWORD="${DEV_MINIO_ROOT_PASSWORD:-minioadmin}"

PG_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${OMI_LOCAL_POSTGRES_PORT}/${POSTGRES_DB}"
PG_DSN_SQLALCHEMY="postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${OMI_LOCAL_POSTGRES_PORT}/${POSTGRES_DB}"
MINIO_ENDPOINT="http://127.0.0.1:${OMI_LOCAL_MINIO_API_PORT}"
# Public object origin seen by clients: same server, addressable from the host.
MINIO_PUBLIC_ENDPOINT="$MINIO_ENDPOINT"
AUTH_URL="http://127.0.0.1:${OMI_LOCAL_AUTH_PORT}"
BACKEND_URL="http://127.0.0.1:${OMI_LOCAL_BACKEND_PORT}"
OMI_LOCAL_SHARE_PORT="${OMI_LOCAL_SHARE_PORT:-3001}"

PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"

# ---------------------------------------------------------------------- helpers
log() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

mkdir -p "$LOG_DIR" "$PID_DIR" "$EVIDENCE_DIR"

port_busy() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null || return 1
  return 0
}

port_owner() {
  local owner
  owner="$(lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1}')"
  printf '%s' "${owner:-unknown}"
}

# A port is "ours" when one of THIS stack's containers publishes it. Another
# project's container on the same port is a collision, not a running stack.
port_is_ours() {
  local port="$1" name
  for name in "$POSTGRES_CONTAINER" "$REDIS_CONTAINER" "$MINIO_CONTAINER" "$EMULATOR_CONTAINER"; do
    if docker ps --filter "name=^${name}$" --format '{{.Ports}}' 2>/dev/null | grep -q ":$port->"; then
      return 0
    fi
  done
  return 1
}

http_ok() {
  local code
  code="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$1" 2>/dev/null || true)"
  case "$code" in
    2* | 3*) return 0 ;;
    *) return 1 ;;
  esac
}

wait_for() {
  local label="$1" timeout="$2"
  shift 2
  local waited=0
  while [ "$waited" -lt "$timeout" ]; do
    if "$@" >/dev/null 2>&1; then
      printf '    %s ready after %ss\n' "$label" "$waited"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  printf '    %s NOT ready after %ss\n' "$label" "$timeout" >&2
  return 1
}

container_running() {
  docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true
}

pid_file() { printf '%s/%s.pid' "$PID_DIR" "$1"; }

pid_alive() {
  local file pid
  file="$(pid_file "$1")"
  [ -f "$file" ] || return 1
  pid="$(cat "$file" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

start_process() {
  local name="$1"
  shift
  if pid_alive "$name"; then
    printf '    %s already running (pid %s)\n' "$name" "$(cat "$(pid_file "$name")")"
    return 0
  fi
  nohup "$@" >"$LOG_DIR/$name.log" 2>&1 &
  printf '%s' "$!" >"$(pid_file "$name")"
  printf '    %s started (pid %s, log %s)\n' "$name" "$!" "$LOG_DIR/$name.log"
}

stop_process() {
  local name="$1" file pid waited=0
  file="$(pid_file "$name")"
  [ -f "$file" ] || return 0
  pid="$(cat "$file" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 10 ]; do
      sleep 1
      waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid" 2>/dev/null || true; fi
    printf '    %s stopped (pid %s)\n' "$name" "$pid"
  fi
  rm -f "$file"
}

# Environment shared by every backend-side process (backend, queue worker,
# verify). Written once to a file with single-quoted values so that spaces or
# shell metacharacters in operator-supplied keys cannot be re-interpreted.
write_child_env() {
  local out="$CHILD_ENV_FILE" key value escaped
  : >"$out"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    key="${line%%=*}"
    value="${line#*=}"
    escaped="$(printf '%s' "$value" | sed "s/'/'\\\\''/g")"
    printf "%s='%s'\n" "$key" "$escaped" >>"$out"
  done < <(backend_env)
}

backend_env() {
  printf 'FIRESTORE_PG_DSN=%s\n' "$PG_DSN_SQLALCHEMY"
  printf 'FIRESTORE_EMULATOR_HOST=127.0.0.1:%s\n' "$OMI_LOCAL_FIRESTORE_PORT"
  printf 'FIREBASE_AUTH_EMULATOR_HOST=127.0.0.1:%s\n' "$OMI_LOCAL_FIREBASE_AUTH_PORT"
  printf 'STORAGE_EMULATOR_HOST=127.0.0.1:%s\n' "$OMI_LOCAL_FIREBASE_STORAGE_PORT"
  printf 'FIREBASE_PROJECT_ID=demo-omi-local\n'
  printf 'OMI_ENV_STAGE=local\n'
  # The self-hosted runtime selects its data plane from the generated profile
  # table (backend/fork/profile.py), not from individual switches; `up` renders
  # that table for the local stage before starting anything.
  printf 'OMI_DEPLOYMENT_PROFILE=self_hosted.local\n'
  printf 'OMI_DEPLOYMENT_TARGET=self_hosted\n'
  printf 'PUBLIC_BACKEND_URL=%s/\n' "$BACKEND_URL"
  printf 'PUBLIC_AUTH_URL=%s\n' "$AUTH_URL"
  printf 'PUBLIC_MCP_URL=%s\n' "$BACKEND_URL"
  printf 'PUBLIC_OBJECTS_URL=%s\n' "$MINIO_PUBLIC_ENDPOINT"
  printf 'OMI_SHARE_BASE_URL=http://127.0.0.1:%s\n' "$OMI_LOCAL_SHARE_PORT"
  printf 'RATE_LIMIT_SHADOW_MODE=true\n'
  printf 'ENCRYPTION_SECRET=%s\n' "$OMI_LOCAL_ENCRYPTION_SECRET"
  printf 'AUTH_PROVIDER=better_auth\n'
  printf 'AUTH_JWKS_URL=%s/api/auth/jwks\n' "$AUTH_URL"
  printf 'AUTH_DEV_ISSUER_SECRET=%s\n' "$OMI_LOCAL_AUTH_DEV_ISSUER_SECRET"
  printf 'STORAGE_BACKEND=minio\n'
  printf 'MINIO_ENDPOINT=%s\n' "$MINIO_ENDPOINT"
  printf 'MINIO_PUBLIC_ENDPOINT=%s\n' "$MINIO_PUBLIC_ENDPOINT"
  printf 'MINIO_REGION=us-east-1\n'
  printf 'MINIO_ACCESS_KEY=%s\n' "$MINIO_ROOT_USER"
  printf 'MINIO_SECRET_KEY=%s\n' "$MINIO_ROOT_PASSWORD"
  printf 'BUCKET_SPEECH_PROFILES=omi-speech-profiles\n'
  printf 'BUCKET_POSTPROCESSING=omi-postprocessing\n'
  printf 'BUCKET_MEMORIES_RECORDINGS=omi-memories-recordings\n'
  printf 'BUCKET_PRIVATE_CLOUD_SYNC=omi-private-cloud-sync\n'
  printf 'BUCKET_TEMPORAL_SYNC_LOCAL=omi-temporal-sync-local\n'
  printf 'BUCKET_PLUGINS_LOGOS=omi-plugins-logos\n'
  printf 'BUCKET_APP_THUMBNAILS=omi-app-thumbnails\n'
  printf 'BUCKET_CHAT_FILES=omi-chat-files\n'
  printf 'BUCKET_DESKTOP_UPDATES=omi-desktop-updates\n'
  printf 'QUEUE_BACKEND=redis\n'
  # One shared local secret, bound to each queue's own env name: the worker
  # presents it as x-omi-queue-secret and the matching handler route verifies it
  # (backend/utils/cloud_tasks.py::_verify_redis_worker). All four names must be
  # set or the corresponding worker thread refuses to start.
  printf 'QUEUE_REDIS_WORKER_SECRET=%s\n' "$OMI_LOCAL_QUEUE_WORKER_SECRET"
  printf 'QUEUE_REDIS_SYNC_WORKER_SECRET=%s\n' "$OMI_LOCAL_QUEUE_WORKER_SECRET"
  printf 'QUEUE_REDIS_AUDIO_MERGE_WORKER_SECRET=%s\n' "$OMI_LOCAL_QUEUE_WORKER_SECRET"
  printf 'QUEUE_REDIS_ACCOUNT_DELETION_WORKER_SECRET=%s\n' "$OMI_LOCAL_QUEUE_WORKER_SECRET"
  printf 'QUEUE_REDIS_FINALIZATION_WORKER_SECRET=%s\n' "$OMI_LOCAL_QUEUE_WORKER_SECRET"
  printf 'REDIS_DB_HOST=127.0.0.1\n'
  printf 'REDIS_DB_PASSWORD=%s\n' "$OMI_LOCAL_REDIS_PASSWORD"
  printf 'REDIS_DB_PORT=%s\n' "$OMI_LOCAL_REDIS_PORT"
  printf 'SYNC_TASKS_HANDLER_URL=%s/v2/sync-jobs/run\n' "$BACKEND_URL"
  printf 'AUDIO_MERGE_HANDLER_URL=%s/v2/audio-merge-jobs/run\n' "$BACKEND_URL"
  printf 'ACCOUNT_DELETION_HANDLER_URL=%s/v1/users/account-deletion-wipes/run\n' "$BACKEND_URL"
  printf 'LISTEN_FINALIZATION_TASKS_HANDLER_URL=%s/v1/conversation-finalization-jobs/run\n' "$BACKEND_URL"
  printf 'SENSEVOICE_MODEL_DIR=%s\n' "${SENSEVOICE_MODEL_DIR:-/tmp/sherpa/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17}"
  printf 'STT_SERVICE_MODELS=%s\n' "${STT_SERVICE_MODELS:-sensevoice}"
  # Operator-supplied AI providers are forwarded only when actually set.
  for passthrough in MIMO_API_KEY MIMO_API_BASE MIMO_TOKENPLAN_BASE MOSS_API_KEY \
    TTS_PROVIDER MIMO_USE_TOKENPLAN MIMO_TTS_VOICE TRANSLATION_PROVIDER \
    CHAT_PROVIDER STT_PRERECORDED_MODEL PROVIDER_MODE; do
    eval "_value=\${$passthrough:-}"
    if [ -n "$_value" ]; then printf '%s=%s\n' "$passthrough" "$_value"; fi
  done
  return 0
}

# Run a command with the shared backend environment applied.
run_with_backend_env() {
  write_child_env
  (
    set -a
    # shellcheck disable=SC1090
    . "$CHILD_ENV_FILE"
    set +a
    "$@"
  )
}

auth_secret_fingerprint() {
  printf '%s' "$1" | shasum -a 256 | awk '{print substr($1,1,16)}'
}

# ------------------------------------------------------------------- subcommands
cmd_ports() {
  log "Omi local dev — resolved ports"
  log "config: $_config_source"
  log ""
  printf '  %-16s %-6s %-8s %s\n' SERVICE PORT STATE OWNER
  # One probe per port: the table and the collision list must never disagree,
  # and a listening socket with a full accept queue can refuse the second probe.
  local conflicts=""
  _port_row() {
    local service="$1" port="$2" state owner
    if port_busy "$port"; then
      if port_is_ours "$port"; then
        state="in use"
      else
        state="TAKEN"
        conflicts="$conflicts $service($port)"
      fi
      owner="$(port_owner "$port")"
    else
      state="free"
      owner="-"
    fi
    printf '  %-16s %-6s %-8s %s\n' "$service" "$port" "$state" "$owner"
  }
  _port_row postgres "$OMI_LOCAL_POSTGRES_PORT"
  _port_row redis "$OMI_LOCAL_REDIS_PORT"
  _port_row minio-api "$OMI_LOCAL_MINIO_API_PORT"
  _port_row minio-console "$OMI_LOCAL_MINIO_CONSOLE_PORT"
  _port_row firestore "$OMI_LOCAL_FIRESTORE_PORT"
  _port_row firebase-auth "$OMI_LOCAL_FIREBASE_AUTH_PORT"
  _port_row firebase-storage "$OMI_LOCAL_FIREBASE_STORAGE_PORT"
  _port_row auth-server "$OMI_LOCAL_AUTH_PORT"
  _port_row backend "$OMI_LOCAL_BACKEND_PORT"

  if [ -n "$conflicts" ]; then
    log ""
    warn "ports held by another process:$conflicts"
    log "    copy $ENV_EXAMPLE to $ENV_FILE and change those ports, then re-run."
    return 1
  fi
  return 0
}

cmd_up() {
  for arg in "$@"; do
    case "$arg" in
      # Kept for compatibility: the checkout harness only ever starts the data
      # plane, because the self-hosted runtime needs the image (see cmd_selfhost).
      --no-backend) ;;
      *) die "unknown option for up: $arg" ;;
    esac
  done

  step "[1/4] prerequisites"
  command -v docker >/dev/null || die "docker is required"
  docker info >/dev/null 2>&1 || die "docker daemon is not responding (start Docker/colima)"
  [ -x "$PYTHON_BIN" ] || die "backend venv missing — run 'make setup-backend' (expected $PYTHON_BIN)"
  [ -d "$AUTH_DIR/node_modules" ] || die "auth-server dependencies missing — run 'cd auth-server && npm ci'"
  log "    docker, backend venv, auth-server deps: OK"

  step "[2/4] ports"
  cmd_ports || die "resolve the port collisions above before starting the stack"

  step "[3/4] containers (postgres / redis / minio / firebase emulators)"
  docker compose -f "$COMPOSE_FILE" up -d

  wait_for "postgres" 60 docker exec "$POSTGRES_CONTAINER" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" ||
    die "postgres not ready — docker logs $POSTGRES_CONTAINER"
  wait_for "redis" 30 docker exec "$REDIS_CONTAINER" redis-cli -a "$OMI_LOCAL_REDIS_PASSWORD" ping ||
    die "redis not ready — docker logs $REDIS_CONTAINER"
  wait_for "minio" 60 curl -sf "$MINIO_ENDPOINT/minio/health/live" ||
    die "minio not ready — docker logs $MINIO_CONTAINER"
  wait_for "firebase emulators" 120 curl -sf "http://127.0.0.1:$OMI_LOCAL_FIRESTORE_PORT/" ||
    warn "firebase emulators not ready; desktop local-auth flows will not work"

  step "[4/4] schema (Better Auth + firestore-pg) and auth-server"
  local fingerprint
  fingerprint="$(auth_secret_fingerprint "$OMI_LOCAL_BETTER_AUTH_SECRET")"
  if [ -f "$STATE_ENV" ]; then
    # shellcheck disable=SC1090
    . "$STATE_ENV"
    if [ -n "${OMI_LOCAL_AUTH_SECRET_FINGERPRINT:-}" ] &&
      [ "$OMI_LOCAL_AUTH_SECRET_FINGERPRINT" != "$fingerprint" ]; then
      log "    BETTER_AUTH_SECRET changed — retiring signing keys encrypted with the old secret"
      docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'TRUNCATE jwks' >/dev/null
    fi
  fi
  (cd "$AUTH_DIR" && DATABASE_URL="$PG_DSN" BETTER_AUTH_SECRET="$OMI_LOCAL_BETTER_AUTH_SECRET" \
    BETTER_AUTH_URL="$AUTH_URL" PORT="$OMI_LOCAL_AUTH_PORT" npm run --silent migrate) ||
    die "Better Auth migration failed (see output above)"
  log "    Better Auth schema + signing keys: current"

  # Same one-shot migration the production compose runs as `firestore-pg-migrate`
  # (deploy/self-host/compose.production.yml). Without it the shim reports
  # SchemaNotCurrent and every authenticated request fails closed with 503.
  write_child_env
  local migrate_log="$LOG_DIR/firestore-pg-migrate.log"
  if ! (cd "$BACKEND_DIR" && set -a && . "$CHILD_ENV_FILE" && set +a &&
    "$PYTHON_BIN" -m fork.migrate migrate) >"$migrate_log" 2>&1; then
    cat "$migrate_log" >&2
    if grep -q "legacy firestore_pg tables" "$migrate_log"; then
      log ""
      log "    This local database was created by an older shim schema and cannot be"
      log "    migrated in place. Local dev data is disposable:"
      log "        dev/local.sh reset && dev/local.sh up"
    fi
    die "firestore-pg migration failed"
  fi
  log "    firestore-pg schema: current"

  start_process auth-server bash -c \
    "cd '$AUTH_DIR' && PORT='$OMI_LOCAL_AUTH_PORT' DATABASE_URL='$PG_DSN' \
     BETTER_AUTH_SECRET='$OMI_LOCAL_BETTER_AUTH_SECRET' BETTER_AUTH_URL='$AUTH_URL' \
     AUTH_DEV_ISSUER_SECRET='$OMI_LOCAL_AUTH_DEV_ISSUER_SECRET' exec node src/index.js"
  wait_for "auth-server" 60 curl -sf "$AUTH_URL/health" ||
    die "auth-server not ready — dev/local.sh logs auth-server"

  printf 'OMI_LOCAL_AUTH_SECRET_FINGERPRINT=%s\n' "$fingerprint" >"$STATE_ENV"

  step "local data plane is up"
  log "  auth-server  $AUTH_URL            (GET /api/auth/jwks)"
  log "  postgres     postgresql://127.0.0.1:$OMI_LOCAL_POSTGRES_PORT/$POSTGRES_DB"
  log "  redis        127.0.0.1:$OMI_LOCAL_REDIS_PORT  (auth required)"
  log "  minio        $MINIO_ENDPOINT   (console http://127.0.0.1:$OMI_LOCAL_MINIO_CONSOLE_PORT)"
  log "  emulators    firestore $OMI_LOCAL_FIRESTORE_PORT / auth $OMI_LOCAL_FIREBASE_AUTH_PORT / storage $OMI_LOCAL_FIREBASE_STORAGE_PORT"
  log ""
  log "  This checkout harness does not run the backend: the self-hosted runtime is"
  log "  an image with a rendered profile, model stores and qdrant. See:"
  log "      dev/local.sh selfhost"
  log ""
  log "  prove the data plane:  dev/local.sh verify"
}

cmd_selfhost() {
  cat <<'EOF'
Server OS (self-hosted) local runtime

The self-hosted backend is an image, not a checkout process: the profile table,
the speech/LLM model stores and the Qdrant/Typesense/SearXNG services are all
part of it (deploy/self-host/Dockerfile renders
`self_hosted.<stage>` with scripts/profiles/render.py).

  SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
    deploy/self-host/operations.sh start

Start from deploy/self-host/.env.production.example: set SELF_HOST_STAGE=local,
point PUBLIC_* at the rendered self_hosted.local profile
(`python3 scripts/profiles/render.py --target self_hosted \
   --manifest brand/omi-upstream/manifest.yaml --stage local --emit-json`),
and replace every REPLACE_* value. `python3 deploy/self-host/check-config.py
--env-file <file>` validates the result before anything starts.

Two host constraints on this Mac Studio:

  * deploy/self-host/build-images.sh requires BACKEND_PLATFORM=linux/amd64, so
    the images build under emulation (the fixture build exceeded 20 minutes
    here); use an amd64 builder or CI for the image step.
  * the local profile declares speech (SenseVoice + Kokoro) and an Ollama
    `qwen3:1.7b` LLM, so those model stores must exist before `start`.

`dev/local.sh up` therefore starts only the data plane: postgres, redis
(password-authenticated), minio, the Firebase emulators and the Better Auth
auth-server — enough for client work and for the migrations the runtime needs.
EOF
}

cmd_status() {
  log "Omi local dev — status (data plane)"
  log "config: $_config_source"
  log "state:  $STATE_DIR"
  log ""
  printf '  %-14s %-9s %-44s %s
' SERVICE KIND ENDPOINT STATE
  _row() { printf '  %-14s %-9s %-44s %s
' "$1" "$2" "$3" "$4"; }
  local state
  for pair in "postgres:$POSTGRES_CONTAINER" "redis:$REDIS_CONTAINER" \
    "minio:$MINIO_CONTAINER" "emulators:$EMULATOR_CONTAINER"; do
    if container_running "${pair#*:}"; then state="up"; else state="down"; fi
    _row "${pair%%:*}" container "${pair#*:}" "$state"
  done
  if http_ok "$AUTH_URL/health"; then state="up"; elif pid_alive auth-server; then state="starting"; else state="down"; fi
  _row auth-server process "$AUTH_URL" "$state"
  if http_ok "$BACKEND_URL/v1/health"; then state="up"; elif pid_alive backend; then state="starting"; else state="absent"; fi
  _row backend process "$BACKEND_URL" "$state"
  log ""
  log "backend is 'absent' unless you started it yourself: the self-hosted runtime is"
  log "an image (dev/local.sh selfhost), not a checkout process."
  log ""
  local evidence
  evidence="$(ls -1t "$EVIDENCE_DIR"/local-verify-*.json 2>/dev/null | head -1 || true)"
  if [ -n "$evidence" ]; then
    log "last verify evidence: $evidence"
  else
    log "last verify evidence: none — run 'dev/local.sh verify'"
  fi
}

cmd_verify() {
  local out
  out="$EVIDENCE_DIR/local-verify-$(date -u +%Y%m%dT%H%M%SZ).json"
  http_ok "$AUTH_URL/health" || die "auth-server is not up — run 'dev/local.sh up' first"
  export OMI_LOCAL_BACKEND_URL="$BACKEND_URL"
  export OMI_LOCAL_AUTH_URL="$AUTH_URL"
  export OMI_LOCAL_STATE_DIR="$STATE_DIR"
  export OMI_LOCAL_POSTGRES_PORT OMI_LOCAL_REDIS_PORT
  export OMI_LOCAL_MINIO_API_PORT OMI_LOCAL_MINIO_CONSOLE_PORT
  export OMI_LOCAL_BACKEND_PORT OMI_LOCAL_AUTH_PORT
  run_with_backend_env "$PYTHON_BIN" "$DEV_DIR/local_verify.py" --evidence "$out"
}

cmd_restart() {
  step "restarting the auth-server (containers and data are kept)"
  stop_process auth-server
  cmd_up
}

cmd_logs() {
  local service="${1:-}" file
  if [ -n "$service" ]; then
    file="$LOG_DIR/$service.log"
    [ -f "$file" ] || die "no log for '$service' (available: $(ls "$LOG_DIR" 2>/dev/null | sed 's/\.log$//' | tr '\n' ' '))"
    tail -n 200 -f "$file"
    return 0
  fi
  for file in "$LOG_DIR"/*.log; do
    [ -f "$file" ] || continue
    printf '\n===== %s =====\n' "$(basename "$file")"
    tail -n 50 "$file"
  done
}

cmd_down() {
  step "stopping application processes"
  stop_process backend
  stop_process queue-worker
  stop_process auth-server
  step "stopping containers"
  docker compose -f "$COMPOSE_FILE" down
  log "stack down (volumes kept — 'dev/local.sh reset' deletes data)"
}

cmd_reset() {
  cmd_down
  step "deleting volumes"
  docker compose -f "$COMPOSE_FILE" down --volumes
  rm -f "$STATE_ENV"
  log "local dev data wiped — 'dev/local.sh up' recreates it from scratch"
}

cmd_env() {
  printf 'export OMI_LOCAL_BACKEND_URL=%s\n' "$BACKEND_URL"
  printf 'export OMI_LOCAL_AUTH_URL=%s\n' "$AUTH_URL"
  printf 'export OMI_DESKTOP_API_URL=%s/\n' "$BACKEND_URL"
  printf 'export OMI_PYTHON_API_URL=%s/\n' "$BACKEND_URL"
  printf 'export FIRESTORE_EMULATOR_HOST=127.0.0.1:%s\n' "$OMI_LOCAL_FIRESTORE_PORT"
  printf 'export FIREBASE_AUTH_EMULATOR_HOST=127.0.0.1:%s\n' "$OMI_LOCAL_FIREBASE_AUTH_PORT"
  printf 'export STORAGE_EMULATOR_HOST=127.0.0.1:%s\n' "$OMI_LOCAL_FIREBASE_STORAGE_PORT"
  printf 'export AUTH_DEV_ISSUER_SECRET=%s\n' "$OMI_LOCAL_AUTH_DEV_ISSUER_SECRET"
  printf 'export FIREBASE_PROJECT_ID=demo-omi-local\n'
}

cmd_help() {
  cat <<'EOF'
Omi local dev — stage 1 of the fork pipeline, on main.

  dev/local.sh up        start the local data plane and wait for health
  dev/local.sh verify    end-to-end proof of what is running + JSON evidence
  dev/local.sh status    what is running, on which ports, healthy or not
  dev/local.sh selfhost  what the Server OS (self-hosted) runtime needs, and why
                         the checkout harness does not run it
  dev/local.sh restart   restart the auth-server
  dev/local.sh logs [svc]  tail logs (auth-server, firestore-pg-migrate, ...)
  dev/local.sh ports     resolved port allocation + collision report
  dev/local.sh env       print `export ...` lines for the current shell
  dev/local.sh down      stop everything (keeps volumes)
  dev/local.sh reset     stop and delete local data (destructive)

Configuration: dev/local.env (copy dev/local.env.example).
Precedence: ambient environment > dev/local.env > dev/local.env.example
EOF
}

# ------------------------------------------------------------------------ entry
command="${1:-help}"
shift || true
case "$command" in
  up) cmd_up "$@" ;;
  selfhost) cmd_selfhost ;;
  status) cmd_status ;;
  verify) cmd_verify "$@" ;;
  restart) cmd_restart ;;
  logs) cmd_logs "$@" ;;
  ports) cmd_ports ;;
  env) cmd_env ;;
  down) cmd_down ;;
  reset) cmd_reset ;;
  help | -h | --help) cmd_help ;;
  --stop) cmd_down ;;          # legacy dev/deploy-local.sh flag
  --no-backend) cmd_up --no-backend ;; # legacy dev/deploy-local.sh flag
  *) die "unknown command '$command' (try: dev/local.sh help)" ;;
esac
