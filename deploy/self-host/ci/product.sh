#!/usr/bin/env bash
# LIFECYCLE: permanent
# Same product cases on a complete real-model runtime locally and in CI.
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
# A hosted operator AI (openrouter / siliconflow / cloudflare-gateway) owns
# embeddings over the wire, so the local BGE-M3 store is not provisioned.
# MiMo keeps the local embedding service. Embedding stores are unused for
# the hosted shape, mandatory for MiMo; the Python product.py enforces the
# pairing once the secret+provider pair is known.
case "${SELF_HOST_CI_OPERATOR_PROVIDER:-}" in
  openrouter|siliconflow|cloudflare-gateway) hosted=1 ;;
  *) hosted=0 ;;
esac
if [ -n "${SELF_HOST_CI_MIMO_SECRET_FILE:-}" ]; then
  if [ -n "${SELF_HOST_CI_LLM_STORE:-}${SELF_HOST_CI_SPEECH_STORE:-}" ]; then
    echo 'MiMo requires only the embedding store; do not select native LLM/speech stores' >&2
    exit 2
  fi
  args+=(--embedding-store "${SELF_HOST_CI_EMBEDDING_STORE:?MiMo requires the BGE-M3 model store}"
    --mimo-secret-file "$SELF_HOST_CI_MIMO_SECRET_FILE")
elif [ -n "${SELF_HOST_CI_OPERATOR_SECRET_FILE:-}" ]; then
  # A hosted operator AI replaces the native LLM, speech and embedding
  # stores. The brand manifest declares which provider the stage selects;
  # the secret file carries that provider's bearer.
  if [ -n "${SELF_HOST_CI_LLM_STORE:-}${SELF_HOST_CI_SPEECH_STORE:-}" ]; then
    echo 'Operator AI requires only the embedding store; do not select native LLM/speech stores' >&2
    exit 2
  fi
  if [ -z "${SELF_HOST_CI_OPERATOR_PROVIDER:-}" ]; then
    echo 'SELF_HOST_CI_OPERATOR_PROVIDER is required alongside SELF_HOST_CI_OPERATOR_SECRET_FILE' >&2
    exit 2
  fi
  if [ "$hosted" -eq 0 ]; then
    args+=(--embedding-store "${SELF_HOST_CI_EMBEDDING_STORE:?MiMo requires the BGE-M3 model store}")
  elif [ -n "${SELF_HOST_CI_EMBEDDING_STORE:-}" ]; then
    echo 'A hosted operator AI owns embeddings; do not provision SELF_HOST_CI_EMBEDDING_STORE' >&2
    exit 2
  fi
  args+=(--operator-secret-file "$SELF_HOST_CI_OPERATOR_SECRET_FILE"
    --operator-provider "$SELF_HOST_CI_OPERATOR_PROVIDER")
else
  echo 'the product fixture requires SELF_HOST_CI_MIMO_SECRET_FILE or SELF_HOST_CI_OPERATOR_SECRET_FILE;' >&2
  echo 'a native local text model is refused at self-host admission' >&2
  exit 2
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
