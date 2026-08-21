#!/usr/bin/env bash
# LIFECYCLE: permanent
# Zero-vendor contract acceptance. The hermetic product-loop lane denies all
# outbound DNS/socket access; --live additionally admits the production stack.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PYTHON:-$ROOT/backend/.venv/bin/python}"
CHECKER="$ROOT/.github/scripts/check_self_host_deployment.py"
OPS="$ROOT/deploy/self-host/operations.sh"
COMPOSE_WRAPPER="$ROOT/deploy/self-host/compose-clean-env.sh"
E2E_RUNNER="$ROOT/backend/testing/e2e/run.sh"
E2E_GUARD="$ROOT/backend/testing/e2e/conftest.py"
AUTH_SMOKE="$ROOT/deploy/self-host/auth-flow-smoke.py"
LIVE_REPLACEMENT_SMOKE="$ROOT/deploy/self-host/live-replacement-smoke.py"
CUTOVER_GATE="$ROOT/deploy/self-host/cutover-https-gate.sh"
CUTOVER_SMOKE="$ROOT/deploy/self-host/cutover-live-smoke.py"
CUTOVER_OVERLAY="$ROOT/deploy/self-host/compose.cutover-acceptance.yml"
CUTOVER_PROXY="$ROOT/deploy/self-host/nginx.cutover-acceptance.conf"
REALTIME_RELAY_FIXTURE="$ROOT/deploy/self-host/realtime-relay-fixture.py"
EGRESS_POLICY_CONTRACT="$ROOT/deploy/self-host/egress-policy-contract.py"
EVIDENCE_BUILDER="$ROOT/deploy/self-host/acceptance_evidence.py"
RUNTIME_EVIDENCE_TOOL="$ROOT/deploy/self-host/runtime-evidence.py"
PUBLIC_OBJECT_EVIDENCE_TOOL="$ROOT/deploy/self-host/public_object_evidence.py"
SOURCE_WRITE_FREEZE_TOOL="$ROOT/backend/scripts/source_write_freeze.py"
AGENT_VM_RECONCILE_TOOL="$ROOT/backend/scripts/agent_vm_reconcile.py"
AGENT_VM_RECONCILE_TEST="$ROOT/backend/scripts/test-agent-vm-reconcile.sh"
EVIDENCE="${SELF_HOST_ACCEPTANCE_EVIDENCE:-${TMPDIR:-/tmp}/omi-zero-vendor-acceptance-evidence.json}"
MODE=contracts
LIVE_REPLACEMENT_JSON=''
ASSEMBLED_LOOP_JSON=''
RUNTIME_EVIDENCE_JSON=''
RECOVERY_EVIDENCE_JSON=''

LOOP_TESTS=(
  testing/e2e/test_listen_stt.py
  testing/e2e/test_conversation_processing_deterministic.py
  testing/e2e/test_canonical_memory_pipeline.py
  testing/e2e/test_retrieval_search.py
  testing/e2e/test_task_integrations.py
  testing/e2e/test_account_deletion_cloud_tasks.py
)

self_check() {
  local path
  for path in "$CHECKER" "$OPS" "$COMPOSE_WRAPPER" "$E2E_RUNNER" "$E2E_GUARD" "$AUTH_SMOKE" "$LIVE_REPLACEMENT_SMOKE" "$CUTOVER_GATE" "$CUTOVER_SMOKE" "$CUTOVER_OVERLAY" "$CUTOVER_PROXY" "$REALTIME_RELAY_FIXTURE" "$EGRESS_POLICY_CONTRACT" "$EVIDENCE_BUILDER" "$RUNTIME_EVIDENCE_TOOL" "$PUBLIC_OBJECT_EVIDENCE_TOOL" "$SOURCE_WRITE_FREEZE_TOOL" "$AGENT_VM_RECONCILE_TOOL" "$AGENT_VM_RECONCILE_TEST"; do
    [[ -f "$path" ]] || { echo "error: acceptance dependency missing: $path" >&2; return 1; }
  done
  "$PY" -m py_compile "$AUTH_SMOKE" "$LIVE_REPLACEMENT_SMOKE" "$CUTOVER_SMOKE" "$REALTIME_RELAY_FIXTURE" "$EGRESS_POLICY_CONTRACT" "$EVIDENCE_BUILDER" "$RUNTIME_EVIDENCE_TOOL" "$PUBLIC_OBJECT_EVIDENCE_TOOL" "$SOURCE_WRITE_FREEZE_TOOL" "$AGENT_VM_RECONCILE_TOOL"
  "$AGENT_VM_RECONCILE_TEST"
  grep -q 'blocked outbound network connection' "$E2E_GUARD"
  grep -q 'blocked DNS lookup' "$E2E_GUARD"
  for path in "${LOOP_TESTS[@]}"; do
    [[ -f "$ROOT/backend/$path" ]] || { echo "error: product-loop test missing: $path" >&2; return 1; }
  done
  "$OPS" self-check
  echo "zero-vendor acceptance self-check OK"
}

if [[ "${1:-}" == --self-check ]]; then
  self_check
  exit 0
elif [[ "${1:-}" == --live ]]; then
  MODE=live
elif [[ "${1:-}" == --cutover-live ]]; then
  MODE=cutover-live
elif [[ "${1:-}" == --external-cutover-live ]]; then
  MODE=external-cutover-live
elif [[ $# -ne 0 ]]; then
  echo "usage: $0 [--self-check|--live|--cutover-live|--external-cutover-live]" >&2
  exit 2
fi

[[ -x "$PY" ]] || PY=python3
SOURCE_ATTRIBUTION_ARGS=(--source-attribution --root "$ROOT")
if [[ "$MODE" == cutover-live || "$MODE" == external-cutover-live ]]; then
  SOURCE_ATTRIBUTION_ARGS+=(--require-clean)
fi
SOURCE_ATTRIBUTION_JSON="$("$PY" "$EVIDENCE_BUILDER" "${SOURCE_ATTRIBUTION_ARGS[@]}")"
self_check
"$PY" "$CHECKER" --env-file "${SELF_HOST_ENV:-$ROOT/deploy/self-host/.env.production.example}"

if [[ "$MODE" != contracts ]]; then
  : "${SELF_HOST_ENV:?live modes require SELF_HOST_ENV pointing to production configuration}"
  if [[ "$MODE" == external-cutover-live ]]; then
    : "${SELF_HOST_SOURCE_WRITE_FREEZE_LEASE:?external cutover requires SELF_HOST_SOURCE_WRITE_FREEZE_LEASE}"
    : "${SELF_HOST_SOURCE_PROJECT:?external cutover requires SELF_HOST_SOURCE_PROJECT}"
    : "${SELF_HOST_SOURCE_DATABASE:?external cutover requires SELF_HOST_SOURCE_DATABASE}"
    : "${SELF_HOST_SOURCE_ENDPOINT:?external cutover requires SELF_HOST_SOURCE_ENDPOINT}"
    "$PY" "$SOURCE_WRITE_FREEZE_TOOL" verify "$SELF_HOST_SOURCE_WRITE_FREEZE_LEASE" \
      --source-project "$SELF_HOST_SOURCE_PROJECT" \
      --source-database "$SELF_HOST_SOURCE_DATABASE" \
      --source-endpoint "$SELF_HOST_SOURCE_ENDPOINT" \
      --scope firestore --scope storage >/dev/null
    : "${SELF_HOST_RECOVERY_EVIDENCE:?external cutover requires SELF_HOST_RECOVERY_EVIDENCE pointing to an operator recovery-drill evidence JSON file}"
    if [[ "$SELF_HOST_RECOVERY_EVIDENCE" != /* || ! -f "$SELF_HOST_RECOVERY_EVIDENCE" || -L "$SELF_HOST_RECOVERY_EVIDENCE" ]]; then
      echo "ERROR: SELF_HOST_RECOVERY_EVIDENCE must be an existing non-symlink absolute JSON file" >&2
      exit 1
    fi
    RECOVERY_EVIDENCE_JSON="$($PY -c 'import json,sys; value=json.load(open(sys.argv[1], encoding="utf-8")); print(json.dumps(value, separators=(",", ":")))' "$SELF_HOST_RECOVERY_EVIDENCE")"
  fi
  export OMI_SOURCE_GIT_COMMIT OMI_SOURCE_GIT_TREE OMI_RUNTIME_CONFIG_SHA256
  OMI_SOURCE_GIT_COMMIT="$(printf '%s' "$SOURCE_ATTRIBUTION_JSON" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["git_commit"])')"
  OMI_SOURCE_GIT_TREE="$(printf '%s' "$SOURCE_ATTRIBUTION_JSON" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["git_tree"])')"
  OMI_RUNTIME_CONFIG_SHA256="$("$PY" -c 'import runpy,sys; from pathlib import Path; m=runpy.run_path(sys.argv[1]); print(m["effective_compose_config_sha256"](compose_file=Path(sys.argv[2]),env_file=Path(sys.argv[3])))' "$RUNTIME_EVIDENCE_TOOL" "$ROOT/deploy/self-host/compose.production.yml" "$SELF_HOST_ENV")"
  SELF_HOST_ENV="$SELF_HOST_ENV" SELF_HOST_REQUIRE_ATTESTED_BUILD=true "$OPS" start
  dotenv_value() {
    "$PY" -c 'import sys; key=sys.argv[1]; values=dict(line.strip().split("=",1) for line in open(sys.argv[2], encoding="utf-8") if line.strip() and not line.lstrip().startswith("#") and "=" in line); print(values[key])' "$1" "$SELF_HOST_ENV"
  }
  AUTH_ORIGIN="$(dotenv_value BETTER_AUTH_TRUSTED_ORIGINS)"
  LIVE_OUTPUT="$(bash "$COMPOSE_WRAPPER" "$SELF_HOST_ENV" "$ROOT/deploy/self-host/compose.production.yml" \
    run --rm --no-deps -T \
    --volume "$LIVE_REPLACEMENT_SMOKE:/tmp/live-replacement-smoke.py:ro" \
    --env "SELF_HOST_AUTH_ORIGIN=${AUTH_ORIGIN%%,*}" \
    backend python /tmp/live-replacement-smoke.py)"
  printf '%s\n' "$LIVE_OUTPUT"
  LIVE_REPLACEMENT_JSON="$(printf '%s\n' "$LIVE_OUTPUT" | "$PY" -c '
import json,sys
for line in reversed(sys.stdin.read().splitlines()):
    try:
        value=json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(value, dict) and value.get("status") == "passed":
        print(json.dumps(value, separators=(",", ":")))
        break
else:
    raise SystemExit("live replacement smoke returned no evidence object")
')"
  if [[ "$MODE" == cutover-live || "$MODE" == external-cutover-live ]]; then
    if [[ "$MODE" == cutover-live ]]; then
      CUTOVER_OUTPUT="$(SELF_HOST_ENV="$SELF_HOST_ENV" SELF_HOST_ACCEPTANCE_ALLOW_CONTROL_SEED=true "$CUTOVER_GATE" --local)"
    else
      CUTOVER_OUTPUT="$(SELF_HOST_ENV="$SELF_HOST_ENV" "$CUTOVER_GATE" --external)"
    fi
    printf '%s\n' "$CUTOVER_OUTPUT"
    ASSEMBLED_LOOP_JSON="$(printf '%s\n' "$CUTOVER_OUTPUT" | "$PY" -c '
import json,sys
for line in reversed(sys.stdin.read().splitlines()):
    try:
        value=json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(value, dict) and value.get("status") == "passed" and value.get("assembled_product_loop"):
        print(json.dumps(value, separators=(",", ":")))
        break
else:
    raise SystemExit("assembled cutover smoke returned no evidence object")
')"
  fi
fi

# This is a hermetic application-contract lane: conftest installs hard
# DNS/socket denial before importing FastAPI, but replaces storage/provider
# boundaries with fakes. It is not evidence that the live replacement services
# were selected. --live runs that separate proof above before these contracts.
if command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1; then
  (cd "$ROOT/backend" && bash testing/e2e/run.sh -q "${LOOP_TESTS[@]}")
else
  # macOS does not ship GNU timeout. Keep this gate bounded without requiring
  # a Homebrew-only prerequisite; conftest still installs the same hard egress
  # denial in the child pytest process.
  (cd "$ROOT/backend" && "$PY" -c "import tiktoken; tiktoken.encoding_for_model('gpt-4')")
  (cd "$ROOT/backend" && MEMORY_MODE=read "$PY" - "${LOOP_TESTS[@]}" <<'PY'
import subprocess
import sys

try:
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', *sys.argv[1:]],
        check=False,
        timeout=120,
    )
except subprocess.TimeoutExpired as error:
    print('ERROR: zero-vendor product-loop pytest exceeded 120 seconds', file=sys.stderr)
    raise SystemExit(124) from error
raise SystemExit(result.returncode)
PY
  )
fi

if [[ "$MODE" != contracts ]]; then
  RUNTIME_EVIDENCE_JSON="$(SELF_HOST_ENV="$SELF_HOST_ENV" "$OPS" runtime-evidence)"
  printf '%s\n' "$RUNTIME_EVIDENCE_JSON"
fi

ACCEPTANCE_MODE="$MODE" \
ACCEPTANCE_EVIDENCE="$EVIDENCE" \
SOURCE_ATTRIBUTION_JSON="$SOURCE_ATTRIBUTION_JSON" \
LIVE_REPLACEMENT_JSON="$LIVE_REPLACEMENT_JSON" \
ASSEMBLED_LOOP_JSON="$ASSEMBLED_LOOP_JSON" \
RUNTIME_EVIDENCE_JSON="$RUNTIME_EVIDENCE_JSON" \
RECOVERY_EVIDENCE_JSON="$RECOVERY_EVIDENCE_JSON" \
"$PY" "$EVIDENCE_BUILDER"
