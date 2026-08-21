#!/usr/bin/env bash
# Pre-cutover contract gate. It never changes a traffic route or touches the
# production DSN: the default lane owns an isolated Compose project and volume.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/dev/docker-compose.dev.yml"
CHECKER="$REPO_ROOT/.github/scripts/check_self_host_deployment.py"
INTEGRATION_TESTS=(
  "$REPO_ROOT/backend/firestore_pg/tests/test_migration_import.py"
  "$REPO_ROOT/backend/firestore_pg/tests/test_transaction_semantics.py"
  "$REPO_ROOT/backend/firestore_pg/tests/test_composite_indexes.py"
)
FIRESTORE_PG_MIGRATOR="$REPO_ROOT/backend/scripts/firestore_pg_migrate.py"
TARGET_SAFETY_CHECK="$REPO_ROOT/backend/scripts/validate_migration_test_targets.py"
SOURCE_WRITE_FREEZE_TOOL="$REPO_ROOT/backend/scripts/source_write_freeze.py"
AGENT_VM_RECONCILE_TOOL="$REPO_ROOT/backend/scripts/agent_vm_reconcile.py"
AGENT_VM_RECONCILE_TEST="$REPO_ROOT/backend/scripts/test-agent-vm-reconcile.sh"
SHADOW_DIFF="$REPO_ROOT/dev/shadow-diff.sh"
AUTH_FLOW_SMOKE="$REPO_ROOT/deploy/self-host/auth-flow-smoke.py"
LEGACY_JWKS_FIXTURE="$REPO_ROOT/auth-server/src/seed-legacy-jwk.js"
PY="${PYTHON:-$REPO_ROOT/backend/.venv/bin/python}"
EVIDENCE_FILE="${SELF_HOST_GATE_EVIDENCE:-$REPO_ROOT/deploy/self-host/migration-gate-evidence.json}"
MANAGED=1

usage() {
  echo "usage: $0 [--self-check] [--external] [--evidence PATH]" >&2
  echo "  --external  use pre-existing disposable PG/emulator targets; also requires a container-reachable AUTH_MIGRATION_DATABASE_URL" >&2
}

self_check() {
  local missing=0 path
  for path in "$COMPOSE_FILE" "$CHECKER" "$SHADOW_DIFF" "$AUTH_FLOW_SMOKE" "$LEGACY_JWKS_FIXTURE" "$FIRESTORE_PG_MIGRATOR" "$TARGET_SAFETY_CHECK" "$SOURCE_WRITE_FREEZE_TOOL" "$AGENT_VM_RECONCILE_TOOL" "$AGENT_VM_RECONCILE_TEST" "${INTEGRATION_TESTS[@]}"; do
    if [[ ! -f "$path" ]]; then
      echo "error: migration gate dependency missing: $path" >&2
      missing=1
    fi
  done
  [[ "$missing" -eq 0 ]] || return 1
  "$PY" "$CHECKER"
  "$PY" -m py_compile "$SOURCE_WRITE_FREEZE_TOOL" "$AGENT_VM_RECONCILE_TOOL"
  "$AGENT_VM_RECONCILE_TEST"
  echo "migration cutover gate self-check OK"
}

if [[ "${1:-}" == "--self-check" ]]; then
  if [[ ! -x "$PY" ]]; then
    PY="python3"
  fi
  self_check
  exit 0
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --external)
      MANAGED=0
      shift
      ;;
    --evidence)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      EVIDENCE_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ ! -x "$PY" ]]; then
  echo "error: backend interpreter not found at $PY (run make setup-backend or set PYTHON)" >&2
  exit 1
fi

PROJECT="omi-neutrality-gate-${$}"
GATE_PG_PORT="${MIGRATION_GATE_POSTGRES_PORT:-15434}"
GATE_FIRESTORE_PORT="${MIGRATION_GATE_FIRESTORE_PORT:-18080}"
GATE_AUTH_PORT="${MIGRATION_GATE_AUTH_PORT:-19099}"
GATE_STORAGE_PORT="${MIGRATION_GATE_STORAGE_PORT:-19199}"
GATE_BETTER_AUTH_PORT="${MIGRATION_GATE_BETTER_AUTH_PORT:-19300}"

compose_gate() {
  DEV_FIREBASE_CONTAINER_NAME="${PROJECT}-emulators" \
  DEV_POSTGRES_CONTAINER_NAME="${PROJECT}-postgres" \
  DEV_POSTGRES_PORT="$GATE_PG_PORT" \
  FIRESTORE_EMULATOR_PORT="$GATE_FIRESTORE_PORT" \
  FIREBASE_AUTH_EMULATOR_PORT="$GATE_AUTH_PORT" \
  FIREBASE_STORAGE_EMULATOR_PORT="$GATE_STORAGE_PORT" \
  docker compose --project-name "$PROJECT" --file "$COMPOSE_FILE" "$@"
}

cleanup() {
  if [[ -n "${AUTH_GATE_CONTAINER:-}" ]]; then
    docker rm --force "$AUTH_GATE_CONTAINER" >/dev/null 2>&1 || true
  fi
  if [[ "$MANAGED" -eq 1 ]]; then
    compose_gate down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
  if [[ -n "${AUTH_GATE_IMAGE:-}" ]]; then
    docker image rm "$AUTH_GATE_IMAGE" >/dev/null 2>&1 || true
  fi
  if [[ -n "${AUTH_JWKS_REGRESSION_DIR:-}" ]]; then
    rm -rf "$AUTH_JWKS_REGRESSION_DIR"
  fi
}
trap cleanup EXIT INT TERM

command -v docker >/dev/null || { echo "error: docker is required" >&2; exit 1; }
AUTH_GATE_RUN_ARGS=()
if [[ "$MANAGED" -eq 1 ]]; then
  compose_gate up --detach --build --wait postgres firebase-emulators
  export FIRESTORE_PG_DSN="postgresql+psycopg://omi:omi-dev-password@127.0.0.1:${GATE_PG_PORT}/omi"
  export FIRESTORE_EMULATOR_HOST="127.0.0.1:${GATE_FIRESTORE_PORT}"
  AUTH_MIGRATION_DATABASE_URL="postgresql://omi:omi-dev-password@postgres:5432/omi"
  AUTH_GATE_RUN_ARGS=(--network "${PROJECT}_default")
else
  : "${FIRESTORE_PG_DSN:?--external requires FIRESTORE_PG_DSN for a disposable non-production PostgreSQL database}"
  : "${FIRESTORE_EMULATOR_HOST:?--external requires FIRESTORE_EMULATOR_HOST}"
  : "${AUTH_MIGRATION_DATABASE_URL:?--external requires a PostgreSQL URL reachable from a Docker container (use host.docker.internal for a host database)}"
  "$PY" "$TARGET_SAFETY_CHECK"
fi

AUTH_GATE_IMAGE="${PROJECT}-auth-server"
docker build --file "$REPO_ROOT/auth-server/Dockerfile" --tag "$AUTH_GATE_IMAGE" "$REPO_ROOT"
AUTH_GATE_ENV=(
  --env NODE_ENV=production
  --env DATABASE_URL="$AUTH_MIGRATION_DATABASE_URL"
  --env BETTER_AUTH_SECRET=gate-only-better-auth-secret-32-characters-minimum
  --env BETTER_AUTH_URL=https://auth.gate.invalid
  --env BETTER_AUTH_TRUSTED_ORIGINS=https://app.gate.invalid
  --env BETTER_AUTH_IP_HEADERS=x-forwarded-for
  --env AUTH_INTERNAL_ADMIN_SECRET=gate-only-internal-admin-secret
  --env AUTH_JWT_ISSUER=https://auth.gate.invalid
  --env AUTH_JWT_AUDIENCE=https://auth.gate.invalid
)
docker run --rm "${AUTH_GATE_RUN_ARGS[@]}" "${AUTH_GATE_ENV[@]}" "$AUTH_GATE_IMAGE" node src/migrate.js
AUTH_JWKS_REGRESSION_DIR="$(mktemp -d)"
docker run --rm "${AUTH_GATE_RUN_ARGS[@]}" "${AUTH_GATE_ENV[@]}" \
  --volume "$AUTH_JWKS_REGRESSION_DIR:/evidence" \
  "$AUTH_GATE_IMAGE" node src/seed-legacy-jwk.js --output /evidence/legacy-jwk.json
docker run --rm "${AUTH_GATE_RUN_ARGS[@]}" "${AUTH_GATE_ENV[@]}" "$AUTH_GATE_IMAGE" node src/migrate.js
docker run --rm "${AUTH_GATE_RUN_ARGS[@]}" "${AUTH_GATE_ENV[@]}" "$AUTH_GATE_IMAGE" node src/migrate.js
docker run --rm "${AUTH_GATE_RUN_ARGS[@]}" "${AUTH_GATE_ENV[@]}" "$AUTH_GATE_IMAGE" node src/migrate.js --check

AUTH_GATE_CONTAINER="${PROJECT}-auth-server"
docker run --detach --name "$AUTH_GATE_CONTAINER" \
  --publish "127.0.0.1:${GATE_BETTER_AUTH_PORT}:3000" \
  "${AUTH_GATE_RUN_ARGS[@]}" "${AUTH_GATE_ENV[@]}" "$AUTH_GATE_IMAGE" node src/index.js >/dev/null
for _attempt in {1..30}; do
  if curl --fail --silent "http://127.0.0.1:${GATE_BETTER_AUTH_PORT}/ready" >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent "http://127.0.0.1:${GATE_BETTER_AUTH_PORT}/ready" >/dev/null
PYTHONPATH="$REPO_ROOT/backend${PYTHONPATH:+:$PYTHONPATH}" "$PY" "$AUTH_FLOW_SMOKE" \
  --base-url "http://127.0.0.1:${GATE_BETTER_AUTH_PORT}" \
  --issuer https://auth.gate.invalid \
  --audience https://auth.gate.invalid \
  --origin https://app.gate.invalid \
  --admin-secret gate-only-internal-admin-secret \
  --legacy-token-file "$AUTH_JWKS_REGRESSION_DIR/legacy-jwk.json"

export FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-demo-omi-local}"
export PYTHONPATH="$REPO_ROOT/backend${PYTHONPATH:+:$PYTHONPATH}"

"$PY" "$CHECKER"
"$PY" "$FIRESTORE_PG_MIGRATOR" migrate
"$PY" "$FIRESTORE_PG_MIGRATOR" migrate
"$PY" "$FIRESTORE_PG_MIGRATOR" check
"$PY" -m pytest -q "${INTEGRATION_TESTS[@]}"

SHADOW_DIR="$(mktemp -d)"
trap 'rm -rf "$SHADOW_DIR"; cleanup' EXIT INT TERM
SHADOW_REAL_OUT="$SHADOW_DIR/firestore-emulator.json" \
SHADOW_SHIM_OUT="$SHADOW_DIR/firestore-pg.json" \
FIRESTORE_PG_DSN="$FIRESTORE_PG_DSN" \
FIRESTORE_EMULATOR_HOST="$FIRESTORE_EMULATOR_HOST" \
FIREBASE_PROJECT_ID="$FIREBASE_PROJECT_ID" \
"$SHADOW_DIFF"

GATE_GIT_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)" \
GATE_EVIDENCE_FILE="$EVIDENCE_FILE" \
GATE_PG_DSN_HOST="${FIRESTORE_PG_DSN#*@}" \
GATE_EMULATOR_HOST="$FIRESTORE_EMULATOR_HOST" \
"$PY" - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

path = Path(os.environ['GATE_EVIDENCE_FILE'])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(
    json.dumps(
        {
            'schema_version': 1,
            'status': 'GO',
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'git_sha': os.environ['GATE_GIT_SHA'],
            'target': {
                'postgres': os.environ['GATE_PG_DSN_HOST'],
                'firestore_emulator': os.environ['GATE_EMULATOR_HOST'],
            },
            'gates': {
                'zero_vendor_static_config': 'passed',
                'better_auth_schema_migration': 'passed',
                'better_auth_session_jwt_jwks_backend_verification': 'passed',
                'firestore_pg_versioned_schema_migration': 'passed',
                'firestore_to_pg_full_path_import_reconciliation': 'passed',
                'firestore_pg_live_integration': 'passed',
                'firestore_emulator_shadow_diff': 'passed',
            },
            'authorizes_traffic_change': False,
        },
        indent=2,
        sort_keys=True,
    )
    + '\n',
    encoding='utf-8',
)
print(f'migration cutover evidence: {path}')
PY

echo "GO: contract gates passed. This evidence does not change or authorize a traffic route."
