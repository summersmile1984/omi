#!/usr/bin/env bash
# LIFECYCLE: permanent
# Production state/health operations for the single self-host Compose entry.

set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$OPS_DIR/../.." && pwd)"
COMPOSE_FILE="$OPS_DIR/compose.production.yml"
ENV_FILE="${SELF_HOST_ENV:-$OPS_DIR/.env.production}"
PY="${PYTHON:-python3}"
SNAPSHOT_TOOL="$OPS_DIR/volume-snapshot.py"
RUNTIME_EVIDENCE_TOOL="$OPS_DIR/runtime-evidence.py"
COMPOSE_WRAPPER="$OPS_DIR/compose-clean-env.sh"
CONFIG_CHECKER="$REPO_ROOT/.github/scripts/check_self_host_deployment.py"
APPLICATION_SERVICES=(queue-worker backend auth-server)
STATE_SERVICES=(postgres redis minio qdrant typesense searxng)
STATE_ARCHIVES=(redis minio qdrant typesense backend)
ARCHIVE_FILES=(postgres.dump redis.tar.gz minio.tar.gz qdrant.tar.gz typesense.tar.gz backend.tar.gz)

usage() {
  echo "usage: SELF_HOST_ENV=... $0 <self-check|start|status|runtime-evidence|metrics|backup DIR|verify-backup DIR|restore DIR|rollback-plan DIR>" >&2
}

compose() {
  bash "$COMPOSE_WRAPPER" "$ENV_FILE" "$COMPOSE_FILE" "$@"
}

effective_config_sha256() {
  "$PY" -c 'import runpy,sys; from pathlib import Path; m=runpy.run_path(sys.argv[1]); print(m["effective_compose_config_sha256"](compose_file=Path(sys.argv[2]),env_file=Path(sys.argv[3])))' "$RUNTIME_EVIDENCE_TOOL" "$COMPOSE_FILE" "$ENV_FILE"
}

require_runtime() {
  command -v docker >/dev/null || { echo "error: docker is required" >&2; exit 1; }
  [[ -f "$ENV_FILE" ]] || { echo "error: environment file not found: $ENV_FILE" >&2; exit 1; }
  [[ "$ENV_FILE" != *.example ]] || { echo "error: operations refuse the checked-in example environment" >&2; exit 1; }
  "$PY" "$CONFIG_CHECKER" --env-file "$ENV_FILE"
  compose config --quiet
}

absolute_directory() {
  local path="$1"
  [[ "$path" == /* && "$path" != "/" ]] || {
    echo "error: backup directory must be an absolute non-root path" >&2
    exit 1
  }
  printf '%s\n' "$path"
}

volume_name() {
  local service="$1" destination="$2" container volume
  container="$(compose ps --all --quiet "$service")"
  [[ -n "$container" ]] || { echo "error: $service container does not exist" >&2; exit 1; }
  volume="$(docker inspect --format "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{.Name}}{{end}}{{end}}" "$container")"
  [[ "$volume" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "error: could not resolve $service volume" >&2; exit 1; }
  printf '%s\n' "$volume"
}

helper_image() {
  compose config --format json | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["services"]["backend"]["image"])'
}

snapshot_volume() {
  local mode="$1" service="$2" destination="$3" archive="$4" volume image state_mode backup_mode
  volume="$(volume_name "$service" "$destination")"
  image="$(helper_image)"
  state_mode=ro
  backup_mode=rw
  if [[ "$mode" == restore ]]; then
    state_mode=rw
    backup_mode=ro
  fi
  docker run --rm --user 0 \
    --volume "$volume:/state:$state_mode" \
    --volume "$OPS_DIR:/ops:ro" \
    --volume "$(dirname "$archive"):/backup:$backup_mode" \
    "$image" python /ops/volume-snapshot.py "$mode" /state "/backup/$(basename "$archive")"
}

start_profile() {
  if [[ "${SELF_HOST_REQUIRE_ATTESTED_BUILD:-false}" == true ]]; then
    [[ "${OMI_SOURCE_GIT_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]] || {
      echo "error: attributed start requires OMI_SOURCE_GIT_COMMIT" >&2
      exit 1
    }
    [[ "${OMI_SOURCE_GIT_TREE:-}" =~ ^[0-9a-f]{40}$ ]] || {
      echo "error: attributed start requires OMI_SOURCE_GIT_TREE" >&2
      exit 1
    }
    [[ "${OMI_RUNTIME_CONFIG_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || {
      echo "error: attributed start requires OMI_RUNTIME_CONFIG_SHA256" >&2
      exit 1
    }
    local actual_config_sha256
    actual_config_sha256="$(effective_config_sha256)"
    [[ "$actual_config_sha256" == "$OMI_RUNTIME_CONFIG_SHA256" ]] || {
      echo "error: reviewed environment changed before attributed build" >&2
      exit 1
    }
    # Build from this checkout before any migration or serving container starts.
    # Content-addressed image IDs and embedded source labels are verified again
    # after the complete acceptance run, so a mutable old tag cannot be reused.
    compose build --pull auth-server backend
  fi
  # A previously successful one-shot container is not proof that the current
  # database is migrated: restore may have replaced PostgreSQL underneath it.
  # Quiesce callers, admit state services, and execute a fresh disposable
  # migration container before Auth/backend/worker traffic can resume.
  compose stop "${APPLICATION_SERVICES[@]}" >/dev/null 2>&1 || true
  compose up --detach --wait "${STATE_SERVICES[@]}"
  compose run --rm --no-deps -T auth-migrate
  compose run --rm --no-deps -T firestore-pg-migrate
  compose up --detach --wait --no-deps "${APPLICATION_SERVICES[@]}"
}

runtime_evidence() {
  require_runtime
  "$PY" "$RUNTIME_EVIDENCE_TOOL" \
    --compose-file "$COMPOSE_FILE" \
    --env-file "$ENV_FILE" \
    --expected-git-commit "${OMI_SOURCE_GIT_COMMIT:?OMI_SOURCE_GIT_COMMIT is required}" \
    --expected-git-tree "${OMI_SOURCE_GIT_TREE:?OMI_SOURCE_GIT_TREE is required}" \
    --expected-config-sha256 "${OMI_RUNTIME_CONFIG_SHA256:?OMI_RUNTIME_CONFIG_SHA256 is required}"
}

backup_state() {
  local directory git_sha
  directory="$(absolute_directory "$1")"
  mkdir -p "$directory"
  [[ -z "$(find "$directory" -mindepth 1 -maxdepth 1 -print -quit)" ]] || {
    echo "error: backup directory must be empty: $directory" >&2
    exit 1
  }
  start_profile
  compose stop "${APPLICATION_SERVICES[@]}"
  trap 'start_profile >/dev/null 2>&1 || true' EXIT INT TERM

  compose exec -T postgres sh -ec 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner' >"$directory/postgres.dump"
  compose exec -T redis redis-cli SAVE >/dev/null
  compose stop redis minio qdrant typesense
  snapshot_volume backup redis /data "$directory/redis.tar.gz"
  snapshot_volume backup minio /data "$directory/minio.tar.gz"
  snapshot_volume backup qdrant /qdrant/storage "$directory/qdrant.tar.gz"
  snapshot_volume backup typesense /data "$directory/typesense.tar.gz"
  snapshot_volume backup backend /app/syncing "$directory/backend.tar.gz"

  git_sha="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  "$PY" "$SNAPSHOT_TOOL" manifest "$directory" --git-sha "$git_sha" "${ARCHIVE_FILES[@]}"
  start_profile
  trap - EXIT INT TERM
  echo "backup OK: $directory"
}

restore_state() {
  local directory
  directory="$(absolute_directory "$1")"
  [[ "${SELF_HOST_RESTORE_ACK:-}" == "I_ACKNOWLEDGE_THIS_OVERWRITES_STATE" ]] || {
    echo "error: restore requires SELF_HOST_RESTORE_ACK=I_ACKNOWLEDGE_THIS_OVERWRITES_STATE" >&2
    exit 1
  }
  "$PY" "$SNAPSHOT_TOOL" verify "$directory"
  require_runtime
  compose stop queue-worker backend auth-server auth-migrate firestore-pg-migrate searxng typesense redis minio qdrant postgres || true
  snapshot_volume restore redis /data "$directory/redis.tar.gz"
  snapshot_volume restore minio /data "$directory/minio.tar.gz"
  snapshot_volume restore qdrant /qdrant/storage "$directory/qdrant.tar.gz"
  snapshot_volume restore typesense /data "$directory/typesense.tar.gz"
  snapshot_volume restore backend /app/syncing "$directory/backend.tar.gz"
  compose up --detach --wait postgres
  # pg_restore --clean only drops objects named in the archive. Recreate the
  # database so objects created after the backup cannot survive the rollback.
  compose exec -T postgres sh -ec 'dropdb -U "$POSTGRES_USER" --force --if-exists -- "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" -- "$POSTGRES_DB"'
  compose exec -T postgres sh -ec 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' <"$directory/postgres.dump"
  start_profile
  echo "restore OK: $directory"
}

status() {
  require_runtime
  compose ps
  local service container state health
  local unhealthy=()
  for service in "${STATE_SERVICES[@]}" auth-server backend queue-worker; do
    container="$(compose ps --quiet "$service")"
    if [[ -z "$container" ]]; then
      unhealthy+=("$service:missing")
      continue
    fi
    read -r state health < <(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container")
    if [[ "$state" != running || "$health" != healthy ]]; then
      unhealthy+=("$service:$state/$health")
    fi
  done
  ((${#unhealthy[@]} == 0)) || {
    echo "error: unhealthy services: ${unhealthy[*]}" >&2
    return 1
  }
}

metrics() {
  require_runtime
  local service container state health restarts queue_name queue_key
  for service in "${STATE_SERVICES[@]}" auth-server backend queue-worker; do
    container="$(compose ps --quiet "$service")"
    [[ -n "$container" ]] || { printf 'omi_container_up{service="%s"} 0\n' "$service"; continue; }
    read -r state health restarts < <(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}}' "$container")
    [[ "$state" == running ]] && printf 'omi_container_up{service="%s"} 1\n' "$service" || printf 'omi_container_up{service="%s"} 0\n' "$service"
    [[ "$health" == healthy ]] && printf 'omi_container_healthy{service="%s"} 1\n' "$service" || printf 'omi_container_healthy{service="%s"} 0\n' "$service"
    printf 'omi_container_restarts_total{service="%s"} %s\n' "$service" "$restarts"
  done
  for queue_name in sync audio-merge account-deletion finalization; do
    queue_key="omi:queue:$queue_name"
    printf 'omi_queue_ready{queue="%s"} %s\n' "$queue_name" "$(compose exec -T redis redis-cli ZCARD "$queue_key:ready" | tr -d '\r')"
    printf 'omi_queue_pending{queue="%s"} %s\n' "$queue_name" "$(compose exec -T redis redis-cli ZCARD "$queue_key:pending" | tr -d '\r')"
    printf 'omi_queue_dlq{queue="%s"} %s\n' "$queue_name" "$(compose exec -T redis redis-cli LLEN "$queue_key:dlq" | tr -d '\r')"
  done
  printf 'omi_postgres_database_bytes %s\n' "$(compose exec -T postgres sh -ec 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT pg_database_size(current_database())"' | tr -d '\r')"
}

case "${1:-}" in
  self-check)
    [[ -f "$COMPOSE_FILE" && -f "$COMPOSE_WRAPPER" && -f "$SNAPSHOT_TOOL" && -f "$RUNTIME_EVIDENCE_TOOL" && -f "$CONFIG_CHECKER" ]] || exit 1
    "$PY" -m py_compile "$SNAPSHOT_TOOL" "$RUNTIME_EVIDENCE_TOOL"
    bash -n "$0" "$COMPOSE_WRAPPER"
    echo "self-host operations self-check OK"
    ;;
  status)
    status
    ;;
  runtime-evidence)
    runtime_evidence
    ;;
  start)
    require_runtime
    start_profile
    status
    ;;
  metrics)
    metrics
    ;;
  backup)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    require_runtime
    backup_state "$2"
    ;;
  verify-backup)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    "$PY" "$SNAPSHOT_TOOL" verify "$(absolute_directory "$2")"
    echo "backup verification OK: $2"
    ;;
  restore)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    restore_state "$2"
    ;;
  rollback-plan)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    "$PY" "$SNAPSHOT_TOOL" verify "$(absolute_directory "$2")"
    echo "rollback evidence OK; drain public traffic, run restore with the explicit acknowledgement, then rerun migration and acceptance gates"
    ;;
  *)
    usage
    exit 2
    ;;
esac
