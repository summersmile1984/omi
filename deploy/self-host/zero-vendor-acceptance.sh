#!/usr/bin/env bash
# LIFECYCLE: permanent
# Zero-vendor contract acceptance. The hermetic product-loop lane denies all
# outbound DNS/socket access; --live additionally admits the production stack.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PYTHON:-$ROOT/backend/.venv/bin/python}"
CHECKER="$ROOT/.github/scripts/check_self_host_deployment.py"
OPS="$ROOT/deploy/self-host/operations.sh"
E2E_RUNNER="$ROOT/backend/testing/e2e/run.sh"
E2E_GUARD="$ROOT/backend/testing/e2e/conftest.py"
AUTH_SMOKE="$ROOT/deploy/self-host/auth-flow-smoke.py"
LIVE_REPLACEMENT_SMOKE="$ROOT/deploy/self-host/live-replacement-smoke.py"
EVIDENCE="${SELF_HOST_ACCEPTANCE_EVIDENCE:-${TMPDIR:-/tmp}/omi-zero-vendor-acceptance-evidence.json}"
MODE=contracts
LIVE_REPLACEMENT_JSON=''

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
  for path in "$CHECKER" "$OPS" "$E2E_RUNNER" "$E2E_GUARD" "$AUTH_SMOKE" "$LIVE_REPLACEMENT_SMOKE"; do
    [[ -f "$path" ]] || { echo "error: acceptance dependency missing: $path" >&2; return 1; }
  done
  "$PY" -m py_compile "$AUTH_SMOKE" "$LIVE_REPLACEMENT_SMOKE"
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
elif [[ $# -ne 0 ]]; then
  echo "usage: $0 [--self-check|--live]" >&2
  exit 2
fi

[[ -x "$PY" ]] || PY=python3
self_check
"$PY" "$CHECKER" --env-file "${SELF_HOST_ENV:-$ROOT/deploy/self-host/.env.production.example}"

if [[ "$MODE" == live ]]; then
  : "${SELF_HOST_ENV:?--live requires SELF_HOST_ENV pointing to production configuration}"
  SELF_HOST_ENV="$SELF_HOST_ENV" "$OPS" start
  dotenv_value() {
    "$PY" -c 'import sys; key=sys.argv[1]; values=dict(line.strip().split("=",1) for line in open(sys.argv[2], encoding="utf-8") if line.strip() and not line.lstrip().startswith("#") and "=" in line); print(values[key])' "$1" "$SELF_HOST_ENV"
  }
  AUTH_ORIGIN="$(dotenv_value BETTER_AUTH_TRUSTED_ORIGINS)"
  LIVE_OUTPUT="$(docker compose \
    --env-file "$SELF_HOST_ENV" \
    --file "$ROOT/deploy/self-host/compose.production.yml" \
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

GIT_SHA="$(git -C "$ROOT" rev-parse HEAD)" \
ACCEPTANCE_MODE="$MODE" \
ACCEPTANCE_EVIDENCE="$EVIDENCE" \
LIVE_REPLACEMENT_JSON="$LIVE_REPLACEMENT_JSON" \
"$PY" - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

path = Path(os.environ['ACCEPTANCE_EVIDENCE'])
path.parent.mkdir(parents=True, exist_ok=True)
live_replacement = (
    json.loads(os.environ['LIVE_REPLACEMENT_JSON'])
    if os.environ['ACCEPTANCE_MODE'] == 'live'
    else None
)
path.write_text(
    json.dumps(
        {
            'schema_version': 1,
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'git_sha': os.environ['GIT_SHA'],
            'mode': os.environ['ACCEPTANCE_MODE'],
            'gates': {
                'zero_vendor_static_config': 'passed',
                'undeclared_dns_and_socket_egress': 'denied',
                'hermetic_capture_understand_remember_retrieve_act_contract': 'passed',
                'hermetic_account_deletion_contract': 'passed',
                'hermetic_contract_uses_replacement_services': False,
                'live_capture_understand_remember_retrieve_act': 'not_run',
                'production_services_healthy': (
                    'passed' if os.environ['ACCEPTANCE_MODE'] == 'live' else 'not_run'
                ),
                'live_replacement_services': (
                    live_replacement if os.environ['ACCEPTANCE_MODE'] == 'live' else 'not_run'
                ),
            },
            # The live smoke covers a real SenseVoice PCM decode but not the
            # complete generic-model-backed product loop. Model quality and a
            # reverse-proxy traffic switch remain operator change-record steps.
            'authorizes_production_cutover': False,
        },
        indent=2,
        sort_keys=True,
    )
    + '\n',
    encoding='utf-8',
)
print(f'zero-vendor acceptance evidence: {path}')
PY
