#!/usr/bin/env bash
# Reproducible shadow-diff regression lane for the firestore_pg shim.
#
# Runs the same Firestore API scenario sequence against the real SDK
# (Firestore emulator) and the shim (PostgreSQL), then diffs the normalized
# JSON. A mismatch exits 1 — intended to be run from CI or a dev stack hook.
#
# Usage:
#   dev/shadow-diff.sh            # full run: real + shim + diff
#   dev/shadow-diff.sh --shim-only   # shim mode only (no emulator needed)
#
# Requires: docker (emulators + PG from dev-up.sh --no-backend), backend/.venv.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
PY="$BACKEND_DIR/.venv/bin/python"
DIFF="$BACKEND_DIR/firestore_pg/tests/shadow_diff.py"
REAL_OUT="/tmp/shadow-real.json"
SHIM_OUT="/tmp/shadow-shim.json"

export FIRESTORE_PG_DSN="postgresql+psycopg://omi:omi-dev-password@localhost:5434/omi"
export FIRESTORE_EMULATOR_HOST=localhost:8080
export FIREBASE_PROJECT_ID=demo-omi-local

if [[ ! -x "$PY" ]]; then
  echo "error: backend venv not found at $PY (run scripts/sync-python-deps.sh)" >&2
  exit 1
fi

echo "==> shim mode (PostgreSQL)"
"$PY" "$DIFF" --mode shim --out "$SHIM_OUT"

if [[ "${1:-}" == "--shim-only" ]]; then
  echo "shim-only OK"
  exit 0
fi

echo "==> real mode (Firestore emulator)"
env -u FIRESTORE_PG_DSN \
  FIRESTORE_EMULATOR_HOST=localhost:8080 \
  FIREBASE_PROJECT_ID=demo-omi-local \
  "$PY" "$DIFF" --mode real --out "$REAL_OUT"

echo "==> diff"
"$PY" "$DIFF" --diff "$REAL_OUT" "$SHIM_OUT"
