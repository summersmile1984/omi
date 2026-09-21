#!/usr/bin/env bash
# Fork local runtime: Compose owns containers; PID + start identity owns processes.
set -euo pipefail
umask 077
_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_DIR="$_REPO_ROOT/dev"
BACKEND_DIR="$_REPO_ROOT/backend"
AUTH_DIR="$_REPO_ROOT/auth-server"
ENV_FILE="${OMI_LOCAL_ENV_FILE:-$DEV_DIR/local.env}"
ENV_EXAMPLE="$DEV_DIR/local.env.example"
STATE_DIR="${OMI_LOCAL_STATE_DIR:-$_REPO_ROOT/.local/local-dev}"
mkdir -p "$STATE_DIR"
STATE_DIR="$(cd "$STATE_DIR" && pwd)"
LOG_DIR="$STATE_DIR/logs"
PID_DIR="$STATE_DIR/pids"
EVIDENCE_DIR="$STATE_DIR/evidence"
TABLE="$STATE_DIR/deployment_profiles.generated.json"
CHILD_ENV_FILE="$STATE_DIR/child.env"
STATE_ENV="$STATE_DIR/auth-secret.sha256"
mkdir -p "$LOG_DIR" "$PID_DIR" "$EVIDENCE_DIR"
PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"
log() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Only explicitly local-scoped configuration may cross the parent boundary.
_CONFIG_KEYS="OMI_LOCAL_POSTGRES_PORT OMI_LOCAL_REDIS_PORT OMI_LOCAL_MINIO_API_PORT
OMI_LOCAL_MINIO_CONSOLE_PORT OMI_LOCAL_FIRESTORE_PORT OMI_LOCAL_FIREBASE_AUTH_PORT
OMI_LOCAL_FIREBASE_STORAGE_PORT OMI_LOCAL_AUTH_PORT OMI_LOCAL_BACKEND_PORT
OMI_LOCAL_BETTER_AUTH_SECRET OMI_LOCAL_AUTH_DEV_ISSUER_SECRET OMI_LOCAL_QUEUE_WORKER_SECRET
OMI_LOCAL_ENCRYPTION_SECRET OMI_LOCAL_REDIS_PASSWORD OMI_LOCAL_INTERNAL_ADMIN_SECRET
OMI_LOCAL_QDRANT_PORT OMI_LOCAL_QDRANT_GRPC_PORT OMI_LOCAL_TYPESENSE_PORT
OMI_LOCAL_QDRANT_API_KEY OMI_LOCAL_QDRANT_COLLECTION_PREFIX OMI_LOCAL_TYPESENSE_API_KEY OMI_LOCAL_EMBEDDING_ENDPOINT
OMI_LOCAL_SHARE_PORT OMI_LOCAL_AI_PROFILE OMI_LOCAL_BRAND_MANIFEST
OMI_LOCAL_MIMO_API_KEY OMI_LOCAL_OPENROUTER_API_KEY OMI_LOCAL_SILICONFLOW_API_KEY
OMI_LOCAL_CLOUDFLARE_API_TOKEN"
_ambient_keys=() _ambient_values=()
for key in $_CONFIG_KEYS; do
  if [ "${!key+x}" ]; then
    _ambient_keys+=("$key")
    _ambient_values+=("${!key}")
  fi
done
. "$ENV_EXAMPLE"
_default_prefix="$OMI_LOCAL_QDRANT_COLLECTION_PREFIX"
unset OMI_LOCAL_QDRANT_COLLECTION_PREFIX
_config_source="$ENV_EXAMPLE"
if [ -f "$ENV_FILE" ]; then . "$ENV_FILE"; _config_source="$ENV_FILE"; fi
_prefix_explicit="${OMI_LOCAL_QDRANT_COLLECTION_PREFIX+x}"
OMI_LOCAL_QDRANT_COLLECTION_PREFIX="${OMI_LOCAL_QDRANT_COLLECTION_PREFIX-$_default_prefix}"
for ((i=0; i<${#_ambient_keys[@]}; i++)); do
  printf -v "${_ambient_keys[$i]}" '%s' "${_ambient_values[$i]}"
  if [ "${_ambient_keys[$i]}" = OMI_LOCAL_QDRANT_COLLECTION_PREFIX ]; then _prefix_explicit=x; fi
done
for key in $_CONFIG_KEYS; do
  case "${!key:-}" in *$'\n'*|*$'\r'*) die "multiline local configuration is not supported: $key" ;; esac
  case "$key" in
    OMI_LOCAL_MIMO_API_KEY|OMI_LOCAL_OPENROUTER_API_KEY|OMI_LOCAL_SILICONFLOW_API_KEY|OMI_LOCAL_CLOUDFLARE_API_TOKEN) continue ;;
  esac
  [ -n "${!key:-}" ] || die "configuration error: $key is empty (check $_config_source)"
  case "$key" in
    *_PORT) [[ "${!key}" =~ ^[0-9]+$ ]] && [ "${!key}" -gt 0 ] && [ "${!key}" -le 65535 ] || die "invalid port: $key" ;;
  esac
done
# A state directory is the instance identity. Neither arbitrary COMPOSE_PROJECT_NAME
# nor another checkout's globally named containers can be adopted accidentally.
INSTANCE="omi-local-$(printf '%s' "$_REPO_ROOT:$STATE_DIR" | shasum -a 256 | cut -c1-12)"
CLEAN_ENV=(env -i "PATH=$PATH" "HOME=$STATE_DIR/home" "LANG=${LANG:-C.UTF-8}")
mkdir -p "$STATE_DIR/home"
PG_DSN="postgresql://omi:omi-dev-password@127.0.0.1:$OMI_LOCAL_POSTGRES_PORT/omi"
PG_DSN_SQLALCHEMY="postgresql+psycopg://omi:omi-dev-password@127.0.0.1:$OMI_LOCAL_POSTGRES_PORT/omi"
AUTH_URL="http://127.0.0.1:$OMI_LOCAL_AUTH_PORT"
BACKEND_URL="http://127.0.0.1:$OMI_LOCAL_BACKEND_PORT"
MINIO_ENDPOINT="http://127.0.0.1:$OMI_LOCAL_MINIO_API_PORT"
compose() {
  # Docker client context is local infrastructure authority, never backend env.
  env -i "PATH=$PATH" "HOME=$HOME" "DOCKER_HOST=${DOCKER_HOST:-}" \
    "DOCKER_CONTEXT=${DOCKER_CONTEXT:-}" \
    "DEV_POSTGRES_PORT=$OMI_LOCAL_POSTGRES_PORT" "DEV_REDIS_PORT=$OMI_LOCAL_REDIS_PORT" \
    "DEV_REDIS_PASSWORD=$OMI_LOCAL_REDIS_PASSWORD" \
    "DEV_MINIO_API_PORT=$OMI_LOCAL_MINIO_API_PORT" "DEV_MINIO_CONSOLE_PORT=$OMI_LOCAL_MINIO_CONSOLE_PORT" \
    "FIRESTORE_EMULATOR_PORT=$OMI_LOCAL_FIRESTORE_PORT" \
    "FIREBASE_AUTH_EMULATOR_PORT=$OMI_LOCAL_FIREBASE_AUTH_PORT" \
    "FIREBASE_STORAGE_EMULATOR_PORT=$OMI_LOCAL_FIREBASE_STORAGE_PORT" \
    "DEV_QDRANT_PORT=$OMI_LOCAL_QDRANT_PORT" "DEV_QDRANT_GRPC_PORT=$OMI_LOCAL_QDRANT_GRPC_PORT" \
    "DEV_QDRANT_API_KEY=$OMI_LOCAL_QDRANT_API_KEY" \
    "DEV_TYPESENSE_PORT=$OMI_LOCAL_TYPESENSE_PORT" "DEV_TYPESENSE_API_KEY=$OMI_LOCAL_TYPESENSE_API_KEY" \
    docker compose --env-file /dev/null -p "$INSTANCE" -f "$DEV_DIR/docker-compose.dev.yml" "$@"
}
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
http_ok() { curl --noproxy '*' -fsS -m 3 "$1" >/dev/null 2>&1; }
wait_for() {
  local label="$1" timeout="$2" waited=0; shift 2
  while [ "$waited" -lt "$timeout" ]; do
    if "$@" >/dev/null 2>&1; then log "    $label ready"; return 0; fi
    sleep 1; waited=$((waited + 1))
  done
  log "    $label NOT ready after ${timeout}s" >&2; return 1
}
pid_file() { printf '%s/%s.pid' "$PID_DIR" "$1"; }
pid_alive() {
  local file pid stamp current
  file="$(pid_file "$1")"
  [ -f "$file" ] && [ -f "$file.started" ] || return 1
  read -r pid <"$file" || return 1
  [[ "$pid" =~ ^[0-9]+$ ]] && [ "$pid" -gt 1 ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  stamp="$(cat "$file.started")"
  current="$(ps -o lstart= -o command= -p "$pid" 2>/dev/null)"
  [ -n "$stamp" ] && [ "$stamp" = "$current" ]
}
start_process() {
  local name="$1" file pid; shift
  if pid_alive "$name"; then return 0; fi
  file="$(pid_file "$name")"
  nohup "$@" </dev/null >"$LOG_DIR/$name.log" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" >"$file"
  # Wait for exec before recording identity; the command must stabilize, not the
  # service's readiness. Readiness is checked against the real endpoint below.
  sleep 1
  ps -o lstart= -o command= -p "$pid" >"$file.started" || die "$name exited (see $LOG_DIR/$name.log)"
  disown
}
stop_process() {
  local name="$1" file pid waited=0
  file="$(pid_file "$name")"
  if pid_alive "$name"; then
    read -r pid <"$file"
    kill "$pid"
    while pid_alive "$name" && [ "$waited" -lt 60 ]; do sleep 1; waited=$((waited + 1)); done
    if pid_alive "$name"; then kill -9 "$pid"; fi
  fi
  rm -f "$file" "$file.started"
}
select_ai() {
  NO_BACKEND=false
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --no-backend) NO_BACKEND=true; shift ;;
      --core-only) OMI_LOCAL_AI_PROFILE=core-only; shift ;;
      --operator-ai) [ "$#" -ge 2 ] || die '--operator-ai requires a provider'; OMI_LOCAL_AI_PROFILE="$2"; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  case "$OMI_LOCAL_AI_PROFILE" in core-only|mimo-cn|openrouter|cloudflare-gateway|siliconflow) ;; *) die 'invalid local AI profile' ;; esac
}
render_profile() {
  local args=(--core-only)
  if [ "$OMI_LOCAL_AI_PROFILE" != core-only ]; then args=(--operator-ai "$OMI_LOCAL_AI_PROFILE"); fi
  "${CLEAN_ENV[@]}" "$PYTHON_BIN" "$_REPO_ROOT/scripts/profiles/render.py" \
    --target self_hosted --manifest "$OMI_LOCAL_BRAND_MANIFEST" --stage local \
    "${args[@]}" --emit-json >"$TABLE.tmp" || { rm -f "$TABLE.tmp"; die 'profile rendering failed'; }
  mv "$TABLE.tmp" "$TABLE"
}
backend_env() {
  # Existing env-loader admission: skip backend/.env and stage files entirely.
  printf 'OMI_HARNESS_INSTANCE=%s\n' "$INSTANCE"
  printf '%s\n' "FIRESTORE_PG_DSN=$PG_DSN_SQLALCHEMY" \
    'OMI_ENV_STAGE=local' 'OMI_DEPLOYMENT_PROFILE=self_hosted.local' 'OMI_DEPLOYMENT_TARGET=self_hosted' \
    "OMI_DEPLOYMENT_PROFILES_PATH=$TABLE" "PUBLIC_BACKEND_URL=$BACKEND_URL/" \
    "PUBLIC_AUTH_URL=$AUTH_URL" "PUBLIC_MCP_URL=$BACKEND_URL" "PUBLIC_OBJECTS_URL=$MINIO_ENDPOINT" \
    "OMI_SHARE_BASE_URL=http://127.0.0.1:$OMI_LOCAL_SHARE_PORT" \
    "ENCRYPTION_SECRET=$OMI_LOCAL_ENCRYPTION_SECRET" 'AUTH_PROVIDER=better_auth' \
    "AUTH_JWKS_URL=$AUTH_URL/api/auth/jwks" "AUTH_DEV_ISSUER_SECRET=$OMI_LOCAL_AUTH_DEV_ISSUER_SECRET" \
    "AUTH_JWT_ISSUER=$AUTH_URL" "AUTH_JWT_AUDIENCE=$AUTH_URL" "AUTH_SERVER_INTERNAL_URL=$AUTH_URL" \
    'AUTH_INTERNAL_ALLOW_HTTP=true' "AUTH_INTERNAL_ADMIN_SECRET=$OMI_LOCAL_INTERNAL_ADMIN_SECRET" \
    'STORAGE_BACKEND=minio' "MINIO_ENDPOINT=$MINIO_ENDPOINT" "MINIO_PUBLIC_ENDPOINT=$MINIO_ENDPOINT" \
    'MINIO_REGION=us-east-1' 'MINIO_ACCESS_KEY=minioadmin' 'MINIO_SECRET_KEY=minioadmin' \
    'QUEUE_BACKEND=redis' 'REDIS_DB_HOST=127.0.0.1' "REDIS_DB_PORT=$OMI_LOCAL_REDIS_PORT" \
    "REDIS_DB_PASSWORD=$OMI_LOCAL_REDIS_PASSWORD" \
    "QDRANT_URL=http://127.0.0.1:$OMI_LOCAL_QDRANT_PORT" "QDRANT_API_KEY=$OMI_LOCAL_QDRANT_API_KEY" \
    "QDRANT_COLLECTION_PREFIX=$OMI_LOCAL_QDRANT_COLLECTION_PREFIX" 'TYPESENSE_HOST=127.0.0.1' \
    "TYPESENSE_HOST_PORT=$OMI_LOCAL_TYPESENSE_PORT" 'TYPESENSE_PROTOCOL=http' \
    "TYPESENSE_API_KEY=$OMI_LOCAL_TYPESENSE_API_KEY" 'MEMORY_TYPESENSE_COLLECTION=memories' \
    "EMBEDDING_ENDPOINT=$OMI_LOCAL_EMBEDDING_ENDPOINT" \
    "OMI_LOCAL_BACKEND_URL=$BACKEND_URL" "OMI_LOCAL_AUTH_URL=$AUTH_URL"
  local key
  for key in SPEECH_PROFILES POSTPROCESSING MEMORIES_RECORDINGS PRIVATE_CLOUD_SYNC TEMPORAL_SYNC_LOCAL PLUGINS_LOGOS APP_THUMBNAILS CHAT_FILES DESKTOP_UPDATES; do
    printf 'BUCKET_%s=omi-%s\n' "$key" "$(printf '%s' "$key" | tr 'A-Z_' 'a-z-')"
  done
  for key in SYNC AUDIO_MERGE ACCOUNT_DELETION FINALIZATION; do
    printf 'QUEUE_REDIS_%s_WORKER_SECRET=%s\n' "$key" "$OMI_LOCAL_QUEUE_WORKER_SECRET"
  done
  printf '%s\n' "SYNC_TASKS_HANDLER_URL=$BACKEND_URL/v2/sync-jobs/run" \
    "AUDIO_MERGE_HANDLER_URL=$BACKEND_URL/v2/audio-merge-jobs/run" \
    "ACCOUNT_DELETION_HANDLER_URL=$BACKEND_URL/v1/users/account-deletion-wipes/run" \
    "LISTEN_FINALIZATION_TASKS_HANDLER_URL=$BACKEND_URL/v1/conversation-finalization-jobs/run"
  case "$OMI_LOCAL_AI_PROFILE" in
    core-only) return 0 ;;
    mimo-cn) key=MIMO_API_KEY ;;
    openrouter) key=OPENROUTER_API_KEY ;;
    siliconflow) key=SILICONFLOW_API_KEY ;;
    cloudflare-gateway) key=CLOUDFLARE_API_TOKEN ;;
  esac
  local scoped="OMI_LOCAL_$key"
  [ -n "${!scoped:-}" ] || die "explicit local credential required: $scoped"
  printf '%s=%s\n' "$key" "${!scoped}"
}
write_child_env() {
  local line key value
  backend_env >"$CHILD_ENV_FILE.raw" || { rm -f "$CHILD_ENV_FILE.raw"; die 'invalid backend environment'; }
  : >"$CHILD_ENV_FILE"
  while IFS= read -r line; do
    key="${line%%=*}"; value="${line#*=}"
    printf 'export %s=%q\n' "$key" "$value" >>"$CHILD_ENV_FILE"
  done <"$CHILD_ENV_FILE.raw"
  rm -f "$CHILD_ENV_FILE.raw"
}
run_backend() {
  "${CLEAN_ENV[@]}" bash -ec 'cd "$1"; . "$2"; shift 2; exec "$@"' local-backend "$BACKEND_DIR" "$CHILD_ENV_FILE" "$@"
}
auth_env() {
  "${CLEAN_ENV[@]}" "DATABASE_URL=$PG_DSN" "BETTER_AUTH_SECRET=$OMI_LOCAL_BETTER_AUTH_SECRET" \
    "BETTER_AUTH_URL=$AUTH_URL" "PORT=$OMI_LOCAL_AUTH_PORT" 'HOST=127.0.0.1' \
    "AUTH_DEV_ISSUER_SECRET=$OMI_LOCAL_AUTH_DEV_ISSUER_SECRET" \
    "AUTH_INTERNAL_ADMIN_SECRET=$OMI_LOCAL_INTERNAL_ADMIN_SECRET" "$@"
}
container_owns_port() {
  local service="$1" port="$2" internal
  case "$service:$port" in
    postgres:*) internal=5432 ;;
    redis:*) internal=6379 ;;
    minio:"$OMI_LOCAL_MINIO_API_PORT") internal=9000 ;;
    minio:*) internal=9001 ;;
    firebase-emulators:"$OMI_LOCAL_FIRESTORE_PORT") internal=8080 ;;
    firebase-emulators:"$OMI_LOCAL_FIREBASE_AUTH_PORT") internal=9099 ;;
    firebase-emulators:*) internal=9199 ;;
    qdrant:"$OMI_LOCAL_QDRANT_PORT") internal=6333 ;;
    qdrant:*) internal=6334 ;;
    typesense:*) internal=8108 ;;
    *) return 1 ;;
  esac
  [ "$(compose port "$service" "$internal" 2>/dev/null)" = "127.0.0.1:$port" ]
}
cmd_ports() {
  log "Omi local dev — resolved ports ($INSTANCE)"; log "config: $_config_source"
  local pair service port conflicts=''
  for pair in "postgres:$OMI_LOCAL_POSTGRES_PORT" "redis:$OMI_LOCAL_REDIS_PORT" \
    "minio:$OMI_LOCAL_MINIO_API_PORT" "minio:$OMI_LOCAL_MINIO_CONSOLE_PORT" \
    "firebase-emulators:$OMI_LOCAL_FIRESTORE_PORT" "firebase-emulators:$OMI_LOCAL_FIREBASE_AUTH_PORT" \
    "firebase-emulators:$OMI_LOCAL_FIREBASE_STORAGE_PORT" "qdrant:$OMI_LOCAL_QDRANT_PORT" \
    "qdrant:$OMI_LOCAL_QDRANT_GRPC_PORT" "typesense:$OMI_LOCAL_TYPESENSE_PORT" \
    "auth-server:$OMI_LOCAL_AUTH_PORT" "backend:$OMI_LOCAL_BACKEND_PORT"; do
    service="${pair%%:*}"; port="${pair#*:}"
    if ! port_busy "$port"; then printf '  %-20s %s free\n' "$service" "$port"
    elif { pid_alive "$service" && lsof -nP -a -p "$(cat "$(pid_file "$service")")" -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; } || \
      container_owns_port "$service" "$port"; then
      printf '  %-20s %s owned\n' "$service" "$port"
    else printf '  %-20s %s TAKEN\n' "$service" "$port"; conflicts="$conflicts $service($port)"; fi
  done
  [ -z "$conflicts" ] || { log "ports held by another process:$conflicts" >&2; return 1; }
}
cmd_backend_up() {
  select_ai "$@"
  if pid_alive backend; then
    [ -f "$STATE_DIR/ai-profile" ] && [ "$(cat "$STATE_DIR/ai-profile")" = "$OMI_LOCAL_AI_PROFILE" ] ||
      die 'running backend has a different AI profile; use restart with explicit options'
    pid_alive queue-worker || die 'queue worker is not running; use restart'
    http_ok "$BACKEND_URL/v1/health" || die 'owned backend is not healthy; restart it'
    log 'backend already healthy; use restart to change its profile'; return
  fi
  port_busy "$OMI_LOCAL_BACKEND_PORT" && die 'backend port is occupied by an unowned process'
  render_profile
  write_child_env
  run_backend "$PYTHON_BIN" -m fork.vector_qdrant migrate >"$LOG_DIR/qdrant-migrate.log" 2>&1 || die 'Qdrant migration failed; see logs'
  start_process backend "${CLEAN_ENV[@]}" bash -ec 'cd "$1"; . "$2"; shift 2; exec "$@"' local-backend \
    "$BACKEND_DIR" "$CHILD_ENV_FILE" "$PYTHON_BIN" -m uvicorn fork.main:app --host 127.0.0.1 --port "$OMI_LOCAL_BACKEND_PORT"
  if ! wait_for backend 90 http_ok "$BACKEND_URL/v1/health" || ! pid_alive backend; then
    stop_process backend; die "backend failed readiness; see $LOG_DIR/backend.log"
  fi
  run_backend "$PYTHON_BIN" -m fork.worker --check >"$LOG_DIR/queue-admission.log" 2>&1 ||
    { stop_process backend; die 'worker admission failed; see queue-admission.log'; }
  start_process queue-worker "${CLEAN_ENV[@]}" bash -ec 'cd "$1"; . "$2"; shift 2; exec "$@"' local-worker \
    "$BACKEND_DIR" "$CHILD_ENV_FILE" "$PYTHON_BIN" -m fork.worker
  pid_alive queue-worker || die 'queue worker exited; see logs'
  printf '%s\n' "$OMI_LOCAL_AI_PROFILE" >"$STATE_DIR/ai-profile"
  printf '%s\n' "$OMI_LOCAL_QDRANT_COLLECTION_PREFIX" >"$STATE_DIR/qdrant-prefix"
  log "backend healthy: $BACKEND_URL ($OMI_LOCAL_AI_PROFILE; self_hosted.local persistence)"
}
cmd_up() {
  select_ai "$@"
  if pid_alive backend; then
    [ -f "$STATE_DIR/ai-profile" ] && [ "$(cat "$STATE_DIR/ai-profile")" = "$OMI_LOCAL_AI_PROFILE" ] ||
      die 'running backend has a different AI profile; use restart with explicit options'
  fi
  command -v docker >/dev/null || die 'docker is required'
  docker info >/dev/null 2>&1 || die 'Docker daemon is unavailable'
  [ -x "$PYTHON_BIN" ] || die "backend venv missing: $PYTHON_BIN (make setup-backend)"
  [ -d "$AUTH_DIR/node_modules" ] || die 'run npm ci in auth-server first'
  cmd_ports || die 'resolve port collisions before starting'
  compose up -d
  wait_for postgres 60 compose exec -T postgres pg_isready -U omi -d omi || die 'postgres not ready'
  wait_for redis 30 compose exec -T redis redis-cli -a "$OMI_LOCAL_REDIS_PASSWORD" ping || die 'redis not ready'
  wait_for minio 60 http_ok "$MINIO_ENDPOINT/minio/health/live" || die 'minio not ready'
  wait_for qdrant 60 curl --noproxy '*' -fsS -m 3 -H "api-key: $OMI_LOCAL_QDRANT_API_KEY" "http://127.0.0.1:$OMI_LOCAL_QDRANT_PORT/collections" || die 'qdrant not ready'
  wait_for typesense 60 http_ok "http://127.0.0.1:$OMI_LOCAL_TYPESENSE_PORT/health" || die 'typesense not ready'
  wait_for emulators 120 http_ok "http://127.0.0.1:$OMI_LOCAL_FIRESTORE_PORT/" || die 'emulators not ready'
  local fingerprint
  fingerprint="$(printf '%s' "$OMI_LOCAL_BETTER_AUTH_SECRET" | shasum -a 256 | cut -d' ' -f1)"
  (cd "$AUTH_DIR" && auth_env npm run --silent migrate) || die 'Better Auth migration failed'
  if [ -f "$STATE_ENV" ] && [ "$(cat "$STATE_ENV")" != "$fingerprint" ]; then
    stop_process auth-server
    compose exec -T postgres psql -U omi -d omi -c 'TRUNCATE jwks' >/dev/null
  fi
  render_profile
  write_child_env
  run_backend "$PYTHON_BIN" -m fork.migrate migrate >"$LOG_DIR/firestore-pg-migrate.log" 2>&1 || die 'firestore-pg migration failed; see logs (legacy disposable DB: reset)'
  start_process auth-server "${CLEAN_ENV[@]}" "DATABASE_URL=$PG_DSN" "BETTER_AUTH_SECRET=$OMI_LOCAL_BETTER_AUTH_SECRET" \
    "BETTER_AUTH_URL=$AUTH_URL" "PORT=$OMI_LOCAL_AUTH_PORT" 'HOST=127.0.0.1' \
    "AUTH_DEV_ISSUER_SECRET=$OMI_LOCAL_AUTH_DEV_ISSUER_SECRET" "AUTH_INTERNAL_ADMIN_SECRET=$OMI_LOCAL_INTERNAL_ADMIN_SECRET" \
    node "$AUTH_DIR/src/index.js"
  wait_for auth-server 60 http_ok "$AUTH_URL/health" || { stop_process auth-server; die 'auth-server not ready'; }
  printf '%s\n' "$fingerprint" >"$STATE_ENV"
  if [ "$NO_BACKEND" = false ]; then cmd_backend_up; fi
  log "local stack ready: $INSTANCE (state: $STATE_DIR)"
}
cmd_backend_stop() { stop_process queue-worker; stop_process backend; }
restore_selection() {
  if [ "$#" -eq 0 ] && [ -f "$STATE_DIR/ai-profile" ]; then
    OMI_LOCAL_AI_PROFILE="$(cat "$STATE_DIR/ai-profile")"
    if [ -z "$_prefix_explicit" ] && [ -f "$STATE_DIR/qdrant-prefix" ]; then
      OMI_LOCAL_QDRANT_COLLECTION_PREFIX="$(cat "$STATE_DIR/qdrant-prefix")"
    fi
  fi
}
cmd_backend_restart() {
  restore_selection "$@"
  cmd_backend_stop; cmd_backend_up "$@"
}
cmd_backend_status() {
  if pid_alive backend; then
    if http_ok "$BACKEND_URL/v1/health"; then log 'backend: healthy'; else log 'backend: running, not healthy'; fi
  else log 'backend: stopped or unowned'; fi
}
cmd_restart() {
  restore_selection "$@"
  cmd_backend_stop; stop_process auth-server; cmd_up "$@"
}
cmd_status() { compose ps; cmd_backend_status; }
cmd_down() { cmd_backend_stop; stop_process auth-server; compose down; }
cmd_verify() {
  pid_alive backend && http_ok "$BACKEND_URL/v1/health" || die 'owned backend is not healthy; run up first'
  [ -f "$CHILD_ENV_FILE" ] || die 'no runtime environment; run up first'
  run_backend "$PYTHON_BIN" "$DEV_DIR/local_verify.py" --evidence "$EVIDENCE_DIR/local-verify-$(date -u +%Y%m%dT%H%M%SZ).json"
}
cmd_env() {
  printf 'export %s=%q\n' OMI_LOCAL_BACKEND_URL "$BACKEND_URL" OMI_LOCAL_AUTH_URL "$AUTH_URL" \
    OMI_DESKTOP_API_URL "$BACKEND_URL/" OMI_PYTHON_API_URL "$BACKEND_URL/" \
    FIRESTORE_EMULATOR_HOST "127.0.0.1:$OMI_LOCAL_FIRESTORE_PORT" \
    FIREBASE_AUTH_EMULATOR_HOST "127.0.0.1:$OMI_LOCAL_FIREBASE_AUTH_PORT" \
    STORAGE_EMULATOR_HOST "127.0.0.1:$OMI_LOCAL_FIREBASE_STORAGE_PORT" \
    AUTH_DEV_ISSUER_SECRET "$OMI_LOCAL_AUTH_DEV_ISSUER_SECRET" FIREBASE_PROJECT_ID demo-omi-local
}
cmd_logs() {
  local service="${1:-backend}"
  case "$service" in backend|auth-server|queue-worker|firestore-pg-migrate|qdrant-migrate) tail -n 200 -f "$LOG_DIR/$service.log" ;; *) compose logs --tail 200 -f "$service" ;; esac
}
cmd_help() {
  cat <<'EOF'
Omi local dev — fork runtime, isolated by OMI_LOCAL_STATE_DIR.
  dev/local.sh up [--core-only | --operator-ai PROVIDER] [--no-backend]
  dev/local.sh restart [--core-only | --operator-ai PROVIDER]
  dev/local.sh status
  dev/local.sh verify
  dev/local.sh logs [service]
  dev/local.sh ports
  dev/local.sh env
  dev/local.sh down
  dev/local.sh reset
  dev/local.sh selfhost [--core-only | --operator-ai PROVIDER]
Default: core-only (no speech/chat models), with local embedding endpoint.
Operator providers: mimo-cn, openrouter, siliconflow, cloudflare-gateway.
All use self_hosted.local persistence. Credentials must be OMI_LOCAL_* scoped.
Config: ambient local-scoped variables > dev/local.env > dev/local.env.example.
EOF
}
command="${1:-help}"; shift || true
case "$command" in
  up) cmd_up "$@" ;;
  restart) cmd_restart "$@" ;;
  selfhost|backend-up) cmd_backend_up "$@" ;;
  backend-stop) cmd_backend_stop ;;
  backend-restart) cmd_backend_restart "$@" ;;
  backend-status) cmd_backend_status ;;
  status) cmd_status ;;
  ports) cmd_ports ;;
  verify) cmd_verify ;;
  env) cmd_env ;;
  logs) cmd_logs "$@" ;;
  down|--stop) cmd_down ;;
  reset) cmd_down; compose down --volumes; rm -f "$STATE_ENV" ;;
  --no-backend) cmd_up --no-backend ;;
  help|-h|--help) cmd_help ;;
  *) die "unknown command '$command' (try: dev/local.sh help)" ;;
esac
