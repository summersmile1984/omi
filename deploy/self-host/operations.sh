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
ARCHIVE_FILES=(postgres.dump.enc redis.tar.gz.enc minio.tar.gz.enc qdrant.tar.gz.enc typesense.tar.gz.enc backend.tar.gz.enc)

usage() {
  echo "usage: SELF_HOST_ENV=... SELF_HOST_BACKUP_KEY_FILE=... $0 <self-check|start|status|runtime-evidence|metrics|backup DIR|verify-backup DIR|restore DIR|rollback-plan DIR>" >&2
}

compose() {
  bash "$COMPOSE_WRAPPER" "$ENV_FILE" "$COMPOSE_FILE" "$@"
}

effective_config_sha256() {
  "$PY" -c 'import runpy,sys; from pathlib import Path; m=runpy.run_path(sys.argv[1]); print(m["effective_compose_config_sha256"](compose_file=Path(sys.argv[2]),env_file=Path(sys.argv[3])))' "$RUNTIME_EVIDENCE_TOOL" "$COMPOSE_FILE" "$ENV_FILE"
}

runtime_fingerprint() {
  compose config --format json | "$PY" -c '
import hashlib,json,sys
config=json.load(sys.stdin)
services=config.get("services")
if not isinstance(services, dict): raise SystemExit("effective Compose services are missing")
images={}
for service in ("auth-server", "backend"):
    row=services.get(service)
    image=row.get("image") if isinstance(row, dict) else None
    if not isinstance(image, str) or not image: raise SystemExit(f"effective Compose image is missing for {service}")
    images[service]=image
print(hashlib.sha256(json.dumps(images, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest())
'
}

migration_fingerprint() {
  git -C "$REPO_ROOT" hash-object -- \
    auth-server/src/migrate.js \
    auth-server/src/auth.js \
    backend/scripts/firestore_pg_migrate.py \
    backend/firestore_pg/migrations.py | "$PY" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

verify_backup() {
  local directory runtime_sha256 config_sha256 migration_sha256 key_file
  require_runtime
  directory="$(absolute_directory "$1")"
  key_file="$(backup_key_file)"
  runtime_sha256="$(runtime_fingerprint)"
  config_sha256="$(effective_config_sha256)"
  migration_sha256="$(migration_fingerprint)"
  verify_snapshot "$directory" "$key_file" "$runtime_sha256" "$config_sha256" "$migration_sha256"
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

backup_key_file() {
  local key_file="${SELF_HOST_BACKUP_KEY_FILE:-}" mode
  [[ -n "$key_file" && "$key_file" == /* && "$key_file" != "/" ]] || {
    echo "error: SELF_HOST_BACKUP_KEY_FILE must be an absolute path to a private key file" >&2
    exit 1
  }
  [[ -f "$key_file" && ! -L "$key_file" ]] || {
    echo "error: SELF_HOST_BACKUP_KEY_FILE is missing or is a symlink" >&2
    exit 1
  }
  mode="$(stat -f '%Lp' "$key_file" 2>/dev/null || true)"
  [[ "$mode" == 600 ]] || mode="$(stat -c '%a' "$key_file" 2>/dev/null || true)"
  [[ "$mode" == 600 ]] || {
    echo "error: SELF_HOST_BACKUP_KEY_FILE must be mode 0600" >&2
    exit 1
  }
  printf '%s\n' "$key_file"
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
  local mode="$1" service="$2" destination="$3" archive="$4" key_file volume image state_mode backup_mode
  key_file="$(backup_key_file)"
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
    --volume "$key_file:/backup-key/key:ro" \
    --volume "$(dirname "$archive"):/backup:$backup_mode" \
    "$image" python /ops/volume-snapshot.py "$mode" /state "/backup/$(basename "$archive")" --key-file /backup-key/key
}

seal_stdin() {
  local archive="$1" key_file image
  key_file="$(backup_key_file)"
  image="$(helper_image)"
  docker run --rm --interactive --user 0 \
    --volume "$OPS_DIR:/ops:ro" \
    --volume "$key_file:/backup-key/key:ro" \
    --volume "$(dirname "$archive"):/backup:rw" \
    "$image" python /ops/volume-snapshot.py seal-stdin "/backup/$(basename "$archive")" --key-file /backup-key/key
}

write_snapshot_manifest() {
  local directory="$1" git_sha="$2" runtime_sha256="$3" config_sha256="$4" migration_sha256="$5" image
  image="$(helper_image)"
  docker run --rm --user 0 \
    --volume "$OPS_DIR:/ops:ro" \
    --volume "$directory:/backup:rw" \
    "$image" python /ops/volume-snapshot.py manifest /backup \
      --git-sha "$git_sha" \
      --runtime-fingerprint "$runtime_sha256" \
      --config-fingerprint "$config_sha256" \
      --migration-fingerprint "$migration_sha256" \
      "${ARCHIVE_FILES[@]}"
}

verify_snapshot() {
  local directory="$1" key_file="$2" runtime_sha256="$3" config_sha256="$4" migration_sha256="$5" image
  image="$(helper_image)"
  docker run --rm --user 0 \
    --volume "$OPS_DIR:/ops:ro" \
    --volume "$key_file:/backup-key/key:ro" \
    --volume "$directory:/backup:ro" \
    "$image" python /ops/volume-snapshot.py verify /backup \
      --expected-files "${ARCHIVE_FILES[@]}" \
      --expected-runtime-fingerprint "$runtime_sha256" \
      --expected-config-fingerprint "$config_sha256" \
      --expected-migration-fingerprint "$migration_sha256" \
      --key-file /backup-key/key
}

open_snapshot() {
  local archive="$1" plaintext="$2" key_file="$3" image
  image="$(helper_image)"
  docker run --rm --user 0 \
    --volume "$OPS_DIR:/ops:ro" \
    --volume "$key_file:/backup-key/key:ro" \
    --volume "$(dirname "$archive"):/backup:rw" \
    "$image" python /ops/volume-snapshot.py open \
      "/backup/$(basename "$archive")" \
      "/backup/$(basename "$plaintext")" \
      --key-file /backup-key/key
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
  # This command is consumed as a JSON API by the acceptance pipeline. Keep
  # checker diagnostics on stderr so stdout remains exactly one JSON object.
  require_runtime >&2
  "$PY" "$RUNTIME_EVIDENCE_TOOL" \
    --compose-file "$COMPOSE_FILE" \
    --env-file "$ENV_FILE" \
    --expected-git-commit "${OMI_SOURCE_GIT_COMMIT:?OMI_SOURCE_GIT_COMMIT is required}" \
    --expected-git-tree "${OMI_SOURCE_GIT_TREE:?OMI_SOURCE_GIT_TREE is required}" \
    --expected-config-sha256 "${OMI_RUNTIME_CONFIG_SHA256:?OMI_RUNTIME_CONFIG_SHA256 is required}"
}

backup_state() {
  local directory git_sha runtime_sha256 config_sha256 migration_sha256 key_file
  directory="$(absolute_directory "$1")"
  key_file="$(backup_key_file)"
  case "$key_file" in
    "$directory"/*)
      echo "error: backup key must be stored outside the backup directory" >&2
      exit 1
      ;;
  esac
  mkdir -p "$directory"
  [[ -z "$(find "$directory" -mindepth 1 -maxdepth 1 -print -quit)" ]] || {
    echo "error: backup directory must be empty: $directory" >&2
    exit 1
  }
  start_profile
  compose stop "${APPLICATION_SERVICES[@]}"
  trap 'start_profile >/dev/null 2>&1 || true' EXIT INT TERM

  compose exec -T postgres sh -ec 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner' \
    | seal_stdin "$directory/postgres.dump.enc"
  compose exec -T redis redis-cli SAVE >/dev/null
  compose stop redis minio qdrant typesense
  snapshot_volume backup redis /data "$directory/redis.tar.gz.enc"
  snapshot_volume backup minio /data "$directory/minio.tar.gz.enc"
  snapshot_volume backup qdrant /qdrant/storage "$directory/qdrant.tar.gz.enc"
  snapshot_volume backup typesense /data "$directory/typesense.tar.gz.enc"
  snapshot_volume backup backend /app/syncing "$directory/backend.tar.gz.enc"

  git_sha="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  runtime_sha256="$(runtime_fingerprint)"
  config_sha256="$(effective_config_sha256)"
  migration_sha256="$(migration_fingerprint)"
  write_snapshot_manifest "$directory" "$git_sha" "$runtime_sha256" "$config_sha256" "$migration_sha256"
  start_profile
  trap - EXIT INT TERM
  echo "backup OK: $directory"
}

restore_state() {
  local directory key_file postgres_restore
  directory="$(absolute_directory "$1")"
  [[ "${SELF_HOST_RESTORE_ACK:-}" == "I_ACKNOWLEDGE_THIS_OVERWRITES_STATE" ]] || {
    echo "error: restore requires SELF_HOST_RESTORE_ACK=I_ACKNOWLEDGE_THIS_OVERWRITES_STATE" >&2
    exit 1
  }
  key_file="$(backup_key_file)"
  verify_backup "$directory"
  compose stop queue-worker backend auth-server auth-migrate firestore-pg-migrate searxng typesense redis minio qdrant postgres || true
  snapshot_volume restore redis /data "$directory/redis.tar.gz.enc"
  snapshot_volume restore minio /data "$directory/minio.tar.gz.enc"
  snapshot_volume restore qdrant /qdrant/storage "$directory/qdrant.tar.gz.enc"
  snapshot_volume restore typesense /data "$directory/typesense.tar.gz.enc"
  snapshot_volume restore backend /app/syncing "$directory/backend.tar.gz.enc"
  compose up --detach --wait postgres
  # pg_restore --clean only drops objects named in the archive. Recreate the
  # database so objects created after the backup cannot survive the rollback.
  compose exec -T postgres sh -ec 'dropdb -U "$POSTGRES_USER" --force --if-exists -- "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" -- "$POSTGRES_DB"'
  postgres_restore="$directory/.postgres.dump.restore"
  trap 'rm -f "$postgres_restore"' EXIT INT TERM
  open_snapshot "$directory/postgres.dump.enc" "$postgres_restore" "$key_file"
  compose exec -T postgres sh -ec 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' <"$postgres_restore"
  rm -f "$postgres_restore"
  trap - EXIT INT TERM
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
    verify_backup "$2"
    echo "backup verification OK: $2"
    ;;
  restore)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    restore_state "$2"
    ;;
  rollback-plan)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    verify_backup "$2"
    echo "rollback evidence OK; drain public traffic, run restore with the explicit acknowledgement, then rerun migration and acceptance gates"
    ;;
  *)
    usage
    exit 2
    ;;
esac
