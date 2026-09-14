#!/usr/bin/env bash
# LIFECYCLE: permanent
# Same bounded product slice on a local Docker engine and the existing CI lane.
#
# The workflow pins SELF_HOST_CI_REPORT_DIR and uploads it as the
# fork-selfhost-fixture-report-<sha> artifact, so a failing command-NN.log survives the run
# instead of disappearing with the runner's temporary directory.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
REPORT="${SELF_HOST_CI_REPORT_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/memweft-product-contract.XXXXXX")}"
args=(--output "$REPORT/server" --brand-id core-server-fixture --port "${SELF_HOST_CI_PORT:-34800}" --self-test)
if [ -n "${SELF_HOST_CI_RUNTIME_IMAGE:-}" ]; then
  args+=(--runtime-image "$SELF_HOST_CI_RUNTIME_IMAGE")
fi
if [ -n "${SELF_HOST_CI_EMBEDDING_STORE:-}${SELF_HOST_CI_LLM_STORE:-}${SELF_HOST_CI_SPEECH_STORE:-}" ]; then
  args+=(--embedding-store "${SELF_HOST_CI_EMBEDDING_STORE:?all three model stores are required}"
    --llm-store "${SELF_HOST_CI_LLM_STORE:?all three model stores are required}"
    --speech-store "${SELF_HOST_CI_SPEECH_STORE:?all three model stores are required}")
fi

# The fixture reports a failed step as "inspect command-NN.log", but those logs live
# in the report directory that only exists on the machine that ran it: the fork gate
# failed on main at command-09 for weeks with nothing diagnosable in the job output.
# Surface the tail of every command log on failure instead.
dump_fixture_logs() {
  local log
  shopt -s nullglob
  local logs=("$REPORT"/server/command-*.log)
  shopt -u nullglob
  if [ "${#logs[@]}" -eq 0 ]; then
    printf '==> fixture logs: none under %s/server\n' "$REPORT" >&2
    return 0
  fi
  printf '==> fixture logs (%s/server), tail of each command\n' "$REPORT" >&2
  for log in "${logs[@]}"; do
    printf '\n----- %s -----\n' "$(basename "$log")" >&2
    tail -n 40 "$log" >&2
  done
  printf '==> full report: %s\n' "$REPORT" >&2
}

cd "$ROOT"
"${PYTHON:-python3}" deploy/self-host/ci/test_product.py
if ! "${PYTHON:-python3}" deploy/self-host/ci/product.py "${args[@]}"; then
  dump_fixture_logs
  exit 1
fi
