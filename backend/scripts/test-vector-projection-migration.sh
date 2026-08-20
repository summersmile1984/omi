#!/usr/bin/env bash
# LIFECYCLE: permanent
set -euo pipefail

backend_root="$(cd "$(dirname "$0")/.." && pwd)"
selected_tests="$(mktemp)"
trap 'rm -f "$selected_tests"' EXIT

printf '%s\n' \
  tests/unit/test_vector_projection_migration_cli.py \
  tests/unit/test_embedding_vector_adapters.py \
  > "$selected_tests"

cd "$backend_root"
BACKEND_UNIT_TEST_FILE_LIST="$selected_tests" bash test.sh
